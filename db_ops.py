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
from datetime import datetime, timezone
import re
from typing import Optional

import pymysql
import pymysql.cursors

from nfo_parser import EpisodeNfo, MovieNfo, TvShowNfo

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


def _episode_exists(conn, idFile: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT idEpisode FROM episode WHERE idFile = %s", (idFile,))
        return cur.fetchone() is not None


def _get_episode_id_by_file(conn, idFile: int) -> Optional[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT idEpisode FROM episode WHERE idFile = %s", (idFile,))
        row = cur.fetchone()
        return int(row["idEpisode"]) if row else None


def _get_episode_row_by_file(conn, idFile: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT idEpisode, idShow, idSeason, c00, c01, c02, c04, c05, c06,
                   c10, c11, c12, c13, c18
            FROM episode
            WHERE idFile = %s
            """,
            (idFile,),
        )
        return cur.fetchone()


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
        name = "Specials" if season_number == 0 else f"Season {season_number}"
        cur.execute(
            "INSERT INTO seasons (idShow, season, name, userrating) VALUES (%s, %s, %s, 0)",
            (idShow, season_number, name),
        )
        return int(cur.lastrowid)


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
) -> Optional[int]:
    """Idempotently insert a movie into Kodi's database.

    Returns the new ``idMovie`` if a row was inserted, ``None`` if it already
    existed (determined by the ``idFile`` in the ``movie`` table).

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
            return None

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
            return None

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
        return idMovie


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


def upsert_episode(
    conn,
    episode_directory_uri: str,
    filename: str,
    full_smb_path: str,
    idShow: int,
    nfo: EpisodeNfo,
) -> Optional[int]:
    """Idempotently insert an episode into Kodi's database.

    Returns the new ``idEpisode`` if a row was inserted, ``None`` if it already
    existed and required no update.

    Mapping:
        c00 = title          c01 = outline
        c02 = plot           c04 = writer
        c05 = aired          c06 = thumb
        c10 = episode number  c11 = TVDB unique id
        c12 = season number   c13 = episode number
        c18 = director
    """
    with transaction(conn):
        try:
            season_num = int(nfo.season)
        except (ValueError, TypeError):
            season_num = 0

        try:
            episode_num = int(nfo.episode)
        except (ValueError, TypeError):
            episode_num = 0

        idPath = _get_or_create_path(conn, episode_directory_uri)
        idFile = _get_or_create_file(conn, idPath, filename)
        idSeason = _get_or_create_season(conn, idShow, season_num)

        existing_row = _get_episode_row_by_file(conn, idFile)
        if existing_row is not None:
            if _episode_row_matches(existing_row, idShow, idSeason, season_num, episode_num, nfo):
                return None

            existing_id = int(existing_row["idEpisode"])
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
            return None

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
        return idEpisode
