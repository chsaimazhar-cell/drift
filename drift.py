from __future__ import annotations

import functools
import contextvars
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
from typing import Generator, Protocol, runtime_checkable
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
MAX_HTTP_CONNECTIONS = max(4, int(os.getenv("DRIFT_MAX_HTTP_CONNECTIONS", "32")))
FFMPEG_PROBE_TIMEOUT_SECONDS = max(5, int(os.getenv("DRIFT_FFMPEG_PROBE_TIMEOUT_SECONDS", "30")))
FFMPEG_CONVERSION_TIMEOUT_SECONDS = max(30, int(os.getenv("DRIFT_FFMPEG_CONVERSION_TIMEOUT_SECONDS", "900")))
HTTP_SOCKET_TIMEOUT_SECONDS = max(5, int(os.getenv("DRIFT_HTTP_SOCKET_TIMEOUT_SECONDS", "30")))
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
DOWNLOAD_REPOSITORY_CONTEXT = contextvars.ContextVar("drift_download_repository", default=None)


class LocalThreadPoolJobQueue:
    """Development-only SQLite queue with a single-process delayed-job dispatcher.

    This adapter is not durable across process loss and must not be used with
    multiple API/worker instances. Production requires a shared queue service.
    """

    LEASE_SECONDS = 300
    MAX_ATTEMPTS = 3
    DISPATCH_INTERVAL_SECONDS = 1

    def __init__(self, max_workers: int):
        self.max_workers = max(1, max_workers)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="drift-download")
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._stop_dispatcher = threading.Event()
        self._dispatcher: threading.Thread | None = None

    def _execute_claimed(self, job_id: str, worker_id: str) -> None:
        job = JOB_REPOSITORY.claim_job(job_id, worker_id, lease_seconds=self.LEASE_SECONDS)
        if job is None:
            return

        stop_heartbeat = threading.Event()

        def renew_lease_loop():
            interval = max(1, self.LEASE_SECONDS // 3)
            while not stop_heartbeat.wait(interval):
                try:
                    if not JOB_REPOSITORY.renew_lease(
                        job_id, worker_id, lease_seconds=self.LEASE_SECONDS
                    ):
                        return
                except Exception:
                    return

        heartbeat = threading.Thread(
            target=renew_lease_loop, daemon=True, name=f"drift-lease-{job_id[:8]}"
        )
        heartbeat.start()
        try:
            run_download(job_id, job["url"], job["quality"])
        except Exception:
            JOB_REPOSITORY.retry_job(
                job_id, worker_id, delay_seconds=30, max_attempts=self.MAX_ATTEMPTS
            )
            raise
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)

        final_job = JOB_REPOSITORY.get(job_id)
        if final_job and final_job.get("status") in {"ready", "failed", "cancelled"}:
            JOB_REPOSITORY.acknowledge_job(job_id, worker_id)
        else:
            JOB_REPOSITORY.retry_job(
                job_id, worker_id, delay_seconds=30, max_attempts=self.MAX_ATTEMPTS
            )

    def _submit(self, job_id: str):
        with self._lock:
            if self._stop_dispatcher.is_set() or job_id in self._in_flight:
                return None
            self._in_flight.add(job_id)
        try:
            future = self.executor.submit(self._execute_claimed, job_id, uuid.uuid4().hex)
        except RuntimeError:
            with self._lock:
                self._in_flight.discard(job_id)
            raise
        future.add_done_callback(lambda _future, current=job_id: self._discard_in_flight(current))
        return future

    def _discard_in_flight(self, job_id: str) -> None:
        with self._lock:
            self._in_flight.discard(job_id)

    def dispatch_due_jobs(self) -> int:
        """Submit due jobs up to currently available local worker capacity."""
        with self._lock:
            capacity = self.max_workers - len(self._in_flight)
        if capacity <= 0:
            return 0
        due_jobs = JOB_REPOSITORY.list_due_jobs(limit=capacity)
        submitted = 0
        for job in due_jobs:
            if self._submit(job["id"]) is not None:
                submitted += 1
        return submitted

    def _dispatch_loop(self) -> None:
        while not self._stop_dispatcher.wait(self.DISPATCH_INTERVAL_SECONDS):
            try:
                self.dispatch_due_jobs()
            except Exception:
                # A later poll retries discovery; no job is claimed by this scan.
                continue

    def start_dispatcher(self) -> None:
        """Start polling due jobs; call only after the local repository is initialized."""
        with self._lock:
            if self._dispatcher is not None or self._stop_dispatcher.is_set():
                return
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop, daemon=True, name="drift-queue-dispatcher"
            )
            self._dispatcher.start()

    def enqueue(self, job_id: str):
        """Validate and immediately submit a new job; later retries are found by polling."""
        if JOB_REPOSITORY.get(job_id) is None:
            raise ValueError("Cannot enqueue an unknown job.")
        return self._submit(job_id)

    def shutdown(self, wait: bool = False, cancel_pending: bool = True) -> None:
        self._stop_dispatcher.set()
        dispatcher = self._dispatcher
        if dispatcher is not None:
            dispatcher.join(timeout=2)
        self.executor.shutdown(wait=wait, cancel_futures=cancel_pending)


# Replace with a durable queue adapter before running multiple API/worker instances.
JOB_QUEUE = LocalThreadPoolJobQueue(MAX_ACTIVE_JOBS)
RATE_LOCK = threading.Lock()
RATE_WINDOWS: dict[str, list[float]] = {}


@runtime_checkable
class AbuseControls(Protocol):
    """Shared enforcement boundary for request limits and the emergency stop."""
    def allow_request(self, key: str, limit: int, window_seconds: int, timestamp: float) -> bool: ...
    def accepting_work(self) -> bool: ...


class LocalAbuseControls:
    """Single-process development adapter; not safe for multi-instance production."""
    def allow_request(self, key: str, limit: int, window_seconds: int, timestamp: float) -> bool:
        cutoff = timestamp - window_seconds
        with RATE_LOCK:
            if len(RATE_WINDOWS) > 10000:
                stale_before = timestamp - 3600
                for old_key in list(RATE_WINDOWS):
                    recent = [stamp for stamp in RATE_WINDOWS[old_key] if stamp > stale_before]
                    if recent:
                        RATE_WINDOWS[old_key] = recent
                    else:
                        RATE_WINDOWS.pop(old_key, None)
                if len(RATE_WINDOWS) > 10000:
                    for old_key in list(RATE_WINDOWS)[:len(RATE_WINDOWS) - 10000]:
                        RATE_WINDOWS.pop(old_key, None)
            timestamps = [stamp for stamp in RATE_WINDOWS.get(key, []) if stamp > cutoff]
            if len(timestamps) >= limit:
                RATE_WINDOWS[key] = timestamps
                return False
            timestamps.append(timestamp)
            RATE_WINDOWS[key] = timestamps
            return True

    def accepting_work(self) -> bool:
        value = os.getenv("DRIFT_ACCEPTING_WORK", "true").strip().lower()
        return value not in {"0", "false", "no", "off", "disabled"}



@runtime_checkable
class AtomicAbuseStateStore(Protocol):
    """Backend contract: each rate-limit consume must be atomic and TTL-bounded."""

    def consume_request(
        self, key: str, limit: int, window_seconds: int, timestamp: float
    ) -> bool: ...
    def accepting_work(self) -> bool: ...


class SharedAbuseControls:
    """Adapter for a shared atomic store; the store implementation is supplied separately."""

    def __init__(self, store: AtomicAbuseStateStore):
        self.store = store

    def allow_request(self, key: str, limit: int, window_seconds: int, timestamp: float) -> bool:
        return self.store.consume_request(key, limit, window_seconds, timestamp)

    def accepting_work(self) -> bool:
        return self.store.accepting_work()


ABUSE_CONTROLS: AbuseControls = LocalAbuseControls()


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
    """Initialize the configured local repository schema."""
    JOB_REPOSITORY.initialize()


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


class SQLiteJobRepository:
    """Local single-process job persistence adapter.

    This adapter deliberately uses SQLite and local disk. It is not a
    multi-instance production repository; replace it with a shared database
    adapter before running multiple API or worker instances.
    """

    SCHEMA_VERSION = 3

    @staticmethod
    def _migration_1(conn: sqlite3.Connection) -> None:
        """Create the baseline jobs schema and indexes without replacing existing data."""
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

    @staticmethod
    def _migration_2(conn: sqlite3.Connection) -> None:
        """Add queue scheduling and worker-lease metadata to existing jobs."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        additions = (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("available_at", "REAL NOT NULL DEFAULT 0"),
            ("lease_owner", "TEXT"),
            ("lease_expires_at", "REAL"),
        )
        for column, definition in additions:
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_queue_claim "
            "ON jobs(status, available_at, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_lease_expiry "
            "ON jobs(status, lease_expires_at)"
        )

    @staticmethod
    def _migration_3(conn: sqlite3.Connection) -> None:
        """Add provider-neutral metadata for private object storage."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        additions = (
            ("storage_backend", "TEXT NOT NULL DEFAULT 'local'"),
            ("object_key", "TEXT"),
            ("object_content_type", "TEXT"),
            ("object_size", "INTEGER"),
            ("object_expires_at", "REAL"),
        )
        for column, definition in additions:
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_object_expiry "
            "ON jobs(storage_backend, object_expires_at)"
        )

    MIGRATIONS = (
        (1, _migration_1.__func__),
        (2, _migration_2.__func__),
        (3, _migration_3.__func__),
    )

    def initialize(self) -> None:
        """Apply versioned, transactional schema migrations without mutating job state."""
        with DB_LOCK, db() as conn:
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {current_version} is newer than this Drift backend supports."
                )

            for version, migration in self.MIGRATIONS:
                if version <= current_version:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                migration(conn)
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.commit()
                current_version = version


    def get(self, job_id: str) -> dict | None:
        with DB_LOCK, db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row_to_job(row) if row else None

    def list_due_jobs(self, limit: int = 10, timestamp: float | None = None) -> list[dict]:
        """Return queued job IDs whose retry/initial availability time has arrived."""
        if limit < 1:
            return []
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            rows = conn.execute(
                """SELECT id FROM jobs
                   WHERE status='queued' AND available_at<=?
                   ORDER BY available_at, created_at LIMIT ?""",
                (timestamp, limit),
            ).fetchall()
        return [{"id": row["id"]} for row in rows]

    def update(self, job_id: str, updates: dict) -> None:
        if not updates:
            return
        values_to_write = dict(updates)
        values_to_write["updated_at"] = now()
        columns = ", ".join(f"{key}=?" for key in values_to_write)
        values = list(values_to_write.values()) + [job_id]
        with DB_LOCK, db() as conn:
            conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", values)
            conn.commit()

    def mark_ready(self, job_id: str, updates: dict) -> bool:
        values_to_write = dict(updates)
        values_to_write["status"] = "ready"
        values_to_write["updated_at"] = now()
        columns = ", ".join(f"{column}=?" for column in values_to_write)
        values = list(values_to_write.values()) + [job_id]
        with DB_LOCK, db() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {columns} WHERE id=? AND status IN ('queued','downloading','processing')",
                values,
            )
            conn.commit()
        return cursor.rowcount == 1

    def insert(self, job: dict) -> None:
        columns = list(job.keys())
        placeholders = ",".join("?" for _ in columns)
        with DB_LOCK, db() as conn:
            conn.execute(
                f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})",
                [job[column] for column in columns],
            )
            conn.commit()

    def insert_if_capacity(self, job: dict) -> bool:
        """Atomically enforce the local running/queued and disk budget."""
        columns = list(job.keys())
        placeholders = ",".join("?" for _ in columns)
        with DB_LOCK, db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')"
            ).fetchone()[0]
            if total >= MAX_TOTAL_JOBS:
                return False
            ready_bytes = conn.execute(
                "SELECT COALESCE(SUM(file_size), 0) FROM jobs WHERE status='ready'"
            ).fetchone()[0]
            reserved_bytes = total * MAX_DOWNLOAD_BYTES * 2
            if ready_bytes + reserved_bytes + (MAX_DOWNLOAD_BYTES * 2) > MAX_STORAGE_BYTES:
                return False
            try:
                if MEDIA_STORAGE.free_bytes() < (MAX_DOWNLOAD_BYTES * 2):
                    return False
            except OSError:
                return False
            conn.execute(
                f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})",
                [job[column] for column in columns],
            )
            conn.commit()
            return True

    def recover_interrupted(
        self, max_attempts: int = 3, timestamp: float | None = None
    ) -> list[dict]:
        """Preserve queued work and safely requeue interrupted leased work on startup."""
        if max_attempts < 1:
            raise ValueError("Attempt limit must be positive.")
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id, job_dir, status, attempts FROM jobs
                   WHERE status IN ('queued','downloading','processing')
                   ORDER BY created_at"""
            ).fetchall()
            for row in rows:
                if row["status"] == "queued":
                    conn.execute(
                        """UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL
                           WHERE id=? AND status='queued'""",
                        (row["id"],),
                    )
                    continue
                retryable = row["attempts"] < max_attempts
                conn.execute(
                    """UPDATE jobs SET status=?, phase=?, message=?, error=?,
                       available_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND status IN ('downloading','processing')""",
                    ("queued" if retryable else "failed",
                     "Queued" if retryable else "Failed",
                     "The service restarted; retry scheduled." if retryable else "The service restarted after the retry limit.",
                     None if retryable else "The service restarted after the retry limit.",
                     timestamp, timestamp, row["id"]),
                )
            conn.commit()
        return [dict(row) for row in rows]

    def expired_jobs(self, cutoff: float) -> dict:
        with DB_LOCK, db() as conn:
            rows = conn.execute(
                """SELECT id, job_dir, status, updated_at, storage_backend, object_key
                   FROM jobs WHERE updated_at < ?""",
                (cutoff,),
            ).fetchall()
            expired = [dict(row) for row in rows if row["status"] not in {"queued", "downloading", "processing"}]
            for row in expired:
                conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            tracked = [row[0] for row in conn.execute(
                "SELECT job_dir FROM jobs WHERE job_dir IS NOT NULL"
            ).fetchall()]
            conn.commit()
        return {"expired": expired, "tracked": tracked}

    def claim_job(
        self, job_id: str, worker_id: str, lease_seconds: int = 300, timestamp: float | None = None
    ) -> dict | None:
        """Atomically claim one queued job for a worker and issue a bounded lease."""
        if not worker_id or lease_seconds < 1:
            raise ValueError("A worker ID and positive lease duration are required.")
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE jobs
                   SET status='downloading', phase='Claimed', lease_owner=?,
                       lease_expires_at=?, attempts=attempts+1, updated_at=?
                   WHERE id=? AND status='queued' AND available_at<=?
                         AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
                (worker_id, timestamp + lease_seconds, timestamp, job_id, timestamp, timestamp),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
        return row_to_job(row) if row else None

    def renew_lease(
        self, job_id: str, worker_id: str, lease_seconds: int = 300, timestamp: float | None = None
    ) -> bool:
        if not worker_id or lease_seconds < 1:
            raise ValueError("A worker ID and positive lease duration are required.")
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET lease_expires_at=?, updated_at=?
                   WHERE id=? AND lease_owner=? AND status IN ('downloading','processing')
                         AND lease_expires_at>?""",
                (timestamp + lease_seconds, timestamp, job_id, worker_id, timestamp),
            )
            conn.commit()
        return cursor.rowcount == 1

    def acknowledge_job(self, job_id: str, worker_id: str, timestamp: float | None = None) -> bool:
        """Release a worker lease only after the job has reached a terminal state."""
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND lease_owner=? AND status IN ('ready','failed','cancelled')""",
                (timestamp, job_id, worker_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    def retry_job(
        self, job_id: str, worker_id: str, delay_seconds: int = 30,
        max_attempts: int = 3, timestamp: float | None = None
    ) -> bool:
        """Requeue a leased job with bounded attempts and a not-before timestamp."""
        if delay_seconds < 0 or max_attempts < 1:
            raise ValueError("Retry delay and attempt limit are invalid.")
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            cursor = conn.execute(
                """UPDATE jobs
                   SET status=CASE WHEN attempts < ? THEN 'queued' ELSE 'failed' END,
                       phase=CASE WHEN attempts < ? THEN 'Queued' ELSE 'Failed' END,
                       message=CASE WHEN attempts < ? THEN 'Retry scheduled.' ELSE 'Retry limit reached.' END,
                       error=CASE WHEN attempts < ? THEN NULL ELSE 'Retry limit reached.' END,
                       available_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND lease_owner=? AND status IN ('downloading','processing')""",
                (max_attempts, max_attempts, max_attempts, max_attempts,
                 timestamp + delay_seconds, timestamp, job_id, worker_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    def recover_expired_leases(
        self, max_attempts: int = 3, timestamp: float | None = None
    ) -> list[dict]:
        """Requeue expired worker leases, or fail jobs that exhausted their attempts."""
        if max_attempts < 1:
            raise ValueError("Attempt limit must be positive.")
        timestamp = now() if timestamp is None else timestamp
        with DB_LOCK, db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id, attempts FROM jobs
                   WHERE status IN ('downloading','processing')
                     AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
                   ORDER BY lease_expires_at, created_at""",
                (timestamp,),
            ).fetchall()
            for row in rows:
                retryable = row["attempts"] < max_attempts
                conn.execute(
                    """UPDATE jobs SET status=?, phase=?, message=?, error=?,
                       available_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND lease_expires_at<=?""",
                    ("queued" if retryable else "failed",
                     "Queued" if retryable else "Failed",
                     "Worker lease expired; retry scheduled." if retryable else "Worker lease expired; retry limit reached.",
                     None if retryable else "Worker lease expired after the retry limit.",
                     timestamp, timestamp, row["id"], timestamp),
                )
            conn.commit()
        return [dict(row) for row in rows]

    def active_count(self) -> int:
        with DB_LOCK, db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')"
            ).fetchone()[0]

    def cancel_active(self, job_id: str) -> None:
        with DB_LOCK, db() as conn:
            conn.execute(
                """UPDATE jobs SET status='cancelled', phase='Cancelled',
                   message='Cancelling download…', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND status IN ('queued','downloading','processing')""",
                (now(), job_id),
            )
            conn.commit()



# The current adapter is intentionally local-only. A managed database adapter
# can replace this object without changing the HTTP handlers or downloader.
JOB_REPOSITORY = SQLiteJobRepository()


def _active_job_repository():
    return DOWNLOAD_REPOSITORY_CONTEXT.get() or JOB_REPOSITORY


def get_job(job_id: str) -> dict | None:
    return _active_job_repository().get(job_id)


def update_job(job_id: str, **updates) -> None:
    _active_job_repository().update(job_id, updates)


def mark_job_ready(job_id: str, **updates) -> bool:
    with DB_LOCK:
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            return False
        return _active_job_repository().mark_ready(job_id, updates)


def insert_job(job: dict) -> None:
    JOB_REPOSITORY.insert(job)


def insert_job_if_capacity(job: dict) -> bool:
    return JOB_REPOSITORY.insert_if_capacity(job)


def validate_public_url(raw_url: str):
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > MAX_URL_LENGTH:
        return False, "Invalid URL."
    try:
        cleaned_url = raw_url.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in cleaned_url):
            return False, "Invalid URL."
        parsed = urlparse(cleaned_url)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES or not parsed.hostname:
            return False, "Only HTTP(S) URLs are supported."
        if parsed.username is not None or parsed.password is not None:
            return False, "URLs containing usernames or passwords are not allowed."
        port = parsed.port
        if port is not None and port != (443 if scheme == "https" else 80):
            return False, "Only standard HTTP and HTTPS ports are supported."
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
    # Provider exceptions can contain signed media URLs, cookies, or access tokens.
    message = re.sub(r"https?://\S+", "[redacted URL]", str(exc).replace("\n", " ").strip(), flags=re.IGNORECASE)
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


def accepting_new_work() -> bool:
    return ABUSE_CONTROLS.accepting_work()


def rate_limit(handler: BaseHTTPRequestHandler, bucket: str, limit: int, window_seconds: int) -> bool:
    key = f"{bucket}:{client_key(handler)}"
    return ABUSE_CONTROLS.allow_request(key, limit, window_seconds, now())


def safe_filename(name: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", name or "Drift video")
    name = re.sub(r"\s+", " ", name).strip(" .")[:160] or "Drift video"
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip(" .") or "Drift video"
    return name + ".mp4"


def check_worker_lease(lease_lost_event=None) -> None:
    """Abort downloader work as soon as its distributed worker lease is lost."""
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise WorkerLeaseLost("Worker lease was lost; stopping media processing.")


class WorkerLeaseLost(RuntimeError):
    """Raised when a worker no longer owns the job lease."""


def progress_hook(job_id: str, lease_lost_event=None):
    def hook(data: dict):
        check_worker_lease(lease_lost_event)
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


def postprocessor_hook(job_id: str, lease_lost_event=None):
    def hook(data: dict):
        check_worker_lease(lease_lost_event)
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


class StoredMediaObject:
    """Provider-neutral reference to a private, completed media object."""

    __slots__ = ("object_key", "size_bytes", "content_type", "filename")

    def __init__(self, object_key: str, size_bytes: int, content_type: str, filename: str):
        self.object_key = object_key
        self.size_bytes = size_bytes
        self.content_type = content_type
        self.filename = filename


@runtime_checkable
class PrivateMediaObjectStore(Protocol):
    """Contract for durable private object storage; implementations must never make objects public."""

    def put_file(
        self, source: Path, object_key: str, content_type: str, filename: str
    ) -> StoredMediaObject:
        """Upload a completed local artifact and return its canonical private object reference."""
        ...

    def create_download_url(self, object_key: str, expires_in_seconds: int) -> str:
        """Return a short-lived, object-scoped download URL after authorization."""
        ...

    def open_file_range(self, object_key: str, start: int, end: int) -> BinaryIO:
        """Open exactly the requested byte range from a private object."""
        ...

    def delete_object(self, object_key: str) -> None:
        """Delete one private object; missing objects should be treated as already deleted."""
        ...

    def delete_expired_objects(self, cutoff_timestamp: float) -> int:
        """Delete objects whose retention deadline has passed and return the deleted count."""
        ...


class LocalMediaStorage:
    """Local-disk implementation of the replaceable media storage boundary."""
    def __init__(self, root: Path): self.root = root
    def create_workspace(self, job_id: str) -> Path:
        return Path(tempfile.mkdtemp(prefix="drift-" + job_id + "-", dir=self.root))
    def find_media(self, job_dir: Path) -> Path | None:
        candidates = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and ".part" not in p.name.lower() and not re.search(r"\.f\d+\.", p.name)]
        if not candidates: candidates = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
        return max(candidates, key=lambda p: p.stat().st_size) if candidates else None
    def size(self, path: Path) -> int: return path.stat().st_size
    def finalize(self, source: Path, destination: Path) -> Path:
        if source.resolve() != destination.resolve(): source.replace(destination)
        return destination
    def delete_workspace(self, path: Path | None) -> None:
        if path is not None: shutil.rmtree(path, ignore_errors=True)
    def is_file(self, path: Path) -> bool: return path.is_file()
    def open_file(self, path: Path): return path.open("rb")
    def free_bytes(self) -> int: return shutil.disk_usage(self.root).free
    def remove_interrupted(self, raw_path: str | None) -> None:
        if not raw_path: return
        try:
            path = Path(raw_path).resolve()
            if path.parent == self.root.resolve() and path.name.startswith("drift-") and path.is_dir(): self.delete_workspace(path)
        except (OSError, RuntimeError): return
    def cleanup_stale(self, tracked: set[str], cutoff: float) -> None:
        for directory in self.root.iterdir():
            try:
                if directory.name not in tracked and directory.is_dir() and directory.name.startswith("drift-") and directory.stat().st_mtime < cutoff: self.delete_workspace(directory)
            except FileNotFoundError: pass

MEDIA_STORAGE = LocalMediaStorage(DOWNLOAD_ROOT)
# Optional provider adapter. None keeps the existing local-disk behavior.
PRIVATE_MEDIA_STORE: PrivateMediaObjectStore | None = None

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
    """Backward-compatible helper delegated to the configured storage adapter."""
    return MEDIA_STORAGE.find_media(job_dir)

def probe_media(media: Path) -> dict:
    """Read codecs with `ffmpeg -i` (works without ffprobe)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available. Install FFmpeg or the imageio-ffmpeg package, then restart Drift.")
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(media)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFMPEG_PROBE_TIMEOUT_SECONDS,
        ).stderr
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Media inspection took too long. Please try another video.") from exc
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


def ensure_compatible_mp4(media: Path, job_id: str, lease_lost_event=None) -> Path:
    """Return an MP4 (H.264 + AAC, yuv420p) that plays in Windows Media Player, phones, WhatsApp, everything.

    Files that are already compatible are left untouched (fast). Otherwise only what is needed is re-encoded.
    """
    check_worker_lease(lease_lost_event)
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
    deadline = time.monotonic() + FFMPEG_CONVERSION_TIMEOUT_SECONDS
    try:
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, FFMPEG_CONVERSION_TIMEOUT_SECONDS)
                _, error_output = process.communicate(timeout=min(0.5, remaining))
                check_worker_lease(lease_lost_event)
                break
            except subprocess.TimeoutExpired:
                check_worker_lease(lease_lost_event)
                event = CANCEL_EVENTS.get(job_id)
                if event and event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise DownloadCancelled()
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise RuntimeError(
                        f"FFmpeg conversion exceeded the {FFMPEG_CONVERSION_TIMEOUT_SECONDS}-second time limit."
                    )
        if process.returncode:
            raise RuntimeError(f"FFmpeg could not create a playable MP4: {(error_output or '')[-600:].strip()}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty file.")
        return output_path
    except WorkerLeaseLost:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        output_path.unlink(missing_ok=True)
        raise
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


def run_download(
    job_id: str, url: str, quality: str, *, lease_lost_event=None, repository=None
) -> None:
    repository_token = DOWNLOAD_REPOSITORY_CONTEXT.set(repository) if repository is not None else None
    job_dir = None
    uploaded_object_key = None
    try:
        check_worker_lease(lease_lost_event)
        early = CANCEL_EVENTS.get(job_id)
        if early and early.is_set():
            update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
            return
        job_dir = MEDIA_STORAGE.create_workspace(job_id)
        update_job(job_id, job_dir=str(job_dir), status="downloading", phase="Downloading", progress=0,
                   message="Starting media transfer…")
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -r requirements.txt")
        options = ydl_common_options()
        options |= {
            "format": format_selector(quality), "merge_output_format": "mp4",
            "outtmpl": str(job_dir / "%(title).180s.%(ext)s"),
            "restrictfilenames": True, "windowsfilenames": True,
            "max_filesize": MAX_DOWNLOAD_BYTES, "progress_hooks": [progress_hook(job_id, lease_lost_event)],
            "postprocessor_hooks": [postprocessor_hook(job_id, lease_lost_event)],
            "continuedl": True, "retries": 5, "fragment_retries": 5,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        check_worker_lease(lease_lost_event)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        media = MEDIA_STORAGE.find_media(job_dir)
        if media is None: raise RuntimeError("The media file was not created.")
        media = ensure_compatible_mp4(media, job_id, lease_lost_event)
        check_worker_lease(lease_lost_event)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        check_worker_lease(lease_lost_event)
        size = MEDIA_STORAGE.size(media)
        if size > MAX_DOWNLOAD_BYTES: raise DownloadTooLarge("The final media file exceeded Drift's 500 MB limit.")
        title = info.get("title") or "Drift video"
        duration = info.get("duration_string")
        final_name = safe_filename(title)
        final_path = job_dir / final_name
        check_worker_lease(lease_lost_event)
        MEDIA_STORAGE.finalize(media, final_path)
        if PRIVATE_MEDIA_STORE is None:
            ready_updates = {
                "phase": "Ready", "progress": 100, "title": title, "duration": duration,
                "filename": final_name, "file_path": str(final_path), "file_size": size,
                "storage_backend": "local", "object_key": None, "object_content_type": None,
                "object_size": None, "object_expires_at": None,
                "message": "Your MP4 is ready to save.", "completed_at": now(),
            }
            check_worker_lease(lease_lost_event)
            if not mark_job_ready(job_id, **ready_updates):
                MEDIA_STORAGE.delete_workspace(job_dir)
        else:
            object_key = f"jobs/{job_id}/{uuid.uuid4().hex}.mp4"
            stored = PRIVATE_MEDIA_STORE.put_file(final_path, object_key, "video/mp4", final_name)
            # Track the upload before checking the lease so lease loss cannot orphan it.
            uploaded_object_key = stored.object_key
            check_worker_lease(lease_lost_event)
            if stored.object_key != object_key or stored.size_bytes != size or stored.content_type != "video/mp4":
                raise RuntimeError("Private storage returned inconsistent media metadata.")
            expires_at = now() + JOB_TTL_SECONDS
            ready_updates = {
                "phase": "Ready", "progress": 100, "title": title, "duration": duration,
                "filename": final_name, "file_path": None, "file_size": size,
                "storage_backend": "object", "object_key": stored.object_key,
                "object_content_type": stored.content_type, "object_size": stored.size_bytes,
                "object_expires_at": expires_at,
                "message": "Your MP4 is ready to save.", "completed_at": now(),
            }
            check_worker_lease(lease_lost_event)
            if mark_job_ready(job_id, **ready_updates):
                uploaded_object_key = None  # The repository now owns this object reference.
            MEDIA_STORAGE.delete_workspace(job_dir)
    except WorkerLeaseLost:
        # The new owner is responsible for job state; this worker only cleans its artifacts.
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except DownloadCancelled:
        update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
        if job_dir is not None:
            MEDIA_STORAGE.delete_workspace(job_dir)
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
        # Let the queue adapter decide whether this failure is retryable.
        # Marking the job failed here would make retry_job() reject the transition.
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        if uploaded_object_key and PRIVATE_MEDIA_STORE is not None:
            try:
                PRIVATE_MEDIA_STORE.delete_object(uploaded_object_key)
            except Exception:
                pass  # Provider lifecycle cleanup is the fallback for an orphaned upload.
        CANCEL_EVENTS.pop(job_id, None)
        if repository_token is not None:
            DOWNLOAD_REPOSITORY_CONTEXT.reset(repository_token)


def recover_interrupted_jobs() -> int:
    """Preserve queued jobs and requeue interrupted work after removing partial output."""
    rows = JOB_REPOSITORY.recover_interrupted(max_attempts=LocalThreadPoolJobQueue.MAX_ATTEMPTS)
    recovered = 0
    for row in rows:
        if row.get("status") in {"downloading", "processing"}:
            MEDIA_STORAGE.remove_interrupted(row.get("job_dir"))
            if row.get("attempts", 0) < LocalThreadPoolJobQueue.MAX_ATTEMPTS:
                recovered += 1
        elif row.get("status") == "queued":
            recovered += 1
    return recovered


def cleanup_once() -> None:
    cutoff = now() - JOB_TTL_SECONDS
    cleanup = JOB_REPOSITORY.expired_jobs(cutoff)
    tracked = {Path(path).name for path in cleanup["tracked"] if path}
    MEDIA_STORAGE.cleanup_stale(tracked, cutoff)
    if PRIVATE_MEDIA_STORE is not None:
        for expired_job in cleanup["expired"]:
            if expired_job.get("storage_backend") == "object" and expired_job.get("object_key"):
                PRIVATE_MEDIA_STORE.delete_object(expired_job["object_key"])
        PRIVATE_MEDIA_STORE.delete_expired_objects(now())


def cleanup_loop() -> None:
    while True:
        time.sleep(60)
        try: cleanup_once()
        except Exception: pass


def json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Drift"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(HTTP_SOCKET_TIMEOUT_SECONDS)

    def log_message(self, fmt, *args):
        # Job IDs authorize status, cancellation, and file access; never write them to logs.
        message = fmt % args
        message = re.sub(r"(/api/downloads/)[^/?\s]+", r"\1[redacted-job-id]", message)
        message = re.sub(r"(GET|POST|HEAD) ([^?\s]+)\?[^\s]+", r"\1 \2?[redacted]", message)
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {message}")

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
        allowed = os.getenv("DRIFT_ALLOWED_ORIGIN", "").strip()
        if allowed == "*" and origin:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and allowed and origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        allowed = os.getenv("DRIFT_ALLOWED_ORIGIN", "").strip()
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
            active = JOB_REPOSITORY.active_count()
            return self.send_json({"status":"ok","service":"drift-backend","downloader":yt_dlp is not None,
                                   "ffmpeg":find_ffmpeg() is not None,"active_jobs":active,
                                   "max_active_jobs":MAX_ACTIVE_JOBS,
                                   "accepting_work":accepting_new_work()})
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
        backend = job.get("storage_backend") or "local"
        object_key = job.get("object_key")
        path = None
        if job["status"] != "ready":
            return self.send_json({"success":False,"message":"The file is not ready yet."},409)
        if backend == "object":
            if not PRIVATE_MEDIA_STORE or not object_key or not isinstance(job.get("object_size"), int):
                return self.send_json({"success":False,"message":"Private media storage is temporarily unavailable."},503)
            if job.get("object_expires_at") is not None and job["object_expires_at"] <= now():
                return self.send_json({"success":False,"message":"The temporary file has expired."},410)
            size = job["object_size"]
        elif backend == "local" and job.get("file_path"):
            path = Path(job["file_path"])
            if not MEDIA_STORAGE.is_file(path):
                update_job(job_id, status="failed", phase="Failed", error="The temporary file is no longer available.", message="The temporary file is no longer available.")
                return self.send_json({"success":False,"message":"The temporary file is no longer available."},410)
            size = MEDIA_STORAGE.size(path)
        else:
            return self.send_json({"success":False,"message":"The file is not ready yet."},409)
        if size < 1 or size > MAX_DOWNLOAD_BYTES:
            return self.send_json({"success":False,"message":"The file exceeds Drift's size limit."},413)
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
            stream = (PRIVATE_MEDIA_STORE.open_file_range(object_key, start, end)
                      if backend == "object" else MEDIA_STORAGE.open_file(path))
            with stream as f:
                if backend == "local":
                    f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(1024*1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining -= len(chunk)
                if remaining:
                    raise OSError("Private media stream ended before the requested range was complete.")
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
        if not accepting_new_work():
            return self.send_json({"success":False,"message":"Drift is temporarily pausing new work. Please try again later."},503)
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
        if not accepting_new_work():
            return self.send_json({"success":False,"message":"Drift is temporarily pausing new work. Please try again later."},503)
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
             "completed_at":None,"job_dir":None,"file_path":None,"storage_backend":"local",
             "object_key":None,"object_content_type":None,"object_size":None,"object_expires_at":None,
             "duration":str(body.get("duration") or "")[:100]}
        if not insert_job_if_capacity(job):
            CANCEL_EVENTS.pop(job_id, None)
            return self.send_json({"success":False,"message":"Drift is at its current capacity. Please wait for a download to finish or for temporary files to expire."},429)
        try:
            JOB_QUEUE.enqueue(job_id)
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
        JOB_REPOSITORY.cancel_active(job_id)
        return self.send_json({"success":True,"job":public_job(get_job(job_id))})


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound concurrent HTTP handlers so connection floods cannot spawn unlimited threads."""

    def __init__(self, server_address, RequestHandlerClass):
        self.connection_slots = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


def main():
    init_db()
    recovered = recover_interrupted_jobs()
    JOB_QUEUE.start_dispatcher()
    if recovered:
        print(f"Recovered {recovered} queued or interrupted job(s) after restart.", flush=True)
    threading.Thread(target=cleanup_loop, daemon=True, name="drift-cleanup").start()
    server=LimitedThreadingHTTPServer((HOST,PORT),Handler)
    print(f"Drift backend listening on http://{HOST}:{PORT}")
    print(f"FFmpeg: {find_ffmpeg() or 'NOT FOUND'} | yt-dlp: {'ok' if yt_dlp else 'MISSING'}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        JOB_QUEUE.shutdown(wait=False, cancel_pending=True); server.server_close()


if __name__ == "__main__": main()
