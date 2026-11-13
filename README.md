# Drift

Drift is a browser-based public-media downloader. A Python HTTP server serves the embedded frontend and exposes a small JSON API. It uses yt-dlp to inspect and retrieve publicly accessible media, SQLite to track jobs, and FFmpeg to prepare compatible MP4 files.

> **Responsible use:** Only download content you own or are authorized to save. Follow copyright law and each source platform's terms. Drift is not designed to bypass DRM, private accounts, paywalls, or other access controls.

## Features

- Embedded responsive HTML/CSS/JavaScript frontend.
- URL validation with supported-host allowlisting and DNS checks.
- Media analysis and quality selection.
- Background download jobs with progress and cancellation.
- SQLite job history and temporary file cleanup.
- FFmpeg conversion to broadly compatible MP4 when needed.
- Health endpoint at `/api/health`.

Supported source domains are defined in `KNOWN_HOSTS` in `drift.py`. A domain being listed does not guarantee every post or format is downloadable; platforms can change their behavior at any time.

## Requirements

- Python 3.10 or newer.
- Dependencies in `requirements.txt`.
- FFmpeg on PATH, or the bundled executable supplied by `imageio-ffmpeg`.

## Windows setup

Open PowerShell in the repository folder:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python drift.py
```

Open http://127.0.0.1:5000. Stop the server with Ctrl+C.

The application does not install or upgrade packages at startup. Install dependencies explicitly inside the virtual environment before running it.

## macOS and Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python drift.py
```

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `PORT` / `DRIFT_PORT` | `5000` | HTTP port |
| `DRIFT_HOST` | `127.0.0.1` locally; `0.0.0.0` when `PORT` is set | Bind address |
| `DRIFT_MAX_DOWNLOAD_MB` | `500` | Download size limit |
| `DRIFT_JOB_TTL_SECONDS` | `1800` | Finished job retention |
| `DRIFT_MAX_ACTIVE_JOBS` | `2` | Background worker count |
| `DRIFT_DOWNLOAD_ROOT` | System temporary directory | Download and database directory |
| `DRIFT_DB_PATH` | `<download root>/drift.sqlite3` | SQLite database |
| `DRIFT_ALLOWED_ORIGIN` | `*` | CORS origin for a separately hosted frontend |
| `DRIFT_COOKIES_FILE` | `cookies.txt` beside the script if present | Optional yt-dlp cookie file |
| `DRIFT_COOKIES_BROWSER` | unset | Optional browser cookie source supported by yt-dlp |

Never commit cookies, credentials, or private tokens. The `.gitignore` excludes `cookies.txt`.

## API health check

```text
GET /api/health
```

The endpoint reports downloader/FFmpeg availability and the current job queue size.

## Security and deployment warning

The current app does **not** provide user accounts, login, or API-key authentication. Its in-memory per-client rate limiter is only a basic abuse guard; it is not a security boundary and is not shared across multiple server processes. URL validation allowlists known media domains and rejects non-public DNS results, but DNS can change between validation and the downloader's later connection.

Keep the server on localhost for personal use. Before public deployment, put it behind a properly configured authentication/access proxy, add production-grade rate limiting and monitoring, use persistent storage, and review the hosting provider's network controls. Do not assume CORS is authentication.

## Tests

```bash
python -m unittest discover -s tests -v
```

The unit tests cover URL validation, DNS rejection, quality selection, filename cleanup, error messages, and rate limiting. They do not prove that every third-party platform currently works. Real downloads must be tested manually with media you are authorized to download.

## Project layout

- `drift.py` — backend server and embedded frontend.
- `requirements.txt` — Python dependencies.
- `tests/test_core.py` — helper and validation tests.

## License

No license file is currently present. Reuse and redistribution permissions are therefore not explicitly granted.
