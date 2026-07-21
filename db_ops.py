"""
db_ops.py
~~~~~~~~~
Relational, transaction-safe writes into Kodi's MyVideos131 MariaDB schema.

Schema targets (Kodi 20+ / Omega):
    path          – directories (strPath ends with '/')
    files         – individual video files, FK → path.idPath
    movie         – movie metadata, FK → files.idFile, path.idPath
    tvshow        – show metadata, FK → path (via tvshowlinkpath)
    tvshowlinkpath– M:N join between tvshow and path
    seasons       – season rows, FK → tvshow.idShow
    episode       – episode metadata, FK → files.idFile, tvshow.idShow, seasons.idSeason

Each public function is fully self-contained and manages its own transaction.
Private helpers (_prefixed) assume they are called inside an active transaction.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Optional

import pymysql
import pymysql.cursors

from nfo_parser import EpisodeNfo, MovieNfo, TvShowNfo
from sidecars import SubtitleSidecar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(
    host: str,
    port: int,
    user: str,
    password: str,
    db: str,
) -> pymysql.Connection:
    """Open and return a new pymysql connection to the Kodi MariaDB instance."""
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


# ---------------------------------------------------------------------------
# Transaction context manager
# ---------------------------------------------------------------------------

@contextmanager
def transaction(conn):
    """Commit on success, roll back on any exception, then re-raise."""
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Upsert outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpsertOutcome:
    """Result of an upsert: identifies the row and whether it was created."""
    media_id: int     # idMovie / idEpisode
    id_file: int      # files.idFile the media row points at
    created: bool     # True when a new media row was inserted


# ---------------------------------------------------------------------------
# Private helpers (no transaction management – called inside a transaction)
# ---------------------------------------------------------------------------

def _get_or_create_path(conn, strPath: str) -> int:
    """Return idPath for *strPath*, inserting a new row when absent.

    Kodi convention: all paths end with ``/``.
    """
    if not strPath.endswith("/"):
        strPath += "/"
    with conn.cursor() as cur:
        cur.execute("SELECT idPath FROM path WHERE strPath = %s", (strPath,))
        row = cur.fetchone()
        if row:
            return int(row["idPath"])
        cur.execute(
            """
            INSERT INTO path
                (strPath, strContent, strScraper, strHash,
                 scanRecursive, useFolderNames, noUpdate, exclude, allAudio, dateAdded)
            VALUES (%s, '', '', '', 0, 0, 1, 0, 0, %s)
            """,
            (strPath, _now()),
        )
        return int(cur.lastrowid)


def _get_or_create_file(conn, idPath: int, strFilename: str) -> int:
    """Return idFile for *(idPath, strFilename)*, inserting when absent."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idFile FROM files WHERE idPath = %s AND strFilename = %s",
            (idPath, strFilename),
        )
        row = cur.fetchone()
        if row:
            return int(row["idFile"])
        cur.execute(
            """
            INSERT INTO files (idPath, strFilename, playCount, dateAdded)
            VALUES (%s, %s, 0, %s)
            """,
            (idPath, strFilename, _now()),
        )
        return int(cur.lastrowid)


def get_file_id(conn, directory_uri: str, filename: str) -> Optional[int]:
    """Return files.idFile for *(directory_uri, filename)*, or None.

    Read-only; does not create path/file rows.
    """
    if not directory_uri.endswith("/"):
        directory_uri += "/"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.idFile
            FROM files f
            JOIN path p ON p.idPath = f.idPath
            WHERE p.strPath = %s AND f.strFilename = %s
            """,
            (directory_uri, filename),
        )
        row = cur.fetchone()
        return int(row["idFile"]) if row else None


def _movie_exists(conn, idFile: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT idMovie FROM movie WHERE idFile = %s", (idFile,))
        return cur.fetchone() is not None


def _is_nonempty(value: Optional[str]) -> bool:
    return bool((value or "").strip())


def _movie_year_from_values(year: str, premiered: str) -> str:
    year = (year or "").strip()
    if re.fullmatch(r"\d{4}", year):
        return year

    premiered = (premiered or "").strip()
    if len(premiered) >= 4 and premiered[:4].isdigit():
        return premiered[:4]
    return ""


def _movie_year_from_nfo(nfo: MovieNfo) -> str:
    return _movie_year_from_values(nfo.year, nfo.premiered)


def _movie_is_fallback_like(nfo: MovieNfo) -> bool:
    """Heuristic for guessed metadata: title/year only, everything else empty."""
    return (
        _is_nonempty(nfo.title)
        and _is_nonempty(_movie_year_from_nfo(nfo))
        and not any(
            _is_nonempty(v)
            for v in (
                nfo.originaltitle,
                nfo.outline,
                nfo.plot,
                nfo.tagline,
                nfo.premiered,
                nfo.mpaa,
                nfo.imdb_id,
                nfo.thumb,
                nfo.fanart,
            )
        )
        and not nfo.genre
        and not nfo.country
        and not nfo.director
        and not nfo.writer
    )


def _movie_row_for_update(conn, idMovie: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT idMovie, idFile,
                   c00, c01, c02, c03, c06, c07, c10, c11, c12, c13, c14, c15, c22,
                   premiered
            FROM movie
            WHERE idMovie = %s
            """,
            (idMovie,),
        )
        return cur.fetchone()


def _find_existing_movie_id(conn, nfo: MovieNfo) -> Optional[int]:
    """Find an existing movie by stable identity (IMDB, then title+year)."""
    imdb_id = (nfo.imdb_id or "").strip()
    with conn.cursor() as cur:
        if imdb_id:
            cur.execute(
                "SELECT idMovie FROM movie WHERE c13 = %s ORDER BY idMovie ASC LIMIT 1",
                (imdb_id,),
            )
            row = cur.fetchone()
            if row:
                return int(row["idMovie"])

        title = (nfo.title or "").strip()
        year = _movie_year_from_nfo(nfo)
        if title and year:
            cur.execute(
                """
                SELECT idMovie
                FROM movie
                WHERE LOWER(TRIM(c00)) = LOWER(TRIM(%s))
                  AND (
                        c07 = %s
                        OR LEFT(COALESCE(premiered, ''), 4) = %s
                      )
                ORDER BY idMovie ASC
                LIMIT 1
                """,
                (title, year, year),
            )
            row = cur.fetchone()
            if row:
                return int(row["idMovie"])

    return None


def _build_movie_values_from_nfo(nfo: MovieNfo, full_smb_path: str) -> dict:
    return {
        "c00": nfo.title,
        "c01": nfo.outline,
        "c02": nfo.plot,
        "c03": nfo.tagline,
        "c06": " / ".join(nfo.writer),
        "c07": nfo.year,
        # Kodi orders movie titles by sort title (c10) when present.
        "c10": nfo.sorttitle or nfo.title,
        "c11": nfo.originaltitle,
        "c12": nfo.thumb,
        "c13": nfo.imdb_id,
        "c14": " / ".join(nfo.genre),
        "c15": " / ".join(nfo.country),
        "c22": full_smb_path,
        "premiered": nfo.premiered or nfo.year,
    }


def _merged_movie_values(existing_row: dict, nfo: MovieNfo, full_smb_path: str, preserve_existing: bool) -> dict:
    """Merge incoming values with existing DB row.

    When preserve_existing=True (fallback-like metadata), empty incoming values
    never overwrite existing populated values.
    """
    incoming = _build_movie_values_from_nfo(nfo, full_smb_path)
    merged = {}
    for key, new_value in incoming.items():
        if preserve_existing and not _is_nonempty(new_value):
            merged[key] = existing_row.get(key) or ""
        else:
            merged[key] = new_value
    return merged


def _update_movie_row(conn, idMovie: int, merged_values: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE movie
            SET c00 = %s,
                c01 = %s,
                c02 = %s,
                c03 = %s,
                c06 = %s,
                c07 = %s,
                c10 = %s,
                c11 = %s,
                c12 = %s,
                c13 = %s,
                c14 = %s,
                c15 = %s,
                c22 = %s,
                premiered = %s
            WHERE idMovie = %s
            """,
            (
                merged_values["c00"],
                merged_values["c01"],
                merged_values["c02"],
                merged_values["c03"],
                merged_values["c06"],
                merged_values["c07"],
                merged_values["c10"],
                merged_values["c11"],
                merged_values["c12"],
                merged_values["c13"],
                merged_values["c14"],
                merged_values["c15"],
                merged_values["c22"],
                merged_values["premiered"],
                idMovie,
            ),
        )


def _movie_quality_score(row: dict) -> int:
    score = 0
    for key in ("c00", "c01", "c02", "c03", "c06", "c07", "c10", "c11", "c12", "c13", "c14", "c15", "c22", "premiered"):
        if _is_nonempty(row.get(key)):
            score += 1

    # Prefer rows that have stable IDs and artwork.
    if _is_nonempty(row.get("c13")):
        score += 3
    if _is_nonempty(row.get("c12")):
        score += 1
    return score


def _movie_dedupe_key(row: dict) -> Optional[str]:
    imdb_id = (row.get("c13") or "").strip()
    if imdb_id:
        return f"imdb:{imdb_id.lower()}"

    title = (row.get("c00") or "").strip().lower()
    year = _movie_year_from_values(row.get("c07") or "", row.get("premiered") or "")
    if title and year:
        return f"title_year:{title}:{year}"
    return None


def dedupe_movies(conn) -> int:
    """Remove duplicate movie rows, keeping the most complete record per key.

    Duplicate key priority:
    1) imdb_id (c13)
    2) title + year
    """
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT idMovie, idFile,
                       c00, c01, c02, c03, c06, c07, c10, c11, c12, c13, c14, c15, c22,
                       premiered
                FROM movie
                ORDER BY idMovie ASC
                """
            )
            rows = cur.fetchall() or []

        groups: dict[str, list[dict]] = {}
        for row in rows:
            key = _movie_dedupe_key(row)
            if not key:
                continue
            groups.setdefault(key, []).append(row)

        removed = 0
        for dup_rows in groups.values():
            if len(dup_rows) < 2:
                continue

            keep_row = sorted(
                dup_rows,
                key=lambda r: (_movie_quality_score(r), -int(r["idMovie"])),
                reverse=True,
            )[0]
            keep_id = int(keep_row["idMovie"])

            # Fill any missing values on the kept row from less complete duplicates.
            merged = dict(keep_row)
            for row in dup_rows:
                if int(row["idMovie"]) == keep_id:
                    continue
                for key in ("c00", "c01", "c02", "c03", "c06", "c07", "c10", "c11", "c12", "c13", "c14", "c15", "c22", "premiered"):
                    if not _is_nonempty(merged.get(key)) and _is_nonempty(row.get(key)):
                        merged[key] = row.get(key)

            _update_movie_row(conn, keep_id, merged)
            _ensure_movie_default_video_version(conn, keep_id, int(merged["idFile"]))
            _upsert_movie_art(conn, keep_id, merged.get("c12") or "")

            for row in dup_rows:
                drop_id = int(row["idMovie"])
                if drop_id == keep_id:
                    continue

                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM art WHERE media_type = 'movie' AND media_id = %s",
                        (drop_id,),
                    )
                    cur.execute(
                        "DELETE FROM videoversion WHERE media_type = 'movie' AND idMedia = %s",
                        (drop_id,),
                    )
                    cur.execute("DELETE FROM movie WHERE idMovie = %s", (drop_id,))
                removed += 1

        return removed


def rebuild_movie_sort_titles(conn) -> dict:
    """Rebuild movie sort titles (c10) from title (c00) for all movies.

    Returns a small summary dictionary with total rows inspected and rows
    updated.
    """
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT idMovie, c00, c10 FROM movie ORDER BY idMovie ASC")
            rows = cur.fetchall() or []

        updated = 0
        for row in rows:
            title = (row.get("c00") or "").strip()
            current_sort = (row.get("c10") or "").strip()

            # Keep sort title aligned with displayed title for a full reset.
            desired_sort = title
            if current_sort == desired_sort:
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE movie SET c10 = %s WHERE idMovie = %s",
                    (desired_sort, int(row["idMovie"])),
                )
            updated += 1

        return {"total": len(rows), "updated": updated}


def _movie_default_video_version_exists(conn, idMovie: int, idFile: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT idFile
            FROM videoversion
            WHERE idMedia = %s AND media_type = 'movie' AND itemType = 0 AND idFile = %s
            """,
            (idMovie, idFile),
        )
        return cur.fetchone() is not None


def _ensure_movie_default_video_version(conn, idMovie: int, idFile: int) -> None:
    if _movie_default_video_version_exists(conn, idMovie, idFile):
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO videoversion (idFile, idMedia, media_type, itemType, idType)
            VALUES (%s, %s, 'movie', 0, 40400)
            """,
            (idFile, idMovie),
        )



def _upsert_movie_art_type(conn, idMovie: int, art_type: str, art_url: str) -> None:
    if not art_url:
        return

    with conn.cursor() as cur:
        cur.execute(
            "SELECT art_id, url FROM art WHERE media_id = %s AND media_type = 'movie' AND type = %s",
            (idMovie, art_type),
        )
        row = cur.fetchone()
        if row:
            if row.get("url") != art_url:
                cur.execute(
                    "UPDATE art SET url = %s WHERE art_id = %s",
                    (art_url, int(row["art_id"])),
                )
            return

        cur.execute(
            "INSERT INTO art (media_id, media_type, type, url) VALUES (%s, 'movie', %s, %s)",
            (idMovie, art_type, art_url),
        )


def _upsert_movie_art(conn, idMovie: int, poster_url: str = "", fanart_url: str = "") -> None:
    """Store Kodi movie artwork rows (poster/fanart), updating in place."""
    _upsert_movie_art_type(conn, idMovie, "poster", poster_url)
    _upsert_movie_art_type(conn, idMovie, "fanart", fanart_url)


def _has_art_type(conn, media_type: str, media_id: int, art_type: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT art_id
            FROM art
            WHERE media_type = %s AND media_id = %s AND type = %s AND COALESCE(url, '') <> ''
            LIMIT 1
            """,
            (media_type, media_id, art_type),
        )
        return cur.fetchone() is not None


def get_movies_missing_artwork(conn) -> list[dict]:
    """Return movie rows that are missing poster and/or fanart in the art table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.idMovie, m.c00 AS title, p.strPath,
                   MAX(CASE WHEN a.type = 'poster' AND COALESCE(a.url, '') <> '' THEN 1 ELSE 0 END) AS hasPoster,
                   MAX(CASE WHEN a.type = 'fanart' AND COALESCE(a.url, '') <> '' THEN 1 ELSE 0 END) AS hasFanart
            FROM movie m
            JOIN files f ON f.idFile = m.idFile
            JOIN path p ON p.idPath = f.idPath
            LEFT JOIN art a
              ON a.media_type = 'movie'
             AND a.media_id = m.idMovie
             AND a.type IN ('poster', 'fanart')
            GROUP BY m.idMovie, m.c00, p.strPath
            HAVING hasPoster = 0 OR hasFanart = 0
            ORDER BY m.idMovie ASC
            """
        )
        return cur.fetchall() or []


def get_tvshows_missing_artwork(conn) -> list[dict]:
    """Return tvshow rows that are missing poster and/or fanart in the art table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.idShow, t.c00 AS title, p.strPath,
                   MAX(CASE WHEN a.type = 'poster' AND COALESCE(a.url, '') <> '' THEN 1 ELSE 0 END) AS hasPoster,
                   MAX(CASE WHEN a.type = 'fanart' AND COALESCE(a.url, '') <> '' THEN 1 ELSE 0 END) AS hasFanart
            FROM tvshow t
            JOIN tvshowlinkpath tlp ON tlp.idShow = t.idShow
            JOIN path p ON p.idPath = tlp.idPath
            LEFT JOIN art a
              ON a.media_type = 'tvshow'
             AND a.media_id = t.idShow
             AND a.type IN ('poster', 'fanart')
            GROUP BY t.idShow, t.c00, p.strPath
            HAVING hasPoster = 0 OR hasFanart = 0
            ORDER BY t.idShow ASC
            """
        )
        return cur.fetchall() or []


def _upsert_media_art_type(conn, media_type: str, media_id: int, art_type: str, art_url: str) -> bool:
    if not art_url:
        return False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT art_id, url FROM art WHERE media_type = %s AND media_id = %s AND type = %s",
            (media_type, media_id, art_type),
        )
        row = cur.fetchone()
        if row:
            if row.get("url") != art_url:
                cur.execute("UPDATE art SET url = %s WHERE art_id = %s", (art_url, int(row["art_id"])))
                return True
            return False

        cur.execute(
            "INSERT INTO art (media_id, media_type, type, url) VALUES (%s, %s, %s, %s)",
            (media_id, media_type, art_type, art_url),
        )
        return True


def upsert_art_batch(conn, media_type: str, media_id: int, art: dict) -> int:
    """Upsert several art rows for one media item in a single transaction.

    *art* maps Kodi art type -> URL; blank URLs are skipped.  Returns the
    number of rows inserted or updated.
    """
    with transaction(conn):
        changed = 0
        for art_type, art_url in art.items():
            if _upsert_media_art_type(conn, media_type, media_id, art_type, art_url or ""):
                changed += 1
        return changed


def backfill_movie_art_if_missing(conn, directory_uri: str, filename: str, poster_url: str = "", fanart_url: str = "") -> bool:
    """Backfill movie poster/fanart only when missing for an existing movie row."""
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT idPath FROM path WHERE strPath = %s", (directory_uri,))
            path_row = cur.fetchone()
            if not path_row:
                return False
            idPath = int(path_row["idPath"])

            cur.execute(
                "SELECT idFile FROM files WHERE idPath = %s AND strFilename = %s",
                (idPath, filename),
            )
            file_row = cur.fetchone()
            if not file_row:
                return False
            idFile = int(file_row["idFile"])

            cur.execute("SELECT idMovie, c12 FROM movie WHERE idFile = %s", (idFile,))
            movie_row = cur.fetchone()
            if not movie_row:
                return False
            idMovie = int(movie_row["idMovie"])

        changed = False
        if poster_url and not _has_art_type(conn, "movie", idMovie, "poster"):
            changed = _upsert_media_art_type(conn, "movie", idMovie, "poster", poster_url) or changed
            if not (movie_row.get("c12") or ""):
                with conn.cursor() as cur:
                    cur.execute("UPDATE movie SET c12 = %s WHERE idMovie = %s", (poster_url, idMovie))
                changed = True

        if fanart_url and not _has_art_type(conn, "movie", idMovie, "fanart"):
            changed = _upsert_media_art_type(conn, "movie", idMovie, "fanart", fanart_url) or changed

        return changed


def backfill_tvshow_art_if_missing(conn, show_directory_uri: str, poster_url: str = "", fanart_url: str = "") -> bool:
    """Backfill tvshow poster/fanart only when missing for an existing tvshow row."""
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT idPath FROM path WHERE strPath = %s", (show_directory_uri,))
            path_row = cur.fetchone()
            if not path_row:
                return False
            idPath = int(path_row["idPath"])

            cur.execute("SELECT idShow FROM tvshowlinkpath WHERE idPath = %s LIMIT 1", (idPath,))
            show_row = cur.fetchone()
            if not show_row:
                return False
            idShow = int(show_row["idShow"])

        changed = False
        if poster_url and not _has_art_type(conn, "tvshow", idShow, "poster"):
            changed = _upsert_media_art_type(conn, "tvshow", idShow, "poster", poster_url) or changed
            with conn.cursor() as cur:
                cur.execute("SELECT c05 FROM tvshow WHERE idShow = %s", (idShow,))
                tv_row = cur.fetchone()
                if tv_row and not (tv_row.get("c05") or ""):
                    cur.execute("UPDATE tvshow SET c05 = %s WHERE idShow = %s", (poster_url, idShow))
                    changed = True

        if fanart_url and not _has_art_type(conn, "tvshow", idShow, "fanart"):
            changed = _upsert_media_art_type(conn, "tvshow", idShow, "fanart", fanart_url) or changed

        return changed


def _episode_row_matches(
    row: dict,
    idShow: int,
    idSeason: int,
    season_num: int,
    episode_num: int,
    nfo: EpisodeNfo,
) -> bool:
    return (
        int(row["idShow"]) == idShow
        and int(row["idSeason"]) == idSeason
        and (row["c00"] or "") == nfo.title
        and (row["c01"] or "") == nfo.outline
        and (row["c02"] or "") == nfo.plot
        and (row["c04"] or "") == " / ".join(nfo.writer)
        and (row["c05"] or "") == nfo.aired
        and (row["c06"] or "") == nfo.thumb
        and str(row["c10"] or "") == str(episode_num)
        and (row["c11"] or "") == nfo.tvdb_id
        and str(row["c12"] or "") == str(season_num)
        and str(row["c13"] or "") == str(episode_num)
        and (row["c18"] or "") == " / ".join(nfo.director)
    )


def _get_or_create_season(conn, idShow: int, season_number: int) -> int:
    """Return idSeason for *(idShow, season_number)*, inserting when absent."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idSeason FROM seasons WHERE idShow = %s AND season = %s",
            (idShow, season_number),
        )
        row = cur.fetchone()
        if row:
            return int(row["idSeason"])
        if season_number == 0:
            name = "Specials"
        elif season_number == -1:
            name = "All seasons"
        else:
            name = f"Season {season_number}"
        cur.execute(
            "INSERT INTO seasons (idShow, season, name, userrating) VALUES (%s, %s, %s, 0)",
            (idShow, season_number, name),
        )
        return int(cur.lastrowid)


def ensure_season(conn, idShow: int, season_number: int) -> int:
    """Public wrapper for :func:`_get_or_create_season` with its own transaction."""
    with transaction(conn):
        return _get_or_create_season(conn, idShow, season_number)


def _link_tvshow_path(conn, idShow: int, idPath: int) -> None:
    """Insert a tvshowlinkpath row if not already present."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idShow FROM tvshowlinkpath WHERE idShow = %s AND idPath = %s",
            (idShow, idPath),
        )
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (%s, %s)",
                (idShow, idPath),
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upsert_movie(
    conn,
    directory_uri: str,
    filename: str,
    full_smb_path: str,
    nfo: MovieNfo,
) -> UpsertOutcome:
    """Idempotently insert a movie into Kodi's database.

    Always returns an :class:`UpsertOutcome`; ``created`` is False when an
    existing row was updated in place (either matched by logical identity or
    by the ``idFile`` in the ``movie`` table).

    Mapping:
        c00 = title          c01 = outline (plot summary)
        c02 = plot           c03 = tagline
        c06 = writers        c07 = year
        c10 = sort title     c11 = original title
        c12 = thumb URL      c13 = IMDB id
        c14 = genre          c15 = country
        c22 = full SMB path  premiered = premiered / year
    """
    with transaction(conn):
        preserve_existing = _movie_is_fallback_like(nfo)

        # If we can already identify the movie logically, do not create a second row.
        existing_id = _find_existing_movie_id(conn, nfo)
        if existing_id is not None:
            existing_row = _movie_row_for_update(conn, existing_id)
            if existing_row:
                merged = _merged_movie_values(existing_row, nfo, full_smb_path, preserve_existing)
                # Keep existing poster field stable for already-known movies.
                merged["c12"] = existing_row.get("c12") or ""
                _update_movie_row(conn, existing_id, merged)
                _ensure_movie_default_video_version(conn, existing_id, int(existing_row["idFile"]))
                return UpsertOutcome(existing_id, int(existing_row["idFile"]), False)

        idPath = _get_or_create_path(conn, directory_uri)
        idFile = _get_or_create_file(conn, idPath, filename)
        if _movie_exists(conn, idFile):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT idMovie, idFile,
                           c00, c01, c02, c03, c06, c07, c10, c11, c12, c13, c14, c15, c22,
                           premiered
                    FROM movie
                    WHERE idFile = %s
                    """,
                    (idFile,),
                )
                row = cur.fetchone()
            if row:
                idMovie = int(row["idMovie"])
                merged = _merged_movie_values(row, nfo, full_smb_path, preserve_existing)
                # Keep existing poster field stable for already-known movies.
                merged["c12"] = row.get("c12") or ""
                _update_movie_row(conn, idMovie, merged)
                _ensure_movie_default_video_version(conn, idMovie, idFile)
                return UpsertOutcome(idMovie, idFile, False)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO movie
                    (idFile,
                     c00, c01, c02, c03, c06, c07, c10, c11, c12, c13, c14, c15, c22,
                     userrating, premiered)
                VALUES
                    (%s,
                     %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     0, %s)
                """,
                (
                    idFile,
                    nfo.title,                          # c00
                    nfo.outline,                        # c01
                    nfo.plot,                           # c02
                    nfo.tagline,                        # c03
                    " / ".join(nfo.writer),             # c06
                    nfo.year,                           # c07
                    nfo.sorttitle or nfo.title,         # c10
                    nfo.originaltitle,                  # c11
                    nfo.thumb,                          # c12
                    nfo.imdb_id,                        # c13
                    " / ".join(nfo.genre),              # c14
                    " / ".join(nfo.country),            # c15
                    full_smb_path,                      # c22
                    nfo.premiered or nfo.year,          # premiered
                ),
            )
            idMovie = int(cur.lastrowid)

        _ensure_movie_default_video_version(conn, idMovie, idFile)
        _upsert_movie_art(conn, idMovie, poster_url=nfo.thumb, fanart_url=nfo.fanart)

        logger.debug("Inserted movie idMovie=%d title=%r", idMovie, nfo.title)
        return UpsertOutcome(idMovie, idFile, True)


def get_or_create_tvshow(
    conn,
    show_directory_uri: str,
    nfo: TvShowNfo,
) -> int:
    """Return idShow for the given show directory, inserting when absent.

    The show is identified by its ``show_directory_uri`` (stored in
    ``tvshowlinkpath``).  A new ``tvshow`` row and a ``tvshowlinkpath`` row are
    created on first encounter.

    Mapping:
        c00 = title          c01 = plot / summary
        c02 = status         c04 = premiered
        c05 = thumb URL      c08 = genre
        c12 = studio         c13 = MPAA
        c16 = original title c19 = TVDB id
        c21 = sort title
    """
    with transaction(conn):
        idPath = _get_or_create_path(conn, show_directory_uri)

        # Check via tvshowlinkpath – this is the canonical link in Kodi 19+
        with conn.cursor() as cur:
            cur.execute(
                "SELECT idShow FROM tvshowlinkpath WHERE idPath = %s",
                (idPath,),
            )
            row = cur.fetchone()
            if row:
                return int(row["idShow"])

        # Insert new tvshow row
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tvshow
                    (c00, c01, c02, c04, c05, c08, c12, c13, c16, c19, c21,
                     userrating, duration)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     0, 0)
                """,
                (
                    nfo.title,                          # c00
                    nfo.plot,                           # c01
                    nfo.status,                         # c02
                    nfo.premiered,                      # c04
                    nfo.thumb,                          # c05
                    " / ".join(nfo.genre),              # c08
                    " / ".join(nfo.studio),             # c12
                    nfo.mpaa,                           # c13
                    nfo.originaltitle,                  # c16
                    nfo.tvdb_id,                        # c19
                    nfo.sorttitle or nfo.title,         # c21
                ),
            )
            idShow = int(cur.lastrowid)

        _link_tvshow_path(conn, idShow, idPath)
        logger.debug("Inserted tvshow idShow=%d title=%r", idShow, nfo.title)
        return idShow


def _get_episode_row_by_file_and_numbers(
    conn, idFile: int, season_num: int, episode_num: int
) -> Optional[dict]:
    """Fetch the episode row for *(idFile, season, episode)*.

    Multi-episode files share one idFile across several episode rows, so
    matching must include c12/c13 – matching by idFile alone would corrupt
    sibling rows of a multi-episode file.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT idEpisode, idShow, idSeason, c00, c01, c02, c04, c05, c06,
                   c10, c11, c12, c13, c18
            FROM episode
            WHERE idFile = %s AND c12 = %s AND c13 = %s
            """,
            (idFile, str(season_num), str(episode_num)),
        )
        return cur.fetchone()


def _upsert_single_episode(conn, idFile: int, idShow: int, nfo: EpisodeNfo) -> UpsertOutcome:
    """Insert or update one episode row for *idFile*.  Assumes an active transaction."""
    try:
        season_num = int(nfo.season)
    except (ValueError, TypeError):
        season_num = 0

    try:
        episode_num = int(nfo.episode)
    except (ValueError, TypeError):
        episode_num = 0

    idSeason = _get_or_create_season(conn, idShow, season_num)

    existing_row = _get_episode_row_by_file_and_numbers(conn, idFile, season_num, episode_num)
    if existing_row is not None:
        existing_id = int(existing_row["idEpisode"])
        if _episode_row_matches(existing_row, idShow, idSeason, season_num, episode_num, nfo):
            return UpsertOutcome(existing_id, idFile, False)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE episode
                SET idShow = %s,
                    idSeason = %s,
                    c00 = %s,
                    c01 = %s,
                    c02 = %s,
                    c04 = %s,
                    c05 = %s,
                    c06 = %s,
                    c10 = %s,
                    c11 = %s,
                    c12 = %s,
                    c13 = %s,
                    c18 = %s
                WHERE idEpisode = %s
                """,
                (
                    idShow,
                    idSeason,
                    nfo.title,
                    nfo.outline,
                    nfo.plot,
                    " / ".join(nfo.writer),
                    nfo.aired,
                    nfo.thumb,
                    str(episode_num),
                    nfo.tvdb_id,
                    str(season_num),
                    str(episode_num),
                    " / ".join(nfo.director),
                    existing_id,
                ),
            )
        logger.debug(
            "Updated episode idEpisode=%d S%sE%s title=%r",
            existing_id,
            season_num,
            episode_num,
            nfo.title,
        )
        return UpsertOutcome(existing_id, idFile, False)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episode
                (idFile, idShow, idSeason,
                 c00, c01, c02, c04, c05, c06, c10, c11, c12, c13, c18,
                 userrating)
            VALUES
                (%s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 0)
            """,
            (
                idFile, idShow, idSeason,
                nfo.title,                          # c00
                nfo.outline,                        # c01
                nfo.plot,                           # c02
                " / ".join(nfo.writer),             # c04
                nfo.aired,                          # c05
                nfo.thumb,                          # c06
                str(episode_num),                   # c10
                nfo.tvdb_id,                        # c11
                str(season_num),                    # c12
                str(episode_num),                   # c13
                " / ".join(nfo.director),           # c18
            ),
        )
        idEpisode = int(cur.lastrowid)

    logger.debug(
        "Inserted episode idEpisode=%d S%sE%s title=%r",
        idEpisode, nfo.season, nfo.episode, nfo.title,
    )
    return UpsertOutcome(idEpisode, idFile, True)


def upsert_episodes_for_file(
    conn,
    episode_directory_uri: str,
    filename: str,
    full_smb_path: str,
    idShow: int,
    nfos: list,
) -> list:
    """Upsert one or more episode rows backed by a single video file.

    Multi-episode files (``S01E01E02.mkv``) carry several ``<episodedetails>``
    NFO blocks; Kodi's convention is one ``episode`` row per block, all
    pointing at the same ``files.idFile``.

    Returns a list of :class:`UpsertOutcome`, one per NFO block.
    """
    with transaction(conn):
        idPath = _get_or_create_path(conn, episode_directory_uri)
        idFile = _get_or_create_file(conn, idPath, filename)
        outcomes = [
            _upsert_single_episode(conn, idFile, idShow, nfo)
            for nfo in nfos
        ]
    return outcomes


# ---------------------------------------------------------------------------
# Stream details (external subtitles)
# ---------------------------------------------------------------------------

def sync_subtitle_streams(conn, id_file: int, subtitles: list) -> int:
    """Add external subtitle language rows for *id_file* (merge-only).

    Kodi's ``streamdetails`` schema has no subtitle codec/forced columns, so
    only ``strSubtitleLanguage`` is stored.  The sync is deliberately
    merge-only – existing iStreamType=2 rows (e.g. embedded subtitle streams
    probed by Kodi) are never deleted, because there is no column that would
    let us distinguish our external rows from probed ones.  Kodi re-probes
    and rewrites all streamdetails on first playback anyway.

    Returns the number of rows inserted.
    """
    languages = []
    seen = set()
    for sub in subtitles:
        lang = (getattr(sub, "language", "") or "").strip().lower()
        if lang and lang not in seen:
            seen.add(lang)
            languages.append(lang)

    if not languages:
        return 0

    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT strSubtitleLanguage FROM streamdetails "
                "WHERE idFile = %s AND iStreamType = 2",
                (id_file,),
            )
            existing = {
                (row["strSubtitleLanguage"] or "").strip().lower()
                for row in cur.fetchall() or []
            }

            inserted = 0
            for lang in languages:
                if lang in existing:
                    continue
                cur.execute(
                    "INSERT INTO streamdetails (idFile, iStreamType, strSubtitleLanguage) "
                    "VALUES (%s, 2, %s)",
                    (id_file, lang),
                )
                inserted += 1
        return inserted


# ---------------------------------------------------------------------------
# Extras / trailers (Kodi Video Versions feature, MyVideos v131)
# ---------------------------------------------------------------------------

def _get_or_create_extras_type(conn, type_name: str) -> int:
    """Return the videoversiontype id for an extras type name.

    Mirrors Kodi's ``AddVideoVersionType(name, AUTO, EXTRA)``: match by
    (name, itemType=1); insert with owner=1 (AUTO) and an auto-assigned id
    when missing.  Kodi does not pre-populate extras types in the 404xx
    range, so name-based matching is the canonical behaviour.
    """
    name = (type_name or "").strip() or "Other"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM videoversiontype WHERE itemType = 1 AND LOWER(name) = LOWER(%s) "
            "ORDER BY id LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur.execute(
            "INSERT INTO videoversiontype (name, owner, itemType) VALUES (%s, 1, 1)",
            (name,),
        )
        return int(cur.lastrowid)


def find_movie_id_by_dir(conn, directory_uri: str) -> Optional[int]:
    """Return the first idMovie whose file lives directly in *directory_uri*."""
    if not directory_uri.endswith("/"):
        directory_uri += "/"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.idMovie
            FROM movie m
            JOIN files f ON f.idFile = m.idFile
            JOIN path p ON p.idPath = f.idPath
            WHERE p.strPath = %s
            ORDER BY m.idMovie
            LIMIT 1
            """,
            (directory_uri,),
        )
        row = cur.fetchone()
        return int(row["idMovie"]) if row else None


def register_movie_extra(
    conn,
    directory_uri: str,
    filename: str,
    id_movie: int,
    type_name: str,
) -> bool:
    """Register *filename* as an extra of *id_movie* (videoversion itemType=1).

    Creates path/files rows as needed and inserts the videoversion row.
    ``videoversion.idFile`` is the primary key, so a file already registered
    (as any asset) is left untouched.  Returns True when a new row was added.
    """
    with transaction(conn):
        idPath = _get_or_create_path(conn, directory_uri)
        idFile = _get_or_create_file(conn, idPath, filename)

        with conn.cursor() as cur:
            cur.execute("SELECT idMedia FROM videoversion WHERE idFile = %s", (idFile,))
            if cur.fetchone() is not None:
                return False

        idType = _get_or_create_extras_type(conn, type_name)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO videoversion (idFile, idMedia, media_type, itemType, idType) "
                "VALUES (%s, %s, 'movie', 1, %s)",
                (idFile, id_movie, idType),
            )
        logger.debug(
            "Registered extra %r (type=%r, idType=%d) for idMovie=%d",
            filename, type_name, idType, id_movie,
        )
        return True


# ---------------------------------------------------------------------------
# Deletion & reconciliation
# ---------------------------------------------------------------------------

_MEDIA_LINK_TABLES = (
    "actor_link",
    "director_link",
    "writer_link",
    "genre_link",
    "country_link",
    "studio_link",
    "tag_link",
)


def _delete_media_links(conn, media_type: str, media_id: int) -> None:
    """Delete art/rating/uniqueid and *_link rows for one media item (in-transaction)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM art WHERE media_type = %s AND media_id = %s",
            (media_type, media_id),
        )
        cur.execute(
            "DELETE FROM rating WHERE media_type = %s AND media_id = %s",
            (media_type, media_id),
        )
        cur.execute(
            "DELETE FROM uniqueid WHERE media_type = %s AND media_id = %s",
            (media_type, media_id),
        )
        for table in _MEDIA_LINK_TABLES:
            cur.execute(
                f"DELETE FROM {table} WHERE media_type = %s AND media_id = %s",
                (media_type, media_id),
            )


def _delete_movie_row(conn, id_movie: int) -> None:
    """Delete one movie row plus everything that references it (in-transaction)."""
    _delete_media_links(conn, "movie", id_movie)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM movielinktvshow WHERE idMovie = %s", (id_movie,))
        cur.execute(
            "DELETE FROM videoversion WHERE media_type = 'movie' AND idMedia = %s",
            (id_movie,),
        )
        cur.execute("DELETE FROM movie WHERE idMovie = %s", (id_movie,))


def _delete_episode_row(conn, id_episode: int) -> None:
    """Delete one episode row plus its references (in-transaction)."""
    _delete_media_links(conn, "episode", id_episode)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episode WHERE idEpisode = %s", (id_episode,))


def _prune_empty_season(conn, id_season: int) -> None:
    """Delete a seasons row (and its art) when no episodes remain (in-transaction)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idEpisode FROM episode WHERE idSeason = %s LIMIT 1",
            (id_season,),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            "DELETE FROM art WHERE media_type = 'season' AND media_id = %s",
            (id_season,),
        )
        cur.execute("DELETE FROM seasons WHERE idSeason = %s", (id_season,))


def _prune_path_if_unused(conn, id_path: int) -> None:
    """Delete a path row when nothing references it any more (in-transaction).

    Conservative: only files rows and tvshowlinkpath links are considered;
    library source paths (which have strContent set) are never removed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strContent FROM path WHERE idPath = %s",
            (id_path,),
        )
        row = cur.fetchone()
        if not row or (row.get("strContent") or ""):
            return
        cur.execute("SELECT idFile FROM files WHERE idPath = %s LIMIT 1", (id_path,))
        if cur.fetchone() is not None:
            return
        cur.execute("SELECT idShow FROM tvshowlinkpath WHERE idPath = %s LIMIT 1", (id_path,))
        if cur.fetchone() is not None:
            return
        cur.execute("DELETE FROM path WHERE idPath = %s", (id_path,))


def _delete_file_and_dependents(conn, id_file: int) -> dict:
    """Transaction-less core of :func:`delete_file_and_dependents`.

    Assumes the caller manages the transaction.
    """
    summary = {"movies": 0, "episodes": 0, "seasons_pruned": 0, "file_deleted": False}
    with conn.cursor() as cur:
        cur.execute("SELECT idPath FROM files WHERE idFile = %s", (id_file,))
        file_row = cur.fetchone()
        if not file_row:
            return summary
        id_path = int(file_row["idPath"])

        cur.execute("SELECT idMovie FROM movie WHERE idFile = %s", (id_file,))
        movie_ids = [int(r["idMovie"]) for r in cur.fetchall() or []]

        cur.execute(
            "SELECT idEpisode, idSeason FROM episode WHERE idFile = %s",
            (id_file,),
        )
        episode_rows = [
            (int(r["idEpisode"]), int(r["idSeason"])) for r in cur.fetchall() or []
        ]

    for id_movie in movie_ids:
        _delete_movie_row(conn, id_movie)
        summary["movies"] += 1

    season_ids = set()
    for id_episode, id_season in episode_rows:
        _delete_episode_row(conn, id_episode)
        season_ids.add(id_season)
        summary["episodes"] += 1

    with conn.cursor() as cur:
        for table in ("streamdetails", "bookmark", "settings", "stacktimes"):
            cur.execute(f"DELETE FROM {table} WHERE idFile = %s", (id_file,))
        cur.execute("DELETE FROM videoversion WHERE idFile = %s", (id_file,))
        cur.execute("DELETE FROM files WHERE idFile = %s", (id_file,))
    summary["file_deleted"] = True

    for id_season in season_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT idEpisode FROM episode WHERE idSeason = %s LIMIT 1",
                (id_season,),
            )
            had_episodes = cur.fetchone() is not None
        if not had_episodes:
            _prune_empty_season(conn, id_season)
            summary["seasons_pruned"] += 1

    _prune_path_if_unused(conn, id_path)

    return summary


def delete_file_and_dependents(conn, id_file: int) -> dict:
    """Delete a files row and every dependent record, in one transaction.

    Cascade order: movie/episode rows (with their art, ratings, uniqueids,
    link tables and videoversion asset rows) -> file-level rows
    (streamdetails, bookmark, settings, stacktimes, videoversion) -> files.
    Seasons left without episodes are pruned; unused path rows are pruned
    unless they are library source paths.

    Returns a small summary dict.
    """
    with transaction(conn):
        return _delete_file_and_dependents(conn, id_file)


def delete_file_by_location(conn, directory_uri: str, filename: str) -> bool:
    """Delete the files row (and dependents) for *(directory_uri, filename)*."""
    id_file = get_file_id(conn, directory_uri, filename)
    if id_file is None:
        return False
    delete_file_and_dependents(conn, id_file)
    return True


def reconcile_directory(conn, directory_uri: str, present_filenames) -> int:
    """Delete DB file rows under *directory_uri* that no longer exist on the share.

    *present_filenames* is the authoritative directory listing (video files
    only).  Callers must only invoke this after a successful SMB listing –
    never on error/timeout.  Returns the number of files purged.
    """
    if not directory_uri.endswith("/"):
        directory_uri += "/"
    present = {name.casefold() for name in present_filenames}

    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.idFile, f.strFilename
                FROM files f
                JOIN path p ON p.idPath = f.idPath
                WHERE p.strPath = %s
                """,
                (directory_uri,),
            )
            rows = cur.fetchall() or []

        stale_ids = [
            int(row["idFile"])
            for row in rows
            if (row["strFilename"] or "").casefold() not in present
        ]

        purged = 0
        for id_file in stale_ids:
            result = _delete_file_and_dependents(conn, id_file)
            if result["file_deleted"]:
                purged += 1
                logger.info(
                    "Reconciled stale file: %s (idFile=%d)", directory_uri, id_file
                )
        return purged


def delete_movie_by_dir(conn, directory_uri: str) -> int:
    """Delete every movie (and its files) located directly in *directory_uri*.

    Used for Radarr ``MovieDelete`` events.  Returns the number of movie rows
    removed.
    """
    if not directory_uri.endswith("/"):
        directory_uri += "/"
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.idFile
                FROM movie m
                JOIN files f ON f.idFile = m.idFile
                JOIN path p ON p.idPath = f.idPath
                WHERE p.strPath = %s
                """,
                (directory_uri,),
            )
            file_ids = [int(r["idFile"]) for r in cur.fetchall() or []]

        removed = 0
        for id_file in file_ids:
            result = _delete_file_and_dependents(conn, id_file)
            removed += result["movies"]
        return removed


def delete_tvshow_by_dir(conn, show_directory_uri: str) -> bool:
    """Delete a TV show, all its episodes, seasons and art, in one transaction.

    Used for Sonarr ``SeriesDelete`` events.  Episode files are located by
    path prefix (show folder and everything below it).  Returns True when a
    show row was removed.
    """
    if not show_directory_uri.endswith("/"):
        show_directory_uri += "/"

    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tlp.idShow
                FROM tvshowlinkpath tlp
                JOIN path p ON p.idPath = tlp.idPath
                WHERE p.strPath = %s
                LIMIT 1
                """,
                (show_directory_uri,),
            )
            show_row = cur.fetchone()
            if not show_row:
                return False
            id_show = int(show_row["idShow"])

            # All episode files anywhere below the show folder.
            cur.execute(
                """
                SELECT DISTINCT f.idFile
                FROM episode e
                JOIN files f ON f.idFile = e.idFile
                JOIN path p ON p.idPath = f.idPath
                WHERE e.idShow = %s AND p.strPath LIKE %s
                """,
                (id_show, show_directory_uri + "%"),
            )
            file_ids = [int(r["idFile"]) for r in cur.fetchall() or []]

        for id_file in file_ids:
            _delete_file_and_dependents(conn, id_file)

        with conn.cursor() as cur:
            # Seasons (incl. art) – episodes are gone by now.
            cur.execute("SELECT idSeason FROM seasons WHERE idShow = %s", (id_show,))
            season_ids = [int(r["idSeason"]) for r in cur.fetchall() or []]
            for id_season in season_ids:
                cur.execute(
                    "DELETE FROM art WHERE media_type = 'season' AND media_id = %s",
                    (id_season,),
                )
            cur.execute("DELETE FROM seasons WHERE idShow = %s", (id_show,))

            _delete_media_links(conn, "tvshow", id_show)
            cur.execute("DELETE FROM movielinktvshow WHERE idShow = %s", (id_show,))
            cur.execute("DELETE FROM tvshowlinkpath WHERE idShow = %s", (id_show,))
            cur.execute("DELETE FROM tvshow WHERE idShow = %s", (id_show,))

        logger.info("Deleted tvshow idShow=%d (%s)", id_show, show_directory_uri)
        return True
