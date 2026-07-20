"""
main.py
~~~~~~~
FastAPI application with APScheduler, scan engine, and SQLite-backed history.

Endpoints
---------
    GET  /           – HTML dashboard
    POST /scan       – trigger an immediate scan (background task)
    GET  /api/history – scan history as JSON

Environment variables (see .env.example)
-----------------------------------------
    SMB_HOST, SMB_SHARE, SMB_USER, SMB_PASS, SMB_PORT
    SMB_MOVIES_PATH, SMB_TV_PATH
    DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME
    SCAN_CRON   (default: "0 4 * * *")
    HISTORY_DB  (default: "/data/scan_history.db")
"""

import asyncio
import logging
import os
import posixpath
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import smbclient

from db_ops import (
    backfill_movie_art_if_missing,
    backfill_tvshow_art_if_missing,
    dedupe_movies,
    get_connection,
    get_movies_missing_artwork,
    get_or_create_tvshow,
    get_tvshows_missing_artwork,
    rebuild_movie_sort_titles,
    upsert_episode,
    upsert_movie,
)
from nfo_parser import MovieNfo, guess_movie_from_filename, parse_episode_nfo, parse_movie_nfo, parse_tvshow_nfo
from smb_walker import (
    VideoFile,
    build_smb_dir_uri,
    build_smb_file_uri,
    build_unc,
    list_smb_files,
    list_smb_subdirs,
    read_smb_file,
    resolve_share_path,
    smb_dir_exists,
    setup_smb_session,
    walk_videos,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SMB_HOST = os.environ.get("SMB_HOST", "")
SMB_SHARE = os.environ.get("SMB_SHARE", "")
SMB_MOVIES_SHARE = os.environ.get("SMB_MOVIES_SHARE", SMB_SHARE)
SMB_TV_SHARE = os.environ.get("SMB_TV_SHARE", SMB_SHARE)
SMB_USER = os.environ.get("SMB_USER", "guest")
SMB_PASS = os.environ.get("SMB_PASS", "")
SMB_PORT = int(os.environ.get("SMB_PORT", "445"))
SMB_MOVIES_PATH = os.environ.get("SMB_MOVIES_PATH", "Movies")
SMB_TV_PATH = os.environ.get("SMB_TV_PATH", "TV")

DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "kodi")
DB_PASS = os.environ.get("DB_PASS", "")
DB_NAME = os.environ.get("DB_NAME", "MyVideos131")

SCAN_CRON = os.environ.get("SCAN_CRON", "0 4 * * *")
HISTORY_DB = os.environ.get("HISTORY_DB", "/data/scan_history.db")
HISTORY_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "7"))


def _normalize_target_path(target_path: str) -> str:
    target_path = target_path.strip().strip('"').strip("'")
    target_path = target_path.split("?", 1)[0].split("#", 1)[0]
    return target_path.replace("\\", "/")


def _now_local_iso() -> str:
    """Return current local time with offset in ISO-8601 format."""
    return datetime.now().astimezone().isoformat()


def _to_local_display(ts: str | None) -> str:
    """Format an ISO timestamp as local wall time for dashboard display."""
    if not ts:
        return ""
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    else:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _looks_like_file_path(path_value: str) -> bool:
    filename = posixpath.basename(path_value.rstrip("/"))
    return bool(filename) and bool(posixpath.splitext(filename)[1])


def _relative_path_after_share(target_path: str, share_name: str) -> Optional[str]:
    normalized = _normalize_target_path(target_path)
    parts = [part for part in normalized.split("/") if part]
    share_index = next((idx for idx, part in enumerate(parts) if part.lower() == share_name.lower()), None)
    if share_index is None:
        return None

    rel_parts = parts[share_index + 1 :]
    if rel_parts and _looks_like_file_path(rel_parts[-1]):
        rel_parts = rel_parts[:-1]
    return "/".join(rel_parts)


def _find_tv_show_root_rel(tv_share: str, scan_rel: str) -> str:
    current_rel = scan_rel.strip("/\\")
    while True:
        tvshow_nfo_rel = f"{current_rel}/tvshow.nfo" if current_rel else "tvshow.nfo"
        if read_smb_file(build_unc(SMB_HOST, tv_share, tvshow_nfo_rel)):
            return current_rel
        if not current_rel:
            return scan_rel.strip("/\\")
        current_rel = posixpath.dirname(current_rel)
        if current_rel == ".":
            current_rel = ""


def _resolve_scan_target(target_path: str) -> Optional[dict]:
    """Resolve a Sonarr/Radarr/Kodi target path to a library share and scan root."""
    if not target_path:
        return None

    for share_name, library_type in (
        (SMB_MOVIES_SHARE, "movies"),
        (SMB_TV_SHARE, "tv"),
    ):
        if not share_name:
            continue

        rel_path = _relative_path_after_share(target_path, share_name)
        if rel_path is None:
            continue

        scan_rel = rel_path.strip("/\\")
        if scan_rel and _looks_like_file_path(scan_rel):
            scan_rel = posixpath.dirname(scan_rel)
            if scan_rel == ".":
                scan_rel = ""

        if library_type == "tv":
            show_root_rel = _find_tv_show_root_rel(share_name, scan_rel)
            return {
                "library_type": library_type,
                "share": share_name,
                "scan_rel": scan_rel,
                "show_root_rel": show_root_rel,
            }

        return {
            "library_type": library_type,
            "share": share_name,
            "scan_rel": scan_rel,
        }

    return None


def _parse_smb_dir_uri(uri: str) -> Optional[tuple[str, str]]:
    """Parse smb://host/share/path/ into (share, relative_path)."""
    normalized = _normalize_target_path(uri)
    if not normalized.lower().startswith("smb://"):
        return None
    parts = [part for part in normalized.split("/") if part]
    # parts: ["smb:", "host", "share", "path", ...]
    if len(parts) < 3:
        return None
    share = parts[2]
    rel = "/".join(parts[3:]).strip("/\\")
    return share, rel


def _pick_art_filename(file_names: list[str], preferred_names: tuple[str, ...]) -> str:
    lower_map = {name.lower(): name for name in file_names}
    for preferred in preferred_names:
        matched = lower_map.get(preferred)
        if matched:
            return matched
    return ""

# ---------------------------------------------------------------------------
# Scan history (SQLite)
# ---------------------------------------------------------------------------

def _ensure_history_dir() -> None:
    db_dir = os.path.dirname(HISTORY_DB)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def init_history_db() -> None:
    _ensure_history_dir()
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at     TEXT    NOT NULL,
            finished_at    TEXT,
            status         TEXT    NOT NULL DEFAULT 'running',
            movies_added   INTEGER DEFAULT 0,
            episodes_added INTEGER DEFAULT 0,
            errors         TEXT
        )
        """
    )
    conn.commit()

    cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_history)").fetchall()}
    if "trigger_source" not in cols:
        conn.execute("ALTER TABLE scan_history ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'unknown'")
    if "trigger_target" not in cols:
        conn.execute("ALTER TABLE scan_history ADD COLUMN trigger_target TEXT NOT NULL DEFAULT ''")
    conn.commit()

    conn.close()
    _prune_history()


def _history_conn() -> sqlite3.Connection:
    return sqlite3.connect(HISTORY_DB)


def _prune_history() -> int:
    """Delete scan history rows older than HISTORY_RETENTION_DAYS.

    Set HISTORY_RETENTION_DAYS to 0 or a negative number to disable pruning.
    """
    if HISTORY_RETENTION_DAYS <= 0:
        return 0

    cutoff = datetime.now().astimezone() - timedelta(days=HISTORY_RETENTION_DAYS)
    cutoff_iso = cutoff.isoformat()

    conn = _history_conn()
    cur = conn.execute(
        "DELETE FROM scan_history WHERE started_at < ?",
        (cutoff_iso,),
    )
    conn.commit()
    deleted = int(cur.rowcount or 0)
    conn.close()

    if deleted:
        logger.info(
            "Pruned %d scan history rows older than %d days",
            deleted,
            HISTORY_RETENTION_DAYS,
        )
    return deleted


def _start_scan_record(trigger_source: str = "unknown", trigger_target: str = "") -> int:
    _prune_history()
    conn = _history_conn()
    cur = conn.execute(
        """
        INSERT INTO scan_history (started_at, status, trigger_source, trigger_target)
        VALUES (?, 'running', ?, ?)
        """,
        (_now_local_iso(), trigger_source, trigger_target),
    )
    conn.commit()
    scan_id = cur.lastrowid
    conn.close()
    return scan_id


def _finish_scan_record(
    scan_id: int,
    status: str,
    movies_added: int,
    episodes_added: int,
    errors: str = "",
) -> None:
    conn = _history_conn()
    conn.execute(
        """
        UPDATE scan_history
        SET finished_at = ?, status = ?, movies_added = ?, episodes_added = ?, errors = ?
        WHERE id = ?
        """,
        (
            _now_local_iso(),
            status,
            movies_added,
            episodes_added,
            errors,
            scan_id,
        ),
    )
    conn.commit()
    conn.close()


def get_history() -> list:
    """Return the 50 most-recent scan records, newest first."""
    conn = _history_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM scan_history ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    for row in result:
        row["started_at_display"] = _to_local_display(row.get("started_at"))
        row["finished_at_display"] = _to_local_display(row.get("finished_at"))
    return result


# ---------------------------------------------------------------------------
# Scan engine
# ---------------------------------------------------------------------------

def _legacy_run_scan() -> None:
    """
    Full media-library scan.  Runs synchronously; intended to be called from
    a background thread or the APScheduler executor.

    Flow
    ----
    1. Register SMB session.
    2. Open MariaDB connection.
    3. Walk *SMB_MOVIES_PATH*, parse every .nfo, insert into `movie` table.
    4. Walk *SMB_TV_PATH* top-level dirs (= shows), parse tvshow.nfo, then
       walk each show dir for episode files and insert into `episode` table.
    5. Persist scan summary to SQLite history.
    """
    if not SMB_HOST or not DB_HOST:
        logger.warning("SMB_HOST or DB_HOST not configured – scan skipped.")
        return

    scan_id = _start_scan_record("legacy")
    movies_added = 0
    episodes_added = 0
    errors: list = []

    try:
        # ── SMB session ────────────────────────────────────────────────────
        setup_smb_session(SMB_HOST, SMB_USER, SMB_PASS, SMB_PORT)

        if not SMB_MOVIES_SHARE:
            errors.append("SMB_MOVIES_SHARE (or SMB_SHARE) must be set")
        if not SMB_TV_SHARE:
            errors.append("SMB_TV_SHARE (or SMB_SHARE) must be set")

        movies_path = resolve_share_path(
            SMB_HOST, SMB_MOVIES_SHARE, SMB_MOVIES_PATH, library_type="movies"
        )
        tv_path = resolve_share_path(
            SMB_HOST, SMB_TV_SHARE, SMB_TV_PATH, library_type="tv"
        )

        if movies_path != SMB_MOVIES_PATH:
            logger.info("Using resolved movies path: %s (configured: %s)", movies_path, SMB_MOVIES_PATH)
        if tv_path != SMB_TV_PATH:
            logger.info("Using resolved TV path: %s (configured: %s)", tv_path, SMB_TV_PATH)

        if not smb_dir_exists(build_unc(SMB_HOST, SMB_MOVIES_SHARE, movies_path)):
            msg = f"Movies path not found on share: {movies_path}"
            logger.warning(msg)
            errors.append(msg)
        if not smb_dir_exists(build_unc(SMB_HOST, SMB_TV_SHARE, tv_path)):
            msg = f"TV path not found on share: {tv_path}"
            logger.warning(msg)
            errors.append(msg)

        # ── DB connection ──────────────────────────────────────────────────
        db_conn = get_connection(DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME)

        try:
            # ── Movies ─────────────────────────────────────────────────────
            logger.info(
                "Scanning movies: smb://%s/%s/%s", SMB_HOST, SMB_MOVIES_SHARE, movies_path
            )
            for vf in walk_videos(SMB_HOST, SMB_MOVIES_SHARE, movies_path):
                try:
                    result = _process_movie(db_conn, vf)
                    if result is not None:
                        movies_added += 1
                except Exception as exc:
                    msg = f"Movie {vf.filename}: {exc}"
                    logger.warning(msg)
                    errors.append(msg)

            # ── TV shows ───────────────────────────────────────────────────
            logger.info(
                "Scanning TV: smb://%s/%s/%s", SMB_HOST, SMB_TV_SHARE, tv_path
            )
            tv_unc = build_unc(SMB_HOST, SMB_TV_SHARE, tv_path)
            for show_name in list_smb_subdirs(tv_unc):
                show_rel = f"{tv_path.rstrip('/')}/{show_name}"
                show_uri = build_smb_dir_uri(SMB_HOST, SMB_TV_SHARE, show_rel)
                ep_count = _process_tvshow(db_conn, SMB_TV_SHARE, show_name, show_rel, show_uri, errors)
                episodes_added += ep_count

        finally:
            db_conn.close()

    except Exception as exc:
        msg = f"Fatal scan error: {exc}\n{traceback.format_exc()}"
        logger.error(msg)
        errors.append(msg)
        _finish_scan_record(scan_id, "failed", movies_added, episodes_added, "\n".join(errors))
        return

    status = "completed" if not errors else "completed_with_errors"
    _finish_scan_record(scan_id, status, movies_added, episodes_added, "\n".join(errors))
    logger.info(
        "Scan finished – %d movies, %d episodes added. Status: %s",
        movies_added, episodes_added, status,
    )


def run_scan(target_path: str | None = None, trigger_source: str = "unknown") -> None:
    """Full scan by default, or a targeted scan when *target_path* is provided."""
    if not SMB_HOST or not DB_HOST:
        logger.warning("SMB_HOST or DB_HOST not configured – scan skipped.")
        return

    scan_id = _start_scan_record(trigger_source, target_path or "")
    movies_added = 0
    episodes_added = 0
    errors: list = []

    try:
        setup_smb_session(SMB_HOST, SMB_USER, SMB_PASS, SMB_PORT)

        if not SMB_MOVIES_SHARE:
            errors.append("SMB_MOVIES_SHARE (or SMB_SHARE) must be set")
        if not SMB_TV_SHARE:
            errors.append("SMB_TV_SHARE (or SMB_SHARE) must be set")

        target = _resolve_scan_target(target_path) if target_path else None

        db_conn = get_connection(DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME)
        try:
            if target is None:
                movies_path = resolve_share_path(
                    SMB_HOST, SMB_MOVIES_SHARE, SMB_MOVIES_PATH, library_type="movies"
                )
                tv_path = resolve_share_path(
                    SMB_HOST, SMB_TV_SHARE, SMB_TV_PATH, library_type="tv"
                )

                if movies_path != SMB_MOVIES_PATH:
                    logger.info("Using resolved movies path: %s (configured: %s)", movies_path, SMB_MOVIES_PATH)
                if tv_path != SMB_TV_PATH:
                    logger.info("Using resolved TV path: %s (configured: %s)", tv_path, SMB_TV_PATH)

                if not smb_dir_exists(build_unc(SMB_HOST, SMB_MOVIES_SHARE, movies_path)):
                    msg = f"Movies path not found on share: {movies_path}"
                    logger.warning(msg)
                    errors.append(msg)
                if not smb_dir_exists(build_unc(SMB_HOST, SMB_TV_SHARE, tv_path)):
                    msg = f"TV path not found on share: {tv_path}"
                    logger.warning(msg)
                    errors.append(msg)

                movies_added += _scan_movies_tree(db_conn, SMB_MOVIES_SHARE, movies_path, errors)
                episodes_added += _scan_tv_library(db_conn, SMB_TV_SHARE, tv_path, errors)
            else:
                logger.info("Targeted scan requested: %s", target_path)
                if target["library_type"] == "movies":
                    scan_rel = resolve_share_path(
                        SMB_HOST, target["share"], target["scan_rel"], library_type="movies"
                    )
                    target_unc = build_unc(SMB_HOST, target["share"], scan_rel)
                    if smb_dir_exists(target_unc):
                        movies_added += _scan_movies_tree(db_conn, target["share"], scan_rel, errors)
                    else:
                        logger.warning(
                            "Target movie path not found: %s. Falling back to full movies root scan.",
                            scan_rel,
                        )
                        root_movies_path = resolve_share_path(
                            SMB_HOST,
                            target["share"],
                            SMB_MOVIES_PATH,
                            library_type="movies",
                        )
                        movies_added += _scan_movies_tree(db_conn, target["share"], root_movies_path, errors)
                else:
                    scan_rel = resolve_share_path(
                        SMB_HOST, target["share"], target["scan_rel"], library_type="tv"
                    )
                    show_root_rel = target["show_root_rel"]
                    show_name = posixpath.basename(show_root_rel.rstrip("/\\")) if show_root_rel else ""
                    episodes_added += _process_tvshow(
                        db_conn,
                        target["share"],
                        show_name,
                        show_root_rel,
                        scan_rel,
                        build_smb_dir_uri(SMB_HOST, target["share"], show_root_rel),
                        errors,
                    )

        finally:
            db_conn.close()

    except Exception as exc:
        msg = f"Fatal scan error: {exc}\n{traceback.format_exc()}"
        logger.error(msg)
        errors.append(msg)
        _finish_scan_record(scan_id, "failed", movies_added, episodes_added, "\n".join(errors))
        return

    status = "completed" if not errors else "completed_with_errors"
    _finish_scan_record(scan_id, status, movies_added, episodes_added, "\n".join(errors))
    logger.info(
        "Scan finished – %d movies, %d episodes added. Status: %s",
        movies_added, episodes_added, status,
    )


async def _extract_target_path(request: Request) -> Optional[str]:
    """Extract a target path from query params or a JSON body."""
    for key in ("path", "directory", "file", "target"):
        value = request.query_params.get(key)
        if value:
            return value

    try:
        payload = await request.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    # Radarr/Sonarr payloads often nest the useful path under movie/movieFile/series.
    candidate_keys = (
        "path",
        "directory",
        "file",
        "target",
        "folderPath",
        "folder",
        "relativePath",
    )

    def _find_path(value) -> Optional[str]:
        if isinstance(value, dict):
            for key in candidate_keys:
                maybe = value.get(key)
                if isinstance(maybe, str) and maybe.strip():
                    return maybe
            for nested in value.values():
                found = _find_path(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = _find_path(nested)
                if found:
                    return found
        return None

    return _find_path(payload)


def _normalize_trigger_source(request: Request) -> str:
    source = (request.query_params.get("source") or "").strip().lower()
    if source:
        return source

    user_agent = (request.headers.get("user-agent") or "").lower()
    if "radarr" in user_agent:
        return "radarr"
    if "sonarr" in user_agent:
        return "sonarr"
    if "kodi" in user_agent:
        return "kodi"
    return "api_scan"


def _queue_scan(background_tasks: BackgroundTasks, target_path: str | None, trigger_source: str) -> dict:
    background_tasks.add_task(run_scan, target_path, trigger_source)
    payload = {"status": "scan triggered", "trigger_source": trigger_source}
    if target_path:
        payload["target_path"] = target_path
    return payload


def _scan_movies_tree(db_conn, movies_share: str, movies_path: str, errors: list) -> int:
    logger.info("Scanning movies: smb://%s/%s/%s", SMB_HOST, movies_share, movies_path)
    movies_added = 0
    for vf in walk_videos(SMB_HOST, movies_share, movies_path):
        try:
            result = _process_movie(db_conn, vf)
            if result is not None:
                movies_added += 1
        except Exception as exc:
            msg = f"Movie {vf.filename}: {exc}"
            logger.warning(msg)
            errors.append(msg)
    return movies_added


def _scan_tv_library(db_conn, tv_share: str, tv_path: str, errors: list) -> int:
    logger.info("Scanning TV: smb://%s/%s/%s", SMB_HOST, tv_share, tv_path)
    episodes_added = 0
    tv_unc = build_unc(SMB_HOST, tv_share, tv_path)
    for show_name in list_smb_subdirs(tv_unc):
        show_rel = f"{tv_path.rstrip('/')}/{show_name}"
        show_uri = build_smb_dir_uri(SMB_HOST, tv_share, show_rel)
        ep_count = _process_tvshow(db_conn, tv_share, show_name, show_rel, show_rel, show_uri, errors)
        episodes_added += ep_count
    return episodes_added


def _process_movie(db_conn, vf: VideoFile):
    """Parse .nfo and upsert movie.  Returns idMovie or None."""
    nfo = None

    if vf.nfo_unc:
        content = read_smb_file(vf.nfo_unc)
        if content:
            nfo = parse_movie_nfo(content)

    if not nfo or not nfo.title:
        guessed = guess_movie_from_filename(vf.filename) or guess_movie_from_filename(vf.directory_uri.rstrip("/").rsplit("/", 1)[-1])
        if not guessed:
            return None
        nfo = MovieNfo(title=guessed.title, year=guessed.year)

    if vf.poster_unc:
        parts = vf.directory_uri.rstrip("/").split("/")
        share_name = parts[3] if len(parts) > 3 else SMB_MOVIES_SHARE
        directory_name = parts[-1]
        nfo.thumb = build_smb_file_uri(SMB_HOST, share_name, f"{directory_name}/poster.jpg")

    if vf.fanart_unc:
        parts = vf.directory_uri.rstrip("/").split("/")
        share_name = parts[3] if len(parts) > 3 else SMB_MOVIES_SHARE
        directory_name = parts[-1]
        fanart_name = vf.fanart_unc.replace("\\", "/").rsplit("/", 1)[-1]
        nfo.fanart = build_smb_file_uri(SMB_HOST, share_name, f"{directory_name}/{fanart_name}")

    result = upsert_movie(db_conn, vf.directory_uri, vf.filename, vf.smb_uri, nfo)
    if result is not None:
        logger.info("Added movie: %r", nfo.title)
    return result


def _process_tvshow(
    db_conn,
    tv_share: str,
    show_name: str,
    show_root_rel: str,
    scan_rel: str,
    show_uri: str,
    errors: list,
) -> int:
    """Process a single TV show directory.  Returns number of new episodes."""
    # Read tvshow.nfo
    tvshow_nfo_unc = build_unc(SMB_HOST, tv_share, f"{show_root_rel}/tvshow.nfo")
    content = read_smb_file(tvshow_nfo_unc)
    if not content:
        logger.debug("No tvshow.nfo in %s – skipping", show_root_rel)
        return 0

    show_nfo = parse_tvshow_nfo(content)
    if not show_nfo or not show_nfo.title:
        logger.debug("Invalid tvshow.nfo in %s – skipping", show_root_rel)
        return 0

    try:
        idShow = get_or_create_tvshow(db_conn, show_uri, show_nfo)
    except Exception as exc:
        msg = f"TV show {show_name}: {exc}"
        logger.warning(msg)
        errors.append(msg)
        return 0

    logger.info("TV show: %r (idShow=%d)", show_nfo.title, idShow)

    episodes_added = 0
    for vf in walk_videos(SMB_HOST, tv_share, scan_rel):
        try:
            if not vf.nfo_unc:
                continue
            ep_content = read_smb_file(vf.nfo_unc)
            if not ep_content:
                continue
            ep_nfo = parse_episode_nfo(ep_content)
            if not ep_nfo or not ep_nfo.title:
                continue
            result = upsert_episode(
                db_conn, vf.directory_uri, vf.filename, vf.smb_uri, idShow, ep_nfo
            )
            if result is not None:
                episodes_added += 1
                logger.info(
                    "Added episode: %r S%sE%s", ep_nfo.title, ep_nfo.season, ep_nfo.episode
                )
        except Exception as exc:
            msg = f"Episode {vf.filename}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    return episodes_added


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_history_db()
    _schedule_scan()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


def _schedule_scan() -> None:
    """Register the cron-triggered scan job if SCAN_CRON is valid."""
    parts = SCAN_CRON.split()
    if len(parts) != 5:
        logger.warning("SCAN_CRON=%r is not a 5-field cron expression – scheduler disabled.", SCAN_CRON)
        return
    trigger = CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
    )
    scheduler.add_job(_async_scan, trigger, id="scheduled_scan", replace_existing=True)
    logger.info("Scheduled scan with cron: %s", SCAN_CRON)


async def _async_scan() -> None:
    """Async wrapper: run the blocking scan in a thread-pool executor."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_scan, None, "scheduled")


def run_movie_dedupe_maintenance() -> int:
    """Run movie dedupe as an explicit dashboard maintenance task."""
    if not DB_HOST:
        raise RuntimeError("DB_HOST not configured")

    db_conn = get_connection(DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME)
    try:
        removed = dedupe_movies(db_conn)
        logger.info("Maintenance dedupe complete: removed=%d", removed)
        return removed
    finally:
        db_conn.close()


def run_movie_sort_title_rebuild_maintenance() -> dict:
    """Rebuild movie sort titles as an explicit dashboard maintenance task."""
    if not DB_HOST:
        raise RuntimeError("DB_HOST not configured")

    db_conn = get_connection(DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME)
    try:
        result = rebuild_movie_sort_titles(db_conn)
        logger.info(
            "Maintenance sort-title rebuild complete: updated=%d total=%d",
            result.get("updated", 0),
            result.get("total", 0),
        )
        return result
    finally:
        db_conn.close()


def run_missing_artwork_backfill_maintenance() -> dict:
    """Backfill missing poster/fanart for movies and tvshows from local sidecars/NFO."""
    if not DB_HOST:
        raise RuntimeError("DB_HOST not configured")

    db_conn = get_connection(DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME)
    summary = {
        "movies_examined": 0,
        "movies_updated": 0,
        "tvshows_examined": 0,
        "tvshows_updated": 0,
        "skipped": 0,
    }
    try:
        movie_rows = get_movies_missing_artwork(db_conn)
        for row in movie_rows:
            summary["movies_examined"] += 1
            parsed = _parse_smb_dir_uri(row.get("strPath") or "")
            if not parsed:
                summary["skipped"] += 1
                continue

            share, rel = parsed
            scan_rel = resolve_share_path(SMB_HOST, share, rel, library_type="movies")
            if not smb_dir_exists(build_unc(SMB_HOST, share, scan_rel)):
                summary["skipped"] += 1
                continue

            for vf in walk_videos(SMB_HOST, share, scan_rel):
                if vf.directory_uri.rstrip("/") != build_smb_dir_uri(SMB_HOST, share, scan_rel).rstrip("/"):
                    continue
                poster_url = ""
                fanart_url = ""
                if vf.poster_unc:
                    art_name = vf.poster_unc.replace("\\", "/").rsplit("/", 1)[-1]
                    poster_url = build_smb_file_uri(SMB_HOST, share, f"{scan_rel}/{art_name}".strip("/"))
                if vf.fanart_unc:
                    art_name = vf.fanart_unc.replace("\\", "/").rsplit("/", 1)[-1]
                    fanart_url = build_smb_file_uri(SMB_HOST, share, f"{scan_rel}/{art_name}".strip("/"))
                if backfill_movie_art_if_missing(db_conn, vf.directory_uri, vf.filename, poster_url, fanart_url):
                    summary["movies_updated"] += 1
                break

        tv_rows = get_tvshows_missing_artwork(db_conn)
        for row in tv_rows:
            summary["tvshows_examined"] += 1
            parsed = _parse_smb_dir_uri(row.get("strPath") or "")
            if not parsed:
                summary["skipped"] += 1
                continue

            share, rel = parsed
            scan_rel = resolve_share_path(SMB_HOST, share, rel, library_type="tv")
            show_uri = build_smb_dir_uri(SMB_HOST, share, scan_rel)
            show_unc = build_unc(SMB_HOST, share, scan_rel)
            if not smb_dir_exists(show_unc):
                summary["skipped"] += 1
                continue

            file_names = list_smb_files(show_unc)
            poster_name = _pick_art_filename(file_names, ("poster.jpg", "poster.jpeg", "folder.jpg", "folder.jpeg"))
            fanart_name = _pick_art_filename(file_names, ("fanart.jpg", "fanart.jpeg", "backdrop.jpg", "backdrop.jpeg"))
            poster_url = build_smb_file_uri(SMB_HOST, share, f"{scan_rel}/{poster_name}".strip("/")) if poster_name else ""
            fanart_url = build_smb_file_uri(SMB_HOST, share, f"{scan_rel}/{fanart_name}".strip("/")) if fanart_name else ""

            if not poster_url or not fanart_url:
                tvshow_nfo_unc = build_unc(SMB_HOST, share, f"{scan_rel}/tvshow.nfo")
                content = read_smb_file(tvshow_nfo_unc)
                if content:
                    show_nfo = parse_tvshow_nfo(content)
                    if show_nfo:
                        if not poster_url:
                            poster_url = show_nfo.thumb
                        if not fanart_url:
                            fanart_url = show_nfo.fanart

            if backfill_tvshow_art_if_missing(db_conn, show_uri, poster_url, fanart_url):
                summary["tvshows_updated"] += 1

        logger.info(
            "Missing artwork backfill complete: movies %d/%d, tvshows %d/%d, skipped=%d",
            summary["movies_updated"],
            summary["movies_examined"],
            summary["tvshows_updated"],
            summary["tvshows_examined"],
            summary["skipped"],
        )
        return summary
    finally:
        db_conn.close()


app = FastAPI(title="Kodi Only Scans", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    history = get_history()
    return templates.TemplateResponse(
        "index.html", {"request": request, "history": history}
    )


@app.post("/scan")
@app.get("/scan")
async def trigger_scan(request: Request, background_tasks: BackgroundTasks):
    """Kick off an immediate scan as a FastAPI background task."""
    target_path = await _extract_target_path(request)
    trigger_source = _normalize_trigger_source(request)
    if trigger_source == "dashboard":
        trigger_source = "dashboard"

    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Incoming /scan trigger: method=%s source=%s client=%s target=%r",
        request.method,
        trigger_source,
        client_host,
        target_path,
    )

    payload = _queue_scan(background_tasks, target_path, trigger_source)
    return JSONResponse(payload, status_code=202)


@app.post("/dashboard/maintenance/dedupe")
async def trigger_movie_dedupe(background_tasks: BackgroundTasks):
    """Dashboard-only trigger for movie dedupe maintenance."""
    background_tasks.add_task(run_movie_dedupe_maintenance)
    return JSONResponse({"status": "movie dedupe triggered"}, status_code=202)


@app.post("/dashboard/maintenance/rebuild-sort-titles")
async def trigger_movie_sort_title_rebuild(background_tasks: BackgroundTasks):
    """Dashboard-only trigger for movie sort-title rebuild maintenance."""
    background_tasks.add_task(run_movie_sort_title_rebuild_maintenance)
    return JSONResponse({"status": "movie sort-title rebuild triggered"}, status_code=202)


@app.post("/dashboard/maintenance/backfill-missing-artwork")
async def trigger_missing_artwork_backfill(background_tasks: BackgroundTasks):
    """Dashboard-only trigger for missing poster/fanart backfill."""
    background_tasks.add_task(run_missing_artwork_backfill_maintenance)
    return JSONResponse({"status": "missing artwork backfill triggered"}, status_code=202)


@app.post("/jsonrpc")
async def kodi_jsonrpc(request: Request, background_tasks: BackgroundTasks):
    """Kodi-compatible JSON-RPC surface for VideoLibrary.Scan."""
    payload = await request.json()
    method = payload.get("method")
    request_id = payload.get("id")

    if method != "VideoLibrary.Scan":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}},
            status_code=200,
        )

    params = payload.get("params") or {}
    target_path = params.get("directory") or params.get("path") or params.get("file") or params.get("target")
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Incoming /jsonrpc VideoLibrary.Scan: client=%s target=%r",
        client_host,
        target_path,
    )
    background_tasks.add_task(run_scan, target_path, "jsonrpc")
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": "OK"}, status_code=200)


@app.get("/api/history")
async def api_history():
    return {"history": get_history()}


# ---------------------------------------------------------------------------
# Entry point (for direct execution)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
