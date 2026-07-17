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


def _episode_exists(conn, idFile: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT idEpisode FROM episode WHERE idFile = %s", (idFile,))
        return cur.fetchone() is not None


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
        c10 = directors      c11 = original title
        c12 = thumb URL      c13 = IMDB id
        c14 = genre          c15 = country
        c22 = full SMB path  premiered = premiered / year
    """
    with transaction(conn):
        idPath = _get_or_create_path(conn, directory_uri)
        idFile = _get_or_create_file(conn, idPath, filename)
        if _movie_exists(conn, idFile):
            return None

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO movie
                    (idFile, idPath,
                     c00, c01, c02, c03, c06, c07, c10, c11, c12, c13, c14, c15, c22,
                     userrating, premiered)
                VALUES
                    (%s, %s,
                     %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     0, %s)
                """,
                (
                    idFile, idPath,
                    nfo.title,                          # c00
                    nfo.outline,                        # c01
                    nfo.plot,                           # c02
                    nfo.tagline,                        # c03
                    " / ".join(nfo.writer),             # c06
                    nfo.year,                           # c07
                    " / ".join(nfo.director),           # c10
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
    existed.

    Mapping:
        c00 = title          c01 = outline
        c02 = plot           c04 = writer
        c05 = aired          c06 = thumb
        c09 = season number  c10 = episode number
        c11 = TVDB unique id c18 = director
    """
    with transaction(conn):
        try:
            season_num = int(nfo.season)
        except (ValueError, TypeError):
            season_num = 0

        idPath = _get_or_create_path(conn, episode_directory_uri)
        idFile = _get_or_create_file(conn, idPath, filename)
        if _episode_exists(conn, idFile):
            return None

        idSeason = _get_or_create_season(conn, idShow, season_num)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episode
                    (idFile, idShow, idSeason,
                     c00, c01, c02, c04, c05, c06, c09, c10, c11, c18,
                     userrating)
                VALUES
                    (%s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                    nfo.season,                         # c09
                    nfo.episode,                        # c10
                    nfo.tvdb_id,                        # c11
                    " / ".join(nfo.director),           # c18
                ),
            )
            idEpisode = int(cur.lastrowid)

        logger.debug(
            "Inserted episode idEpisode=%d S%sE%s title=%r",
            idEpisode, nfo.season, nfo.episode, nfo.title,
        )
        return idEpisode
