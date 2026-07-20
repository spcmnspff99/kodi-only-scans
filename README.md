# kodi-only-scans

A lightweight Docker service that walks an SMB/NAS share, parses Kodi-compatible `.nfo` files, and upserts movies and TV episodes directly into a remote Kodi MariaDB database. It exposes a small FastAPI web UI for on-demand scans and scan history.

## Features

- **Scheduled scanning** via a configurable cron expression (default: daily at 04:00 America/Boise local time)
- **On-demand scans** triggered through the web dashboard or REST API
- **Kodi-compatible JSON-RPC** support for `VideoLibrary.Scan`
- **Movie & TV support** — processes both `Movies` and `TV` library paths on your share
- **NFO parsing** — reads Kodi `.nfo` sidecar files for metadata (title, year, plot, ratings, etc.)
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

## Project Structure

```
.
├── main.py          # FastAPI app, scheduler, scan engine
├── smb_walker.py    # SMB directory walker and file reader
├── nfo_parser.py    # Kodi NFO XML parser
├── db_ops.py        # MariaDB upsert helpers
├── templates/       # Jinja2 HTML templates
├── Dockerfile
├── docker-compose.yml
└── .env.example
```