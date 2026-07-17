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
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import smbclient

from db_ops import get_connection, get_or_create_tvshow, upsert_episode, upsert_movie
from nfo_parser import parse_episode_nfo, parse_movie_nfo, parse_tvshow_nfo
from smb_walker import (
    VideoFile,
    build_smb_dir_uri,
    build_unc,
    list_smb_subdirs,
    read_smb_file,
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
    conn.close()


def _history_conn() -> sqlite3.Connection:
    return sqlite3.connect(HISTORY_DB)


def _start_scan_record() -> int:
    conn = _history_conn()
    cur = conn.execute(
        "INSERT INTO scan_history (started_at, status) VALUES (?, 'running')",
        (datetime.now(timezone.utc).isoformat(),),
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
            datetime.now(timezone.utc).isoformat(),
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
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Scan engine
# ---------------------------------------------------------------------------

def run_scan() -> None:
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

    scan_id = _start_scan_record()
    movies_added = 0
    episodes_added = 0
    errors: list = []

    try:
        # ── SMB session ────────────────────────────────────────────────────
        setup_smb_session(SMB_HOST, SMB_USER, SMB_PASS, SMB_PORT)

        # ── DB connection ──────────────────────────────────────────────────
        db_conn = get_connection(DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME)

        try:
            # ── Movies ─────────────────────────────────────────────────────
            logger.info(
                "Scanning movies: smb://%s/%s/%s", SMB_HOST, SMB_SHARE, SMB_MOVIES_PATH
            )
            for vf in walk_videos(SMB_HOST, SMB_SHARE, SMB_MOVIES_PATH):
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
                "Scanning TV: smb://%s/%s/%s", SMB_HOST, SMB_SHARE, SMB_TV_PATH
            )
            tv_unc = build_unc(SMB_HOST, SMB_SHARE, SMB_TV_PATH)
            for show_name in list_smb_subdirs(tv_unc):
                show_rel = f"{SMB_TV_PATH.rstrip('/')}/{show_name}"
                show_uri = build_smb_dir_uri(SMB_HOST, SMB_SHARE, show_rel)
                ep_count = _process_tvshow(db_conn, show_name, show_rel, show_uri, errors)
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


def _process_movie(db_conn, vf: VideoFile):
    """Parse .nfo and upsert movie.  Returns idMovie or None."""
    if not vf.nfo_unc:
        return None
    content = read_smb_file(vf.nfo_unc)
    if not content:
        return None
    nfo = parse_movie_nfo(content)
    if not nfo or not nfo.title:
        return None
    result = upsert_movie(db_conn, vf.directory_uri, vf.filename, vf.smb_uri, nfo)
    if result is not None:
        logger.info("Added movie: %r", nfo.title)
    return result


def _process_tvshow(
    db_conn,
    show_name: str,
    show_rel: str,
    show_uri: str,
    errors: list,
) -> int:
    """Process a single TV show directory.  Returns number of new episodes."""
    # Read tvshow.nfo
    tvshow_nfo_unc = build_unc(SMB_HOST, SMB_SHARE, f"{show_rel}/tvshow.nfo")
    content = read_smb_file(tvshow_nfo_unc)
    if not content:
        logger.debug("No tvshow.nfo in %s – skipping", show_rel)
        return 0

    show_nfo = parse_tvshow_nfo(content)
    if not show_nfo or not show_nfo.title:
        logger.debug("Invalid tvshow.nfo in %s – skipping", show_rel)
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
    for vf in walk_videos(SMB_HOST, SMB_SHARE, show_rel):
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
    await loop.run_in_executor(None, run_scan)


app = FastAPI(title="Kodi Only Scans", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    history = get_history()
    return templates.TemplateResponse(
        "index.html", {"request": request, "history": history}
    )


@app.post("/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Kick off an immediate scan as a FastAPI background task."""
    background_tasks.add_task(run_scan)
    return JSONResponse({"status": "scan triggered"}, status_code=202)


@app.get("/api/history")
async def api_history():
    return {"history": get_history()}


# ---------------------------------------------------------------------------
# Entry point (for direct execution)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
