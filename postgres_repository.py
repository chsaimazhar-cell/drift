"""PostgreSQL job repository adapter.

This module has no driver dependency at import time. Supply a DB-API connection
factory (for example, a psycopg connection factory) after configuring credentials.
All timestamps cross the adapter boundary as Unix seconds and are stored as
TIMESTAMPTZ in PostgreSQL.
"""
from __future__ import annotations

import os
from typing import Callable


class PostgresJobRepository:
    """Shared PostgreSQL repository; requires the migration in migrations/postgres."""

    ACTIVE_STATES = ("queued", "downloading", "processing")
    MUTABLE_COLUMNS = {
        "status", "phase", "progress", "downloaded_bytes", "total_bytes",
        "speed", "eta", "title", "thumbnail", "filename", "file_size",
        "message", "error", "completed_at", "job_dir", "file_path", "duration",
        "attempts", "available_at", "lease_owner", "lease_expires_at",
        "storage_backend", "object_key", "object_content_type", "object_size",
        "object_expires_at",
    }
    TIMESTAMP_COLUMNS = {
        "created_at", "updated_at", "completed_at", "available_at",
        "lease_expires_at", "object_expires_at",
    }

    def __init__(
        self,
        connect: Callable,
        *,
        max_active_jobs: int | None = None,
        max_queued_jobs: int | None = None,
        max_download_bytes: int | None = None,
        max_storage_bytes: int | None = None,
    ):
        if not callable(connect):
            raise TypeError("A PostgreSQL DB-API connection factory is required.")
        self.connect = connect
        self.max_active_jobs = max_active_jobs if max_active_jobs is not None else int(os.getenv("DRIFT_MAX_ACTIVE_JOBS", "2"))
        self.max_queued_jobs = max_queued_jobs if max_queued_jobs is not None else int(os.getenv("DRIFT_MAX_QUEUED_JOBS", "4"))
        self.max_download_bytes = max_download_bytes if max_download_bytes is not None else int(os.getenv("DRIFT_MAX_DOWNLOAD_MB", "500")) * 1024 * 1024
        self.max_storage_bytes = max_storage_bytes if max_storage_bytes is not None else int(os.getenv("DRIFT_MAX_STORAGE_MB", "2048")) * 1024 * 1024
        if min(self.max_active_jobs, self.max_queued_jobs, self.max_download_bytes, self.max_storage_bytes) < 0:
            raise ValueError("PostgreSQL repository limits cannot be negative.")
        if self.max_active_jobs < 1 or self.max_download_bytes < 1 or self.max_storage_bytes < 1:
            raise ValueError("Active-job, download, and storage limits must be positive.")

    @staticmethod
    def _dict_rows(cursor, rows):
        if not rows:
            return []
        names = [column.name if hasattr(column, "name") else column[0] for column in cursor.description]
        return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in rows]

    @classmethod
    def _job(cls, row):
        if row is None:
            return None
        result = dict(row)
        for key in cls.TIMESTAMP_COLUMNS:
            value = result.get(key)
            if value is not None and hasattr(value, "timestamp"):
                result[key] = value.timestamp()
        return result

    @staticmethod
    def _timestamp_expr(column):
        return f"to_timestamp(%s)" if column in PostgresJobRepository.TIMESTAMP_COLUMNS else "%s"

    def _execute(self, sql, params=(), *, fetchone=False, fetchall=False):
        conn = self.connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    if fetchone:
                        row = cursor.fetchone()
                        if row is None:
                            return None
                        return self._dict_rows(cursor, [row])[0]
                    if fetchall:
                        return self._dict_rows(cursor, cursor.fetchall())
                    return cursor.rowcount
        finally:
            conn.close()

    def initialize(self):
        """Verify that the baseline migration has been applied; never auto-migrate."""
        row = self._execute(
            "SELECT to_regclass('public.drift_jobs') AS jobs_table, "
            "to_regclass('public.drift_schema_migrations') AS migrations_table",
            fetchone=True,
        )
        if not row or not row.get("jobs_table") or not row.get("migrations_table"):
            raise RuntimeError("Drift PostgreSQL schema is missing; apply reviewed migrations first.")
        version = self._execute("SELECT MAX(version) AS version FROM drift_schema_migrations", fetchone=True)
        if not version or int(version.get("version") or 0) < 1:
            raise RuntimeError("Drift PostgreSQL migration version 1 is not recorded.")
        return True

    def get(self, job_id):
        return self._job(self._execute(
            "SELECT *, EXTRACT(EPOCH FROM created_at) AS created_at, "
            "EXTRACT(EPOCH FROM updated_at) AS updated_at, "
            "EXTRACT(EPOCH FROM completed_at) AS completed_at, "
            "EXTRACT(EPOCH FROM available_at) AS available_at, "
            "EXTRACT(EPOCH FROM lease_expires_at) AS lease_expires_at, "
            "EXTRACT(EPOCH FROM object_expires_at) AS object_expires_at "
            "FROM drift_jobs WHERE id=%s", (job_id,), fetchone=True
        ))

    def list_due_jobs(self, limit=10, timestamp=None):
        if limit < 1:
            return []
        rows = self._execute(
            "SELECT id FROM drift_jobs WHERE status='queued' AND available_at<=COALESCE(to_timestamp(%s), NOW()) "
            "ORDER BY available_at, created_at LIMIT %s",
            (timestamp, limit), fetchall=True,
        )
        return rows

    def update(self, job_id, updates):
        values = {key: value for key, value in updates.items() if key in self.MUTABLE_COLUMNS}
        if not values:
            return
        values["updated_at"] = __import__("time").time()
        assignments = []
        params = []
        for key, value in values.items():
            assignments.append(f"{key}={self._timestamp_expr(key)}")
            params.append(value)
        params.append(job_id)
        self._execute(f"UPDATE drift_jobs SET {', '.join(assignments)} WHERE id=%s", params)

    def mark_ready(self, job_id, updates):
        values = {key: value for key, value in updates.items() if key in self.MUTABLE_COLUMNS}
        values.update(status="ready", updated_at=__import__("time").time())
        assignments, params = [], []
        for key, value in values.items():
            assignments.append(f"{key}={self._timestamp_expr(key)}")
            params.append(value)
        params.append(job_id)
        return self._execute(
            f"UPDATE drift_jobs SET {', '.join(assignments)} "
            "WHERE id=%s AND status IN ('queued','downloading','processing')", params
        ) == 1

    def insert(self, job):
        data = dict(job)
        columns = list(data)
        expressions = [self._timestamp_expr(column) for column in columns]
        self._execute(
            f"INSERT INTO drift_jobs ({', '.join(columns)}) VALUES ({', '.join(expressions)})",
            [data[column] for column in columns],
        )

    def insert_if_capacity(self, job):
        """Reserve global queue/storage capacity under a transaction-scoped guard lock."""
        conn = self.connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT guard_id FROM drift_queue_guard WHERE guard_id=1 FOR UPDATE")
                    if cursor.fetchone() is None:
                        raise RuntimeError("PostgreSQL queue guard row is missing.")
                    cursor.execute(
                        "SELECT COUNT(*) FILTER (WHERE status IN ('downloading','processing')) AS active, "
                        "COUNT(*) FILTER (WHERE status='queued') AS queued, "
                        "COALESCE(SUM(file_size) FILTER (WHERE status='ready'),0) AS ready_bytes "
                        "FROM drift_jobs"
                    )
                    active, queued, ready_bytes = cursor.fetchone()
                    active = int(active or 0)
                    queued = int(queued or 0)
                    ready_bytes = int(ready_bytes or 0)
                    if active >= self.max_active_jobs or queued >= self.max_queued_jobs:
                        return False
                    reserved = (active + queued) * self.max_download_bytes * 2
                    if ready_bytes + reserved + self.max_download_bytes * 2 > self.max_storage_bytes:
                        return False
                    data = dict(job)
                    columns = list(data)
                    expressions = [self._timestamp_expr(column) for column in columns]
                    cursor.execute(
                        f"INSERT INTO drift_jobs ({', '.join(columns)}) VALUES ({', '.join(expressions)})",
                        [data[column] for column in columns],
                    )
                    return True
        finally:
            conn.close()

    def recover_interrupted(self, max_attempts=3, timestamp=None):
        """Recover all interrupted work atomically before any worker starts."""
        if max_attempts < 1:
            raise ValueError("Attempt limit must be positive.")
        conn = self.connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, status, attempts, job_dir FROM drift_jobs "
                        "WHERE status IN ('queued','downloading','processing') "
                        "ORDER BY created_at FOR UPDATE"
                    )
                    rows = self._dict_rows(cursor, cursor.fetchall())
                    for row in rows:
                        if row["status"] == "queued":
                            cursor.execute(
                                "UPDATE drift_jobs SET lease_owner=NULL, lease_expires_at=NULL "
                                "WHERE id=%s AND status='queued'",
                                (row["id"],),
                            )
                            continue
                        retryable = int(row["attempts"]) < max_attempts
                        cursor.execute(
                            "UPDATE drift_jobs SET status=%s, phase=%s, message=%s, error=%s, "
                            "available_at=COALESCE(to_timestamp(%s),NOW()), lease_owner=NULL, "
                            "lease_expires_at=NULL, updated_at=COALESCE(to_timestamp(%s),NOW()) "
                            "WHERE id=%s AND status IN ('downloading','processing')",
                            ("queued" if retryable else "failed",
                             "Queued" if retryable else "Failed",
                             "The service restarted; retry scheduled." if retryable else "The service restarted after the retry limit.",
                             None if retryable else "The service restarted after the retry limit.",
                             timestamp, timestamp, row["id"]),
                        )
                    return rows
        finally:
            conn.close()

    def expired_jobs(self, cutoff):
        """Delete terminal job records past retention and return objects/workspaces for cleanup."""
        conn = self.connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, job_dir, status, EXTRACT(EPOCH FROM updated_at) AS updated_at, "
                        "storage_backend, object_key FROM drift_jobs "
                        "WHERE updated_at < to_timestamp(%s) "
                        "AND status NOT IN ('queued','downloading','processing') FOR UPDATE",
                        (cutoff,),
                    )
                    expired = self._dict_rows(cursor, cursor.fetchall())
                    if expired:
                        cursor.execute(
                            "DELETE FROM drift_jobs WHERE id = ANY(%s)",
                            ([row["id"] for row in expired],),
                        )
                    cursor.execute("SELECT job_dir FROM drift_jobs WHERE job_dir IS NOT NULL")
                    tracked_rows = cursor.fetchall()
                    tracked = [row[0] for row in tracked_rows]
                    return {"expired": expired, "tracked": tracked}
        finally:
            conn.close()

    def claim_next_job(self, worker_id, lease_seconds=300, timestamp=None):
        """Atomically claim the oldest due job without blocking other workers."""
        if not worker_id or lease_seconds < 1:
            raise ValueError("A worker ID and positive lease duration are required.")
        row = self._execute(
            "WITH candidate AS ("
            " SELECT id FROM drift_jobs"
            " WHERE status='queued' AND available_at<=COALESCE(to_timestamp(%s),NOW())"
            " AND (lease_expires_at IS NULL OR lease_expires_at<=COALESCE(to_timestamp(%s),NOW()))"
            " ORDER BY available_at, created_at"
            " FOR UPDATE SKIP LOCKED LIMIT 1"
            ") "
            "UPDATE drift_jobs AS job SET status='downloading', phase='Claimed', lease_owner=%s, "
            "lease_expires_at=COALESCE(to_timestamp(%s),NOW()) + (%s * INTERVAL '1 second'), "
            "attempts=job.attempts+1, updated_at=COALESCE(to_timestamp(%s),NOW()) "
            "FROM candidate WHERE job.id=candidate.id "
            "RETURNING job.*, EXTRACT(EPOCH FROM job.created_at) AS created_at, "
            "EXTRACT(EPOCH FROM job.updated_at) AS updated_at, "
            "EXTRACT(EPOCH FROM job.available_at) AS available_at, "
            "EXTRACT(EPOCH FROM job.lease_expires_at) AS lease_expires_at",
            (timestamp, timestamp, worker_id, timestamp, lease_seconds, timestamp),
            fetchone=True,
        )
        return self._job(row)

    def claim_job(self, job_id, worker_id, lease_seconds=300, timestamp=None):
        if not worker_id or lease_seconds < 1:
            raise ValueError("A worker ID and positive lease duration are required.")
        row = self._execute(
            "UPDATE drift_jobs SET status='downloading', phase='Claimed', lease_owner=%s, "
            "lease_expires_at=COALESCE(to_timestamp(%s),NOW()) + (%s * INTERVAL '1 second'), "
            "attempts=attempts+1, updated_at=COALESCE(to_timestamp(%s),NOW()) "
            "WHERE id=%s AND status='queued' AND available_at<=COALESCE(to_timestamp(%s),NOW()) "
            "AND (lease_expires_at IS NULL OR lease_expires_at<=COALESCE(to_timestamp(%s),NOW())) "
            "RETURNING *, EXTRACT(EPOCH FROM created_at) AS created_at, "
            "EXTRACT(EPOCH FROM updated_at) AS updated_at, "
            "EXTRACT(EPOCH FROM available_at) AS available_at, "
            "EXTRACT(EPOCH FROM lease_expires_at) AS lease_expires_at",
            (worker_id, timestamp, lease_seconds, timestamp, job_id, timestamp, timestamp),
            fetchone=True,
        )
        return self._job(row)

    def renew_lease(self, job_id, worker_id, lease_seconds=300, timestamp=None):
        if not worker_id or lease_seconds < 1:
            raise ValueError("A worker ID and positive lease duration are required.")
        return self._execute(
            "UPDATE drift_jobs SET lease_expires_at=COALESCE(to_timestamp(%s),NOW()) + "
            "(%s * INTERVAL '1 second'), updated_at=COALESCE(to_timestamp(%s),NOW()) "
            "WHERE id=%s AND lease_owner=%s AND status IN ('downloading','processing') "
            "AND lease_expires_at>COALESCE(to_timestamp(%s),NOW())",
            (timestamp, lease_seconds, timestamp, job_id, worker_id, timestamp),
        ) == 1

    def acknowledge_job(self, job_id, worker_id, timestamp=None):
        return self._execute(
            "UPDATE drift_jobs SET lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=COALESCE(to_timestamp(%s),NOW()) WHERE id=%s AND lease_owner=%s "
            "AND status IN ('ready','failed','cancelled')",
            (timestamp, job_id, worker_id),
        ) == 1

    def retry_job(self, job_id, worker_id, delay_seconds=30, max_attempts=3, timestamp=None):
        if delay_seconds < 0 or max_attempts < 1:
            raise ValueError("Retry delay and attempt limit are invalid.")
        return self._execute(
            "UPDATE drift_jobs SET status=CASE WHEN attempts<%s THEN 'queued' ELSE 'failed' END, "
            "phase=CASE WHEN attempts<%s THEN 'Queued' ELSE 'Failed' END, "
            "message=CASE WHEN attempts<%s THEN 'Retry scheduled.' ELSE 'Retry limit reached.' END, "
            "error=CASE WHEN attempts<%s THEN NULL ELSE 'Retry limit reached.' END, "
            "available_at=COALESCE(to_timestamp(%s),NOW()) + (%s * INTERVAL '1 second'), "
            "lease_owner=NULL, lease_expires_at=NULL, updated_at=COALESCE(to_timestamp(%s),NOW()) "
            "WHERE id=%s AND lease_owner=%s AND status IN ('downloading','processing')",
            (max_attempts, max_attempts, max_attempts, max_attempts, timestamp, delay_seconds,
             timestamp, job_id, worker_id),
        ) == 1

    def recover_expired_leases(self, max_attempts=3, timestamp=None):
        if max_attempts < 1:
            raise ValueError("Attempt limit must be positive.")
        rows = self._execute(
            "UPDATE drift_jobs SET status=CASE WHEN attempts<%s THEN 'queued' ELSE 'failed' END, "
            "phase=CASE WHEN attempts<%s THEN 'Queued' ELSE 'Failed' END, "
            "message=CASE WHEN attempts<%s THEN 'Worker lease expired; retry scheduled.' "
            "ELSE 'Worker lease expired; retry limit reached.' END, "
            "error=CASE WHEN attempts<%s THEN NULL ELSE 'Worker lease expired after retry limit.' END, "
            "available_at=COALESCE(to_timestamp(%s),NOW()), lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=COALESCE(to_timestamp(%s),NOW()) WHERE status IN ('downloading','processing') "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at<=COALESCE(to_timestamp(%s),NOW()) "
            "RETURNING id, attempts",
            (max_attempts, max_attempts, max_attempts, max_attempts, timestamp, timestamp, timestamp),
            fetchall=True,
        )
        return rows

    def active_count(self):
        row = self._execute(
            "SELECT COUNT(*) AS count FROM drift_jobs WHERE status IN ('queued','downloading','processing')",
            fetchone=True,
        )
        return int(row["count"])

    def cancel_active(self, job_id):
        self._execute(
            "UPDATE drift_jobs SET status='cancelled', phase='Cancelled', "
            "message='Cancelling download…', lease_owner=NULL, lease_expires_at=NULL, updated_at=NOW() "
            "WHERE id=%s AND status IN ('queued','downloading','processing')",
            (job_id,),
        )
