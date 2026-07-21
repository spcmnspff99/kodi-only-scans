# kodi-only-scans

A lightweight Docker service that walks an SMB/NAS share, parses Kodi-compatible `.nfo` files, and upserts movies and TV episodes directly into a remote Kodi MariaDB database. It exposes a small FastAPI web UI for on-demand scans and scan history.

## Features

- **Scheduled scanning** via a configurable cron expression (default: daily at 04:00 America/Boise local time)
- **On-demand scans** triggered through the web dashboard or REST API
- **Kodi-compatible JSON-RPC** support for `VideoLibrary.Scan`
- **Movie & TV support** — processes both `Movies` and `TV` library paths on your share
- **NFO parsing** — reads Kodi `.nfo` sidecar files for metadata (title, year, plot, ratings, etc.)
- **External subtitles** — detects `.srt/.ass/.ssa/.vtt/.idx/.sub` sidecars (incl. `.forced`/`.sdh`/region tags and VobSub pairs) and syncs `streamdetails` subtitle language rows
- **Extended artwork** — picks up clearlogo, banner, landscape, clearart, characterart, discart, season posters/fanart (`seasonXX-*`, `season-all-*`, `season-specials-*`) and episode thumbs into Kodi's `art` table
- **Multi-episode files** — `S01E01E02.mkv` with multiple `<episodedetails>` NFO blocks creates one `episode` row per block, all sharing one `idFile`
- **Trailers & extras** — `*-trailer.*` files and `extras/`/`featurettes/`/`deleted scenes/` etc. folders are registered in Kodi's `videoversion` extras (never as movies); `*-sample.*` files are ignored
- **Sonarr/Radarr webhooks** — handles `Download` (rescan + upgrade reconciliation), `MovieFileDelete`, `EpisodeFileDelete`, `MovieDelete`, `SeriesDelete`, and `Test` events
- **Upgrade/deletion reconciliation** — per-directory compare against the share; stale `files` rows are cascade-deleted (art, streamdetails, ratings, links) safely
- **Scan history** — persists scan results (movies added, episodes added, errors) to a local SQLite database
- **Docker-first** — single image, minimal dependencies

## Prerequisites

- Docker & Docker Compose
- A NAS/SMB share containing Kodi-formatted media (with `.nfo` sidecar files)
- A running Kodi MariaDB instance (e.g. `MyVideos131` database)

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```ini
# SMB / NAS settings
SMB_HOST=192.168.1.10       # IP or hostname of your NAS
SMB_SHARE=media             # Share name
SMB_USER=kodiuser
SMB_PASS=s3cret
SMB_PORT=445

# Paths within the share (no leading slash required)
SMB_MOVIES_PATH=Movies
SMB_TV_PATH=TV

# MariaDB / Kodi database
DB_HOST=192.168.1.20
DB_PORT=3306
DB_USER=kodi
DB_PASS=kodipass
DB_NAME=MyVideos131         # Match your Kodi DB version (e.g. MyVideos131)

# Scheduler – standard 5-field cron (minute hour dom month dow)
SCAN_CRON=0 4 * * *         # Default: daily at 04:00 America/Boise local time

# Path inside the container for the SQLite history DB
HISTORY_DB=/data/scan_history.db

# Keep only the last N days of scan history (0 disables pruning)
HISTORY_RETENTION_DAYS=7
```

## Deployment

```bash
# 1. Copy and edit the environment file
cp .env.example .env

# 2. Build and start the container
docker compose up -d

# 3. Tail logs
docker compose logs -f kodi-scanner
```

The web dashboard will be available at `http://<host>:8080`.

## API

| Method | Path            | Description                                      |
|--------|-----------------|--------------------------------------------------|
| `GET`  | `/`             | HTML dashboard showing scan history              |
| `POST` | `/scan`         | Trigger a scan; optionally pass `path`/`directory`|
| `POST` | `/jsonrpc`      | Kodi-compatible `VideoLibrary.Scan` endpoint     |
| `GET`  | `/api/history`  | Scan history as JSON (last 50 runs)              |

`/scan` and `/jsonrpc` both accept a target path. If a file path is provided, the scanner automatically scans the containing folder instead of only the file path itself.

### Sonarr/Radarr webhook handling

Point a Radarr/Sonarr "Webhook" connection at `POST /scan`. The service reads the payload's `eventType`:

| eventType | Action |
|-----------|--------|
| `Test` | Acknowledged, no work queued |
| `Download` / `Grab` / `Rename` / others | Targeted scan of the affected folder; missing files are reconciled out of the DB |
| `MovieFileDelete` / `EpisodeFileDelete` | The `files` row and all dependents (`movie`/`episode`, `art`, `streamdetails`, ratings, link tables) are cascade-deleted |
| `MovieDelete` | All movies under the folder are removed |
| `SeriesDelete` | The show, its seasons, episodes and art are removed |

### Sidecar conventions

- **Subtitles:** `<video>.<lang>.srt` (also `.ass/.ssa/.vtt`), with optional `.forced`/`.sdh` markers (e.g. `movie.en.forced.srt`); `.idx/.sub` VobSub pairs count as one stream. Files without a language token are skipped.
- **Artwork:** `poster.jpg`, `fanart.jpg`, `banner.jpg`, `landscape.jpg`, `logo.png`/`clearlogo.png`, `clearart.png`, `characterart.png`, `disc.png`/`discart.png`, `<video>-<type>.<ext>` stem-prefixed variants, `seasonXX-poster.jpg`, `season-all-poster.jpg`, `season-specials-poster.jpg`, and episode `<video>-thumb.jpg` / `<video>.tbn`.
- **Trailers/extras:** `<video>-trailer.<ext>` and videos inside `extras/`, `behind the scenes/`, `deleted scenes/`, `featurettes/`, `interviews/`, `scenes/`, `shorts/`, `trailers/` folders become Kodi (v21) Extras of the parent movie via the `videoversion` table.
- **Samples:** `*-sample.*` files are always ignored.

## Project Structure

```
.
├── main.py          # FastAPI app, scheduler, scan engine, webhook routing
├── smb_walker.py    # SMB directory walker and file reader
├── sidecars.py      # Pure filename parsing: subtitles, art, trailers, extras
├── nfo_parser.py    # Kodi NFO XML parser (incl. multi-episode blocks)
├── db_ops.py        # MariaDB upserts, streamdetails, art, deletes, reconcile
├── test_sidecars.py # Unit tests for sidecar parsing
├── test_nfo_parser.py
├── templates/       # Jinja2 HTML templates
├── Dockerfile
├── docker-compose.yml
└── .env.example
```