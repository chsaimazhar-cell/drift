"""Shared PostgreSQL worker loop.

The runtime is provider-neutral: callers supply a PostgreSQL repository and a
processor. It claims work with SKIP LOCKED, renews leases while processing,
retries failures, and acknowledges terminal jobs. It does not start an API
server or configure credentials.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable


logger = logging.getLogger(__name__)
TERMINAL_STATES = {"ready", "failed", "cancelled"}


class PostgresWorker:
    """One independently runnable worker backed by PostgresJobRepository."""

    def __init__(
        self,
        repository,
        processor: Callable[[dict], None],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        poll_seconds: float = 1.0,
        retry_delay_seconds: int = 30,
        max_attempts: int = 3,
        recovery_interval_seconds: float = 30.0,
    ):
        if not callable(processor):
            raise TypeError("A job processor callback is required.")
        if lease_seconds < 3 or poll_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("Worker timing configuration is invalid.")
        if max_attempts < 1 or recovery_interval_seconds <= 0:
            raise ValueError("Worker retry and recovery configuration is invalid.")
        self.repository = repository
        self.processor = processor
        self.worker_id = worker_id or f"drift-worker-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.recovery_interval_seconds = recovery_interval_seconds
        self._stop = threading.Event()
        self._last_recovery = 0.0

    def _heartbeat(
        self, job_id: str, finished: threading.Event, lease_lost: threading.Event
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not finished.wait(interval):
            try:
                if not self.repository.renew_lease(
                    job_id, self.worker_id, lease_seconds=self.lease_seconds
                ):
                    lease_lost.set()
                    logger.warning("Worker lease renewal stopped for job %s", job_id[:8])
                    return
            except Exception:
                lease_lost.set()
                logger.exception("Worker lease renewal failed for job %s", job_id[:8])
                return

    def run_once(self) -> bool:
        """Claim and process at most one due job. Return False when none is due."""
        job = self.repository.claim_next_job(
            self.worker_id, lease_seconds=self.lease_seconds
        )
        if job is None:
            return False

        job_id = job["id"]
        finished = threading.Event()
        lease_lost = threading.Event()
        # This private, in-process signal lets a cooperative processor stop
        # external work if another worker may have reclaimed the job.
        job["_lease_lost_event"] = lease_lost
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(job_id, finished, lease_lost),
            daemon=True, name=f"drift-heartbeat-{job_id[:8]}",
        )
        heartbeat.start()
        try:
            self.processor(job)
        except Exception:
            logger.exception("Worker processing failed for job %s", job_id[:8])
            if not lease_lost.is_set():
                try:
                    self.repository.retry_job(
                        job_id, self.worker_id,
                        delay_seconds=self.retry_delay_seconds,
                        max_attempts=self.max_attempts,
                    )
                except Exception:
                    logger.exception("Could not schedule retry for job %s", job_id[:8])
            return True
        finally:
            finished.set()
            heartbeat.join(timeout=2)

        if lease_lost.is_set():
            logger.warning("Skipping completion update after lease loss for job %s", job_id[:8])
            return True

        current = self.repository.get(job_id)
        if current and current.get("status") in TERMINAL_STATES:
            self.repository.acknowledge_job(job_id, self.worker_id)
        else:
            # A processor must persist a terminal state before returning.
            self.repository.retry_job(
                job_id, self.worker_id,
                delay_seconds=self.retry_delay_seconds,
                max_attempts=self.max_attempts,
            )
        return True

    def run_forever(self) -> None:
        """Poll until stop() is called; expired leases are recovered periodically."""
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_recovery >= self.recovery_interval_seconds:
                try:
                    self.repository.recover_expired_leases(max_attempts=self.max_attempts)
                except Exception:
                    logger.exception("Expired worker lease recovery failed")
                self._last_recovery = now
            try:
                if not self.run_once():
                    self._stop.wait(self.poll_seconds)
            except Exception:
                logger.exception("Worker poll failed")
                self._stop.wait(self.poll_seconds)

    def stop(self) -> None:
        """Stop polling. The active processor is allowed to finish its current call."""
        self._stop.set()
