from __future__ import annotations

import functools
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Generator
from urllib.parse import quote, unquote, urlparse

# Dependencies are installed explicitly from requirements.txt. Never run pip during import.
try:
    import certifi
except ImportError:
    certifi = None

CERTIFI_CA = certifi.where() if certifi else None
if CERTIFI_CA:
    os.environ.setdefault("SSL_CERT_FILE", CERTIFI_CA)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CERTIFI_CA)

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

PORT = int(os.getenv("PORT") or os.getenv("DRIFT_PORT") or "5000")   # Render gives us PORT
HOST = os.getenv("DRIFT_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
MAX_URL_LENGTH = 4096
MAX_DOWNLOAD_BYTES = int(os.getenv("DRIFT_MAX_DOWNLOAD_MB", "500")) * 1024 * 1024
MAX_STORAGE_BYTES = int(os.getenv("DRIFT_MAX_STORAGE_MB", "2048")) * 1024 * 1024
JOB_TTL_SECONDS = int(os.getenv("DRIFT_JOB_TTL_SECONDS", str(30 * 60)))
MAX_ACTIVE_JOBS = max(1, int(os.getenv("DRIFT_MAX_ACTIVE_JOBS", "2")))
MAX_QUEUED_JOBS = max(0, int(os.getenv("DRIFT_MAX_QUEUED_JOBS", "4")))
MAX_TOTAL_JOBS = MAX_ACTIVE_JOBS + MAX_QUEUED_JOBS
DOWNLOAD_ROOT = Path(os.getenv("DRIFT_DOWNLOAD_ROOT", str(Path(tempfile.gettempdir()) / "drift-downloads")))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DRIFT_DB_PATH", str(DOWNLOAD_ROOT / "drift.sqlite3")))
ALLOWED_SCHEMES = {"http", "https"}
KNOWN_HOSTS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "instagram.com", "instagr.am", "facebook.com", "fb.watch", "fb.com",
    "x.com", "twitter.com", "threads.net", "threads.com",
    "reddit.com", "redd.it", "rumble.com", "vimeo.com", "dailymotion.com", "dai.ly",
    "twitch.tv", "pinterest.com", "pin.it", "snapchat.com", "linkedin.com",
    "tumblr.com", "bilibili.com", "soundcloud.com", "streamable.com", "imgur.com",
    "vk.com", "ok.ru", "likee.video", "kwai.com", "9gag.com", "bsky.app", "t.me",
}

DB_LOCK = threading.RLock()
CANCEL_EVENTS: dict[str, threading.Event] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_ACTIVE_JOBS, thread_name_prefix="drift-download")
RATE_LOCK = threading.Lock()
RATE_WINDOWS: dict[str, list[float]] = {}


# ======================================================================
# EMBEDDED FRONTEND (HTML + CSS + JS) - everything lives in this one file
# ======================================================================
FRONTEND_ROOT = Path(__file__).resolve().parent / "public"


def now() -> float:
    return time.time()


@contextmanager
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with DB_LOCK, db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress REAL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                speed REAL,
                eta REAL,
                url TEXT NOT NULL,
                quality TEXT NOT NULL,
                format TEXT NOT NULL,
                platform TEXT NOT NULL,
                title TEXT,
                thumbnail TEXT,
                filename TEXT,
                file_size INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                job_dir TEXT,
                file_path TEXT,
                duration TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        # Jobs cannot safely remain mid-network operation after a hard process restart.
        conn.execute("""
            UPDATE jobs
            SET status='failed', phase='Failed', error='Backend restarted while this download was running.',
                message='The backend restarted before this download completed.', updated_at=?
            WHERE status IN ('queued','downloading','processing')
        """, (now(),))
        conn.commit()


def row_to_job(row: sqlite3.Row) -> dict:
    return dict(row)


def public_job(job: dict | None) -> dict | None:
    """Expose only browser-required job fields; never return server filesystem paths."""
    if job is None:
        return None
    public_fields = {
        "id", "status", "phase", "progress", "downloaded_bytes", "total_bytes",
        "speed", "eta", "url", "quality", "format", "platform", "title",
        "thumbnail", "filename", "file_size", "message", "error", "created_at",
        "updated_at", "completed_at", "duration",
    }
    return {key: value for key, value in job.items() if key in public_fields}


def get_job(job_id: str) -> dict | None:
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def update_job(job_id: str, **updates) -> None:
    if not updates:
        return
    updates["updated_at"] = now()
    columns = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [job_id]
    with DB_LOCK, db() as conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", values)
        conn.commit()


def mark_job_ready(job_id: str, **updates) -> bool:
    updates["status"] = "ready"
    updates["updated_at"] = now()
    columns = ", ".join(f"{column}=?" for column in updates)
    values = list(updates.values()) + [job_id]
    with DB_LOCK:
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            return False
        with db() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {columns} WHERE id=? AND status IN ('queued','downloading','processing')",
                values,
            )
            conn.commit()
    return cursor.rowcount == 1


def insert_job(job: dict) -> None:
    columns = list(job.keys())
    placeholders = ",".join("?" for _ in columns)
    with DB_LOCK, db() as conn:
        conn.execute(f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})", [job[c] for c in columns])
        conn.commit()


def insert_job_if_capacity(job: dict) -> bool:
    """Atomically enforce the per-process running-plus-queued job ceiling."""
    columns = list(job.keys())
    placeholders = ",".join("?" for _ in columns)
    with DB_LOCK, db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')"
        ).fetchone()[0]
        if total >= MAX_TOTAL_JOBS:
            return False
        # Reserve the maximum permitted size for each queued/active job and
        # account for completed files until their normal TTL cleanup.
        ready_bytes = conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) FROM jobs WHERE status='ready'"
        ).fetchone()[0]
        reserved_bytes = total * MAX_DOWNLOAD_BYTES
        if ready_bytes + reserved_bytes + MAX_DOWNLOAD_BYTES > MAX_STORAGE_BYTES:
            return False
        try:
            if shutil.disk_usage(DOWNLOAD_ROOT).free < MAX_DOWNLOAD_BYTES:
                return False
        except OSError:
            return False
        conn.execute(
            f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})",
            [job[column] for column in columns],
        )
        conn.commit()
        return True


def validate_public_url(raw_url: str):
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > MAX_URL_LENGTH:
        return False, "Invalid URL."
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
            return False, "Only HTTP(S) URLs are supported."
        if parsed.username is not None or parsed.password is not None:
            return False, "URLs containing usernames or passwords are not allowed."
        host = parsed.hostname.lower().rstrip(".")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, "Private or internal addresses are not allowed."
        except ValueError:
            pass
        if host == "localhost" or host.endswith(".localhost"):
            return False, "Local addresses are not allowed."
        if not any(host == allowed or host.endswith("." + allowed) for allowed in KNOWN_HOSTS):
            return False, "This source is not supported. Use a public video link from YouTube, Instagram, TikTok, Facebook, X, Reddit, Pinterest, Vimeo, etc."
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if not ip.is_global:
                    return False, "Private or internal addresses are not allowed."
        except socket.gaierror:
            return False, "The hostname could not be resolved."
        return True, parsed
    except (ValueError, UnicodeError):
        return False, "Invalid URL."


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "instagram." in host: return "Instagram"
    if "tiktok." in host: return "TikTok"
    if "youtube." in host or host == "youtu.be" or host.endswith(".youtu.be"): return "YouTube"
    if "facebook." in host or host == "fb.watch" or host.endswith(".fb.watch"): return "Facebook"
    if host == "x.com" or host.endswith(".x.com") or "twitter." in host: return "X"
    if "threads." in host: return "Threads"
    if "reddit." in host or host == "redd.it": return "Reddit"
    if "rumble." in host: return "Rumble"
    if "vimeo." in host: return "Vimeo"
    if "pinterest." in host or host == "pin.it": return "Pinterest"
    if "dailymotion." in host or host == "dai.ly": return "Dailymotion"
    if "twitch." in host: return "Twitch"
    if "snapchat." in host: return "Snapchat"
    if "linkedin." in host: return "LinkedIn"
    if "tumblr." in host: return "Tumblr"
    return "Public media"


def normalize_quality(quality: str) -> str:
    """Return 'best' or a '<height>p' string between 144p and 2160p."""
    quality = (quality or "").strip().lower()
    m = re.fullmatch(r"(\d{3,4})p", quality)
    if m and 144 <= int(m.group(1)) <= 2160:
        return f"{int(m.group(1))}p"
    return "best"


def format_selector(quality: str) -> str:
    """Prefer H.264 video + AAC audio (plays everywhere); fall back to anything available."""
    quality = normalize_quality(quality)
    if quality == "best":
        return "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    height = int(quality[:-1])
    return (f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best")


def clean_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    low = message.lower()
    if "certificate_verify_failed" in low or "unable to get local issuer" in low:
        return "TLS certificate verification failed. Update certifi and yt-dlp."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg is required for this media. Install FFmpeg or the imageio-ffmpeg package, then restart Drift."
    if "requested format is not available" in low:
        return "That quality is not available for this source. Choose another quality and try again."
    if ("login" in low or "log in" in low or "sign in" in low or "cookies" in low
            or "rate-limit" in low or "empty media response" in low or "private" in low):
        return ("This site is asking for a login (or blocked the request). Public videos usually work; "
                "for others add a cookies.txt file next to drift.py (see the instructions).")
    if "unsupported url" in low:
        return "This URL is not supported by Drift."
    if "no video could be found" in low or "no video formats" in low:
        return "No downloadable video was found at this link."
    if "http error 403" in low or "http error 429" in low:
        return "The website refused the request (403/429). Wait a bit, update yt-dlp, or try again later."
    return message[:600] or "Unknown downloader error."


def client_key(handler: BaseHTTPRequestHandler) -> str:
    # Only enable this when the hosting proxy overwrites/sanitizes X-Forwarded-For
    # and the backend cannot be reached directly by untrusted clients.
    trust_proxy = os.getenv("DRIFT_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes"}
    if trust_proxy:
        headers = getattr(handler, "headers", {})
        forwarded = headers.get("X-Forwarded-For", "") if headers else ""
        candidate = forwarded.split(",", 1)[0].strip()
        try:
            if candidate:
                return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return handler.client_address[0] or "unknown"


def rate_limit(handler: BaseHTTPRequestHandler, bucket: str, limit: int, window_seconds: int) -> bool:
    key = f"{bucket}:{client_key(handler)}"
    cutoff = now() - window_seconds
    with RATE_LOCK:
        timestamps = [t for t in RATE_WINDOWS.get(key, []) if t > cutoff]
        if len(timestamps) >= limit:
            RATE_WINDOWS[key] = timestamps
            return False
        timestamps.append(now())
        RATE_WINDOWS[key] = timestamps
        return True


def safe_filename(name: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", name or "Drift video")
    name = re.sub(r"\s+", " ", name).strip(" .")[:160] or "Drift video"
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip(" .") or "Drift video"
    return name + ".mp4"


def progress_hook(job_id: str):
    def hook(data: dict):
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            raise DownloadCancelled()
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            if downloaded > MAX_DOWNLOAD_BYTES:
                raise DownloadTooLarge("The download exceeded Drift's 500 MB limit.")
            progress = round(downloaded / total * 100, 1) if total else None
            update_job(job_id, status="downloading", phase="Downloading", progress=progress,
                       downloaded_bytes=downloaded, total_bytes=total,
                       speed=data.get("speed"), eta=data.get("eta"), message="Downloading media…")
        elif status == "finished":
            update_job(job_id, downloaded_bytes=int(data.get("downloaded_bytes") or 0), speed=None, eta=None,
                       message="Stream received…")
    return hook


def postprocessor_hook(job_id: str):
    def hook(data: dict):
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            raise DownloadCancelled()
        if data.get("status") == "started":
            update_job(job_id, status="processing", phase="Processing", progress=100, speed=None, eta=None,
                       message="Download received. Finalizing the MP4…")
    return hook


class DownloadCancelled(Exception): pass
class DownloadTooLarge(Exception): pass


MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".3gp", ".ts"}


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Use the system FFmpeg if present, otherwise the one bundled with imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def find_media(job_dir: Path) -> Path | None:
    candidates = [p for p in job_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and ".part" not in p.name.lower()
                  and not re.search(r"\.f\d+\.", p.name)]
    if not candidates:  # merge failed: fall back to any media piece
        candidates = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def probe_media(media: Path) -> dict:
    """Read codecs with `ffmpeg -i` (works without ffprobe)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available. Install FFmpeg or the imageio-ffmpeg package, then restart Drift.")
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", str(media)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    info = {"container": "", "video": [], "audio": []}
    m = re.search(r"Input #0, ([^,]+(?:,[^,]+)*?), from", out)
    if m:
        info["container"] = m.group(1)
    for line in out.splitlines():
        if "Stream #" not in line:
            continue
        if " Video:" in line and "attached pic" not in line:
            codec = re.search(r"Video: (\w+)", line)
            profile = re.search(r"Video: \w+ \(([^)]*)\)", line)
            pix = re.search(r"\b(yuvj?\d{3}p\w*|nv12|gbrp\w*|rgba?|bgra?|gray\w*|pal8)\b", line)
            dims = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            info["video"].append({
                "codec": codec.group(1) if codec else "",
                "profile": profile.group(1) if profile else "",
                "pix_fmt": pix.group(1) if pix else "",
                "w": int(dims.group(1)) if dims else 0,
                "h": int(dims.group(2)) if dims else 0,
            })
        elif " Audio:" in line:
            codec = re.search(r"Audio: (\w+)", line)
            info["audio"].append(codec.group(1) if codec else "")
    return info


def ensure_compatible_mp4(media: Path, job_id: str) -> Path:
    """Return an MP4 (H.264 + AAC, yuv420p) that plays in Windows Media Player, phones, WhatsApp, everything.

    Files that are already compatible are left untouched (fast). Otherwise only what is needed is re-encoded.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available. Restart Drift so it can install it.")
    info = probe_media(media)
    if not info["video"]:
        raise RuntimeError("The downloaded file has no video track.")
    v = info["video"][0]
    video_ok = (v["codec"] == "h264"
                and v["pix_fmt"] in {"yuv420p", "yuvj420p"}
                and v["profile"] in {"Baseline", "Constrained Baseline", "Main", "High"}
                and v["w"] % 2 == 0 and v["h"] % 2 == 0)
    audio_ok = all(c == "aac" for c in info["audio"])
    container_ok = "mp4" in info["container"].split(",")
    if video_ok and audio_ok and container_ok and media.suffix.lower() == ".mp4":
        return media

    output = tempfile.NamedTemporaryFile(prefix="drift-compatible-", suffix=".mp4", dir=media.parent, delete=False)
    output_path = Path(output.name)
    output.close()
    update_job(job_id, status="processing", phase="Processing", progress=100, speed=None, eta=None,
               message="Making the video playable everywhere (converting)…")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(media), "-map", "0:v:0", "-map", "0:a:0?"]
    if video_ok:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "21", "-pix_fmt", "yuv420p", "-profile:v", "high"]
    if audio_ok:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(output_path)]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace")
    try:
        while True:
            try:
                _, error_output = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                event = CANCEL_EVENTS.get(job_id)
                if event and event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise DownloadCancelled()
        if process.returncode:
            raise RuntimeError(f"FFmpeg could not create a playable MP4: {(error_output or '')[-600:].strip()}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty file.")
        return output_path
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def ydl_common_options() -> dict:
    """Options shared by analyze + download (certificates, ffmpeg, optional cookies for login-only sites)."""
    opts: dict = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}
    if CERTIFI_CA:
        opts["ca_certs"] = CERTIFI_CA
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    cookie_file = os.getenv("DRIFT_COOKIES_FILE") or str(Path(__file__).resolve().parent / "cookies.txt")
    if Path(cookie_file).is_file():
        opts["cookiefile"] = cookie_file
    browser = os.getenv("DRIFT_COOKIES_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def run_download(job_id: str, url: str, quality: str) -> None:
    job_dir = None
    try:
        early = CANCEL_EVENTS.get(job_id)
        if early and early.is_set():
            update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
            return
        job_dir = Path(tempfile.mkdtemp(prefix=f"drift-{job_id}-", dir=DOWNLOAD_ROOT))
        update_job(job_id, job_dir=str(job_dir), status="downloading", phase="Downloading", progress=0,
                   message="Starting media transfer…")
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -r requirements.txt")
        options = ydl_common_options()
        options |= {
            "format": format_selector(quality), "merge_output_format": "mp4",
            "outtmpl": str(job_dir / "%(title).180s.%(ext)s"),
            "restrictfilenames": True, "windowsfilenames": True,
            "max_filesize": MAX_DOWNLOAD_BYTES, "progress_hooks": [progress_hook(job_id)],
            "postprocessor_hooks": [postprocessor_hook(job_id)],
            "continuedl": True, "retries": 5, "fragment_retries": 5,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        media = find_media(job_dir)
        if media is None: raise RuntimeError("The media file was not created.")
        media = ensure_compatible_mp4(media, job_id)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        size = media.stat().st_size
        if size > MAX_DOWNLOAD_BYTES: raise DownloadTooLarge("The final media file exceeded Drift's 500 MB limit.")
        title = info.get("title") or "Drift video"
        duration = info.get("duration_string")
        final_name = safe_filename(title)
        final_path = job_dir / final_name
        if media.resolve() != final_path.resolve():
            media.replace(final_path)
        if not mark_job_ready(job_id, phase="Ready", progress=100, title=title,
                              duration=duration, filename=final_name, file_path=str(final_path),
                              file_size=size, message="Your MP4 is ready to save.", completed_at=now()):
            shutil.rmtree(job_dir, ignore_errors=True)
    except DownloadCancelled:
        update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
    except DownloadTooLarge as exc:
        update_job(job_id, status="failed", phase="Failed", error=str(exc), message=str(exc))
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():  # downloader wrapped our cancel exception
            update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)
            return
        message = clean_error(exc)
        update_job(job_id, status="failed", phase="Failed", error=message, message=message)
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
    finally:
        CANCEL_EVENTS.pop(job_id, None)


def cleanup_once() -> None:
    cutoff = now() - JOB_TTL_SECONDS
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT id, job_dir, status, updated_at FROM jobs WHERE updated_at < ?", (cutoff,)).fetchall()
        for row in rows:
            if row[2] in {"queued", "downloading", "processing"}: continue
            conn.execute("DELETE FROM jobs WHERE id=?", (row[0],))
        conn.commit()
        tracked = {Path(r[0]).name for r in conn.execute(
            "SELECT job_dir FROM jobs WHERE job_dir IS NOT NULL").fetchall()}
    for directory in DOWNLOAD_ROOT.iterdir():
        try:
            if directory.name in tracked: continue
            if directory.is_dir() and directory.name.startswith("drift-") and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        except FileNotFoundError: pass


def cleanup_loop() -> None:
    while True:
        time.sleep(60)
        try: cleanup_once()
        except Exception: pass


def json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Drift/2.1"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {fmt % args}")

    def send_json(self, data: dict, status=200, headers=None):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors()
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_cors(self):
        origin = self.headers.get("Origin")
        allowed = os.getenv("DRIFT_ALLOWED_ORIGIN", "*").strip()
        if allowed == "*" and origin:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and allowed and origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        allowed = os.getenv("DRIFT_ALLOWED_ORIGIN", "*").strip()
        self.send_response(204)
        if allowed == "*" and origin:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and allowed and origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def read_json(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Send this request as application/json.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            raise ValueError("A valid Content-Length header is required.")
        if length <= 0:
            raise ValueError("The JSON request body is empty.")
        if length > 64 * 1024:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("The request body was incomplete.")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object.")
        return data

    def static_file_path(self, path: str) -> Path | None:
        requested = unquote(path or "/").lstrip("/") or "index.html"
        root = FRONTEND_ROOT.resolve()
        candidate = (root / requested).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    def serve_static(self, path: str):
        file_path = self.static_file_path(path)
        if file_path is None:
            return self.send_json({"success": False, "message": "Not found."}, 404)
        data = file_path.read_bytes()
        content_type = (mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        if content_type.startswith("text/") or content_type in {"application/javascript", "text/javascript"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            file_path = self.static_file_path(path)
            if file_path is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.end_headers()
            return
        if path == "/api/health":
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); return
        self.send_response(404); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            with DB_LOCK, db() as conn:
                active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0]
            return self.send_json({"status":"ok","service":"drift-backend","downloader":yt_dlp is not None,
                                   "ffmpeg":find_ffmpeg() is not None,"active_jobs":active,
                                   "max_active_jobs":MAX_ACTIVE_JOBS})
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)", path)
        if m:
            job = get_job(m.group(1))
            return self.send_json({"success":True,"job":public_job(job)} if job else {"success":False,"message":"Download job not found or expired."}, 200 if job else 404)
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/file", path)
        if m: return self.stream_file(m.group(1))
        if not path.startswith("/api/"):
            return self.serve_static(path)
        self.send_json({"success":False,"message":"Not found."}, 404)

    def stream_file(self, job_id):
        job = get_job(job_id)
        if not job: return self.send_json({"success":False,"message":"Download job not found or expired."},404)
        if job["status"] != "ready" or not job.get("file_path"):
            return self.send_json({"success":False,"message":"The file is not ready yet."},409)
        path = Path(job["file_path"])
        if not path.is_file():
            update_job(job_id, status="failed", phase="Failed", error="The temporary file is no longer available.", message="The file is no longer available.")
            return self.send_json({"success":False,"message":"The temporary file is no longer available."},410)
        size = path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES: return self.send_json({"success":False,"message":"The file exceeds Drift's size limit."},413)
        range_header = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1): start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    length = int(m.group(2)); start = max(0, size-length); end = size-1
                if start > end or start >= size:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
                end = min(end, size-1); status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        filename = safe_filename(job.get("title") or "Drift video")
        # HTTP headers must be ASCII/latin-1: give an ASCII fallback name plus the full UTF-8 name.
        base = filename[:-4] if filename.lower().endswith(".mp4") else filename
        ascii_base = re.sub(r"[^A-Za-z0-9 ._()\-]+", "", base.encode("ascii", "ignore").decode("ascii")).strip(" .")
        ascii_name = (ascii_base or "drift-video") + ".mp4"
        self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self.send_cors(); self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start); remaining = length
                while remaining:
                    chunk = f.read(min(1024*1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # browser closed the connection (e.g. user cancelled) - not an error

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/analyze": return self.analyze()
        if path == "/api/downloads": return self.create_download()
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/cancel", path)
        if m: return self.cancel(m.group(1))
        if path == "/api/download": return self.send_json({"success":False,"message":"This endpoint was replaced by the live download job API. Refresh the Drift frontend."},410)
        self.send_json({"success":False,"message":"Not found."},404)

    def analyze(self):
        if not rate_limit(self,"analyze",20,60): return self.send_json({"success":False,"message":"Too many analysis requests. Please wait a moment."},429)
        if yt_dlp is None: return self.send_json({"success":False,"message":"yt-dlp is not installed. Run: python -m pip install -r requirements.txt"},500)
        try: body=self.read_json()
        except Exception as exc: return self.send_json({"success":False,"message":str(exc)},400)
        url=str(body.get("url","")).strip(); valid, result=validate_public_url(url)
        if not valid: return self.send_json({"success":False,"message":result},400)
        try:
            opts=ydl_common_options(); opts["skip_download"]=True
            with yt_dlp.YoutubeDL(opts) as ydl: info=ydl.extract_info(url,download=False)
            heights=sorted({int(f["height"]) for f in info.get("formats",[]) if f.get("height") and f.get("vcodec") not in (None,"none")},reverse=True)
            heights=[h for h in heights if 144 <= h <= 2160]
            formats=[{"quality":f"{h}p","height":h} for h in heights[:8]]
            formats.insert(0,{"quality":"Best available","height":None})
            return self.send_json({"success":True,"platform":detect_platform(url),"title":info.get("title") or "Public video",
                                   "duration":info.get("duration_string"),"thumbnail":info.get("thumbnail"),"formats":formats,
                                   "ffmpeg":find_ffmpeg() is not None})
        except Exception as exc:
            return self.send_json({"success":False,"message":clean_error(exc)},422)

    def create_download(self):
        if not rate_limit(self,"download",5,60): return self.send_json({"success":False,"message":"Download limit reached. Please wait a moment."},429)
        if yt_dlp is None: return self.send_json({"success":False,"message":"yt-dlp is not installed. Run: python -m pip install -r requirements.txt"},500)
        try: body=self.read_json()
        except Exception as exc: return self.send_json({"success":False,"message":str(exc)},400)
        url=str(body.get("url","")).strip(); quality=str(body.get("quality","best"))
        valid, result=validate_public_url(url)
        if not valid: return self.send_json({"success":False,"message":result},400)
        quality=normalize_quality(quality)
        job_id=uuid.uuid4().hex
        event=threading.Event(); CANCEL_EVENTS[job_id]=event
        t=now()
        job={"id":job_id,"status":"queued","phase":"Queued","progress":0,"downloaded_bytes":0,"total_bytes":0,
             "speed":None,"eta":None,"url":url,"quality":quality,"format":"MP4","platform":detect_platform(url),
             "title":str(body.get("title") or "Preparing media…")[:300],"thumbnail":str(body.get("thumbnail") or "")[:2000],
             "filename":None,"file_size":0,"message":"Your download is queued.","error":None,"created_at":t,"updated_at":t,
             "completed_at":None,"job_dir":None,"file_path":None,"duration":str(body.get("duration") or "")[:100]}
        if not insert_job_if_capacity(job):
            CANCEL_EVENTS.pop(job_id, None)
            return self.send_json({"success":False,"message":"Drift is at its current capacity. Please wait for a download to finish or for temporary files to expire."},429)
        try:
            EXECUTOR.submit(run_download,job_id,url,quality)
        except RuntimeError:
            CANCEL_EVENTS.pop(job_id, None)
            update_job(job_id, status="failed", phase="Failed",
                       error="The download service is temporarily unavailable.",
                       message="The download service is temporarily unavailable.")
            return self.send_json({"success":False,"message":"The download service is temporarily unavailable. Please try again shortly."},503)
        return self.send_json({"success":True,"job":public_job(job)},202)

    def cancel(self, job_id):
        job=get_job(job_id)
        if not job: return self.send_json({"success":False,"message":"Download job not found or expired."},404)
        if job["status"] in {"ready","failed","cancelled"}: return self.send_json({"success":True,"job":job})
        event=CANCEL_EVENTS.setdefault(job_id,threading.Event()); event.set()
        with DB_LOCK, db() as conn:
            conn.execute(
                """UPDATE jobs SET status='cancelled', phase='Cancelled', message='Cancelling download…', updated_at=?
                   WHERE id=? AND status IN ('queued','downloading','processing')""",
                (now(), job_id),
            )
            conn.commit()
        return self.send_json({"success":True,"job":public_job(get_job(job_id))})


def main():
    init_db()
    threading.Thread(target=cleanup_loop, daemon=True, name="drift-cleanup").start()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    print(f"Drift backend listening on http://{HOST}:{PORT}")
    print(f"FFmpeg: {find_ffmpeg() or 'NOT FOUND'} | yt-dlp: {'ok' if yt_dlp else 'MISSING'}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        EXECUTOR.shutdown(wait=False,cancel_futures=True); server.server_close()


if __name__ == "__main__": main()
