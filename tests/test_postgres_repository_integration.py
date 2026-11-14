"""Live PostgreSQL repository integration tests.

These tests run only when DRIFT_TEST_DATABASE_URL is configured. CI supplies an
isolated PostgreSQL service; local unit-test runs skip this module's test cases.
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATABASE_URL = os.getenv("DRIFT_TEST_DATABASE_URL")

try:
    import psycopg
except ImportError:  # Keep the production runtime driver-optional.
    psycopg = None

from postgres_repository import PostgresJobRepository
from scripts.migrate_postgres import apply_migrations


@unittest.skipUnless(DATABASE_URL and psycopg, "PostgreSQL integration environment is not configured")
class PostgresRepositoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connect = staticmethod(lambda: psycopg.connect(DATABASE_URL))
        with cls.connect() as conn:
            cls.schema_version = apply_migrations(conn)
        cls.repo = PostgresJobRepository(
            cls.connect,
            max_active_jobs=2,
            max_queued_jobs=4,
            max_download_bytes=100,
            max_storage_bytes=10000,
        )
        cls.repo.initialize()

    def setUp(self):
        with self.connect() as conn:
            conn.execute("TRUNCATE drift_jobs")

    def make_job(self, **overrides):
        timestamp = 1_800_000_000.0
        job = {
            "id": uuid.uuid4().hex,
            "status": "queued",
            "phase": "Queued",
            "url": "https://example.com/video",
            "quality": "best",
            "format": "MP4",
            "platform": "Example",
            "created_at": timestamp,
            "updated_at": timestamp,
            "available_at": timestamp,
        }
        job.update(overrides)
        return job

    def test_migration_runner_is_idempotent_and_insert_get(self):
        with self.connect() as conn:
            self.assertEqual(apply_migrations(conn), self.schema_version)
        self.assertGreaterEqual(self.schema_version, 1)
        job = self.make_job()
        self.repo.insert(job)
        found = self.repo.get(job["id"])
        self.assertEqual(found["id"], job["id"])
        self.assertEqual(found["status"], "queued")
        self.assertAlmostEqual(found["created_at"], job["created_at"], places=3)

    def test_due_jobs_claim_lease_and_retry(self):
        job = self.make_job()
        self.repo.insert(job)
        due = self.repo.list_due_jobs(timestamp=job["available_at"] + 1)
        self.assertEqual([row["id"] for row in due], [job["id"]])

        claimed = self.repo.claim_job(
            job["id"], "worker-a", lease_seconds=60, timestamp=job["available_at"] + 1
        )
        self.assertEqual(claimed["status"], "downloading")
        self.assertEqual(claimed["lease_owner"], "worker-a")
        self.assertEqual(claimed["attempts"], 1)
        self.assertTrue(self.repo.renew_lease(
            job["id"], "worker-a", lease_seconds=90, timestamp=job["available_at"] + 2
        ))
        self.assertTrue(self.repo.retry_job(
            job["id"], "worker-a", delay_seconds=0, max_attempts=3,
            timestamp=job["available_at"] + 3,
        ))
        self.assertEqual(self.repo.get(job["id"])["status"], "queued")

    def test_startup_recovery_and_terminal_expiry(self):
        queued = self.make_job(
            lease_owner="stale-worker",
            lease_expires_at=1_800_000_050.0,
            available_at=1_800_000_500.0,
        )
        interrupted = self.make_job(status="processing", attempts=1, job_dir="/tmp/drift-test")
        exhausted = self.make_job(status="downloading", attempts=3, job_dir="/tmp/drift-exhausted")
        expired = self.make_job(status="failed", updated_at=1.0, job_dir="/tmp/drift-expired")
        self.repo.insert(queued)
        self.repo.insert(interrupted)
        self.repo.insert(exhausted)
        self.repo.insert(expired)

        recovered = self.repo.recover_interrupted(max_attempts=3, timestamp=1_800_000_100.0)
        self.assertEqual(
            {row["id"] for row in recovered},
            {queued["id"], interrupted["id"], exhausted["id"]},
        )
        self.assertEqual(self.repo.get(interrupted["id"])["status"], "queued")
        recovered_queued = self.repo.get(queued["id"])
        self.assertEqual(recovered_queued["status"], "queued")
        self.assertIsNone(recovered_queued["lease_owner"])
        self.assertIsNone(recovered_queued["lease_expires_at"])
        self.assertAlmostEqual(recovered_queued["available_at"], queued["available_at"], places=3)
        self.assertEqual(self.repo.get(exhausted["id"])["status"], "failed")

        cleanup = self.repo.expired_jobs(cutoff=2.0)
        self.assertIn(expired["id"], {row["id"] for row in cleanup["expired"]})
        self.assertIsNone(self.repo.get(expired["id"]))

    def test_claim_next_job_is_safe_across_concurrent_workers(self):
        jobs = [self.make_job(available_at=1.0) for _ in range(8)]
        for job in jobs:
            self.repo.insert(job)
        barrier = threading.Barrier(8)

        def claim(index):
            barrier.wait(timeout=10)
            return self.repo.claim_next_job(
                f"worker-{index}", lease_seconds=60, timestamp=1_800_000_100.0
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(claim, range(8)))
        claimed_ids = [job["id"] for job in claimed if job]
        self.assertEqual(len(claimed_ids), 8)
        self.assertEqual(len(set(claimed_ids)), 8)
        self.assertEqual(
            {job["status"] for job in claimed if job}, {"downloading"}
        )
        self.assertEqual(self.repo.active_count(), 8)

    def test_capacity_admission_is_serialized_across_connections(self):
        barrier = threading.Barrier(8)

        def attempt(_):
            job = self.make_job()
            barrier.wait(timeout=10)
            return self.repo.insert_if_capacity(job)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(outcomes), 4)
        self.assertEqual(self.repo.active_count(), 4)

    def test_cancel_active_and_acknowledge_terminal_job(self):
        job = self.make_job(
            status="downloading", lease_owner="worker-cancel",
            lease_expires_at=1_800_000_300.0,
        )
        self.repo.insert(job)
        self.assertEqual(self.repo.active_count(), 1)
        self.repo.cancel_active(job["id"])
        cancelled = self.repo.get(job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["lease_owner"])
        self.assertIsNone(cancelled["lease_expires_at"])
        self.assertEqual(self.repo.active_count(), 0)

    def test_expired_worker_leases_are_recovered_atomically(self):
        retryable = self.make_job(
            status="processing", attempts=1, lease_owner="dead-worker-a",
            lease_expires_at=1_800_000_050.0,
        )
        exhausted = self.make_job(
            status="downloading", attempts=3, lease_owner="dead-worker-b",
            lease_expires_at=1_800_000_060.0,
        )
        self.repo.insert(retryable)
        self.repo.insert(exhausted)

        recovered = self.repo.recover_expired_leases(
            max_attempts=3, timestamp=1_800_000_100.0
        )
        self.assertEqual({row["id"] for row in recovered}, {retryable["id"], exhausted["id"]})
        retry_job = self.repo.get(retryable["id"])
        self.assertEqual(retry_job["status"], "queued")
        self.assertIsNone(retry_job["lease_owner"])
        self.assertIsNone(retry_job["lease_expires_at"])
        failed_job = self.repo.get(exhausted["id"])
        self.assertEqual(failed_job["status"], "failed")
        self.assertIsNone(failed_job["lease_owner"])
        self.assertIsNone(failed_job["lease_expires_at"])

    def test_mark_ready_only_transitions_active_jobs(self):
        job = self.make_job()
        self.repo.insert(job)
        self.assertTrue(self.repo.mark_ready(job["id"], {"file_size": 42, "filename": "clip.mp4"}))
        ready = self.repo.get(job["id"])
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["file_size"], 42)
        self.assertFalse(self.repo.mark_ready(job["id"], {"file_size": 99}))
        self.assertEqual(self.repo.get(job["id"])["file_size"], 42)


if __name__ == "__main__":
    unittest.main()
