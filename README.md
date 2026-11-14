# Drift

Drift is a browser-based downloader for publicly accessible media. It provides a responsive HTML/CSS/JavaScript interface and a Python HTTP backend powered by yt-dlp, SQLite, and FFmpeg.

> **Responsible use:** Only download content you own or are authorized to save. Follow copyright law and each source platform's terms. Drift is not designed to bypass DRM, private accounts, paywalls, or other access controls.

## Features

- Responsive frontend in `public/index.html`, `public/css/style.css`, and `public/js/app.js`.
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

## Run locally (Windows)

Open PowerShell in the repository folder:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python drift.py
```

Open http://127.0.0.1:5000. Stop the server with Ctrl+C.

## Run locally (macOS and Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python drift.py
```

The application does not install or upgrade packages at startup. Install dependencies explicitly inside the virtual environment before running it.

## Configuration

| Environment variable | Default | Purpose |
|---|---:|---|
| `PORT` / `DRIFT_PORT` | `5000` | HTTP port |
| `DRIFT_HOST` | `127.0.0.1` locally; `0.0.0.0` when `PORT` is set | Bind address |
| `DRIFT_MAX_DOWNLOAD_MB` | `500` | Maximum downloaded file size and per-job storage reservation |
| `DRIFT_MAX_STORAGE_MB` | `2048` | Maximum reserved active-download plus retained completed-file storage |
| `DRIFT_JOB_TTL_SECONDS` | `1800` | Finished job retention |
| `DRIFT_MAX_ACTIVE_JOBS` | `2` | Background worker count |
| `DRIFT_MAX_QUEUED_JOBS` | `4` | Maximum waiting jobs |
| `DRIFT_FFMPEG_PROBE_TIMEOUT_SECONDS` | `30` | Maximum media inspection time |
| `DRIFT_FFMPEG_CONVERSION_TIMEOUT_SECONDS` | `900` | Maximum MP4 conversion time (15 minutes) |
| `DRIFT_HTTP_SOCKET_TIMEOUT_SECONDS` | `30` | Idle HTTP connection timeout |
| `DRIFT_DOWNLOAD_ROOT` | System temporary directory | Download and database directory |
| `DRIFT_DB_PATH` | `<download root>/drift.sqlite3` | SQLite database path |
| `DRIFT_ALLOWED_ORIGIN` | unset (no cross-origin access) | Exact CORS origin for a separately hosted frontend; set explicitly |
| `DRIFT_TRUST_PROXY` | `false` | Trust the first `X-Forwarded-For` IP only behind a proxy that sanitizes/overwrites that header |
| `DRIFT_COOKIES_FILE` | `cookies.txt` beside the script if present | Optional yt-dlp cookie file |
| `DRIFT_COOKIES_BROWSER` | unset | Optional browser cookie source supported by yt-dlp |

Never commit cookies, credentials, or private tokens. The `.gitignore` excludes `cookies.txt`.

## API health check

```text
GET /api/health
```

The endpoint reports downloader/FFmpeg availability and the current job queue size.

## Deployment status and architecture

The current backend is a long-running Python `ThreadingHTTPServer` with a local worker pool, SQLite database, and local temporary download files. It is suitable for local development and controlled single-instance testing, but it is **not ready for a public multi-instance production deployment**.

A Vercel static deployment alone will not run this backend: the frontend calls same-origin `/api/*` endpoints for analysis, job creation, progress, cancellation, and file streaming. Deploying only the `public/` directory would therefore produce a page whose download functions do not work. The backend must first be adapted to a supported service/runtime, with durable shared job storage, background workers, and object storage or equivalent file delivery.

Before opening Drift to worldwide public traffic, complete and verify:
- A shared persistent database and durable job queue.
- Durable download storage and safe file delivery.
- Global abuse controls and correctly configured client-IP/proxy handling.

For reverse-proxy deployments, keep `DRIFT_TRUST_PROXY=false` unless the backend is reachable only through a trusted proxy that **overwrites or sanitizes** incoming `X-Forwarded-For`. If that condition is met, set `DRIFT_TRUST_PROXY=true` so per-IP rate limits can use the visitor IP. Never enable it on a directly exposed backend or behind a proxy that simply appends to an untrusted incoming header; otherwise visitors may spoof IPs and bypass rate limits.
- Outbound network protections against DNS rebinding and unsafe redirects.
- Monitoring, hard usage limits, and provider spending safeguards.

The current in-memory rate limiter and queue capacity apply only to one running process. Local SQLite and temporary files are not shared between instances and may be lost on hosts with ephemeral disks. Do not treat these controls as global quotas or a production security boundary.

## Accounts and visitor access

The current app does not require visitor accounts or provide user login. Keep this product requirement when implementing production abuse protection; use rate limits and other non-account safeguards instead of mandatory visitor registration.

## Tests

```bash
python -m unittest discover -s tests -v
```

The automated tests cover URL validation, DNS rejection, quality selection, filename cleanup, error messages, rate limiting, static frontend serving, API validation, job serialization, queue capacity, storage reservations, low-disk rejection, and file streaming. They do not prove that every third-party platform currently works. Real downloads must be tested manually with media you are authorized to download.

## Project layout

- `drift.py` — Python backend server and API.
- `public/index.html` — frontend markup.
- `public/css/style.css` — frontend styles.
- `public/js/app.js` — frontend behavior and API calls.
- `requirements.txt` — Python dependencies.
- `tests/test_core.py` — backend and HTTP tests.
- `.github/workflows/tests.yml` — automated tests on GitHub Actions.

## License

No license file is currently present. Reuse and redistribution permissions are therefore not explicitly granted.


## Build and run the backend as a container

The repository includes a portable Docker image for the existing single-instance Python backend. This is a packaging step, not a production-scale architecture: it still uses one process, SQLite, a local worker queue, and local files.

Build and run locally:

```bash
docker build -t drift-backend .
docker run --rm -p 5000:5000 -v drift-data:/data drift-backend
```

Then check `http://localhost:5000/api/health`. The named Docker volume keeps the SQLite database and completed download files across container replacement. A production host must provide a persistent volume mounted at `/data`; without it, local job history and files can be lost.

The container runs as a non-root user, installs FFmpeg, and includes an HTTP health check. Do not expose this single-instance container to unrestricted worldwide traffic yet. Before production, replace or externalize the local queue/database/file store, add shared abuse controls and outbound-network protections, and verify provider-level spending limits. A Vercel frontend can be connected later through a configured backend origin once a suitable backend host has been selected.



## Conservative single-instance host template

A Docker Compose template is provided for a **single backend instance** with conservative defaults: one active download, two queued jobs, a 300 MB per-file ceiling, a 2 GB application storage budget, a persistent named volume, container memory/CPU limits, and a loopback-only host binding. It deliberately does not expose the backend directly to the public internet.

Before using it on a host:
1. Install Docker Engine and the Docker Compose plugin.
2. Create a private environment file containing `DRIFT_ALLOWED_ORIGIN=https://your-exact-vercel-domain.example`. Replace the example with the actual HTTPS origin; do not include a path or trailing slash.
3. Keep `DRIFT_TRUST_PROXY=false` unless a trusted reverse proxy overwrites incoming `X-Forwarded-For` and direct access to the backend is blocked.
4. Configure the reverse proxy to forward HTTPS traffic to `127.0.0.1:5000`, and apply request, connection, and bandwidth limits there.
5. Run `docker compose up -d --build` and verify `/api/health`.
6. Back up the named `drift-data` volume and monitor actual disk usage; the app quota is a guardrail, not a substitute for host disk limits and alerts.

This template is not a host purchase or a production approval. The current Python service uses a single-process in-memory queue, per-process rate limits, and SQLite. Run only one backend replica. Do not scale horizontally or advertise unrestricted worldwide capacity until shared queue/database/storage, global abuse controls, outbound request protections, and load testing are implemented. Provider-level budgets and alerts must be configured in the hosting account; application code cannot enforce the provider's invoice ceiling.

## Vercel frontend configuration (backend required)

Vercel is configured to build and publish only the static frontend from `public/`. The build runs `npm run build`, which generates `public/runtime-config.js` from the `DRIFT_API_BASE_URL` environment variable. Set this variable in the Vercel project to the HTTPS base URL of the separately hosted Drift backend (for example, `https://api.example.com`). The build intentionally fails if this value is missing or invalid, so a Vercel deployment cannot silently publish a frontend that points to a nonexistent same-origin API.

Use the `public-deployment` branch, root directory `./`, Framework Preset `Other`, Build Command `npm run build`, and Output Directory `public`. Do not deploy until a working backend URL exists and the backend CORS allow-origin is set to the exact Vercel site origin. The frontend uses `window.API_BASE` to call the backend; no visitor account is needed.

The example API URL above is illustrative only. Do not enter it as a real production setting.
