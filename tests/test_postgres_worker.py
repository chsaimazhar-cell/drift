"""Unit tests for the standalone PostgreSQL worker runtime."""
import unittest

from postgres_worker import PostgresWorker


class FakeRepository:
    def __init__(self, job=None):
        self.job = job or {
            "id": "job-12345678", "url": "https://example.com/video",
            "quality": "best", "status": "downloading",
        }
        self.claimed = False
        self.acknowledged = []
        self.retries = []
        self.recoveries = 0
        self.renewals = 0
        self.lose_lease = False

    def claim_next_job(self, worker_id, lease_seconds=300):
        if self.claimed:
            return None
        self.claimed = True
        return dict(self.job)

    def renew_lease(self, job_id, worker_id, lease_seconds=300):
        self.renewals += 1
        return not self.lose_lease

    def get(self, job_id):
        return dict(self.job)

    def acknowledge_job(self, job_id, worker_id):
        self.acknowledged.append((job_id, worker_id))
        return True

    def retry_job(self, job_id, worker_id, delay_seconds=30, max_attempts=3):
        self.retries.append((job_id, worker_id, delay_seconds, max_attempts))
        return True

    def recover_expired_leases(self, max_attempts=3):
        self.recoveries += 1
        return []


class PostgresWorkerTests(unittest.TestCase):
    def test_claims_processes_and_acknowledges_terminal_job(self):
        repo = FakeRepository()
        processed = []

        def process(job):
            processed.append(job["id"])
            repo.job["status"] = "ready"

        worker = PostgresWorker(repo, process, worker_id="worker-test")
        self.assertTrue(worker.run_once())
        self.assertEqual(processed, ["job-12345678"])
        self.assertEqual(repo.acknowledged, [("job-12345678", "worker-test")])
        self.assertEqual(repo.retries, [])

    def test_processing_exception_schedules_retry(self):
        repo = FakeRepository()

        def process(_job):
            raise RuntimeError("simulated processor failure")

        worker = PostgresWorker(
            repo, process, worker_id="worker-test",
            retry_delay_seconds=7, max_attempts=4,
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(
            repo.retries, [("job-12345678", "worker-test", 7, 4)]
        )
        self.assertEqual(repo.acknowledged, [])

    def test_nonterminal_processor_result_is_retried(self):
        repo = FakeRepository()
        worker = PostgresWorker(repo, lambda _job: None, worker_id="worker-test")
        self.assertTrue(worker.run_once())
        self.assertEqual(len(repo.retries), 1)
        self.assertEqual(repo.acknowledged, [])

    def test_lease_loss_is_exposed_and_prevents_acknowledgement(self):
        repo = FakeRepository()
        repo.lose_lease = True
        observed = []

        def process(job):
            lost = job["_lease_lost_event"]
            observed.append(lost.wait(timeout=2))

        worker = PostgresWorker(
            repo, process, worker_id="worker-test", lease_seconds=3
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(observed, [True])
        self.assertEqual(repo.acknowledged, [])
        self.assertEqual(repo.retries, [])

    def test_returns_false_when_no_job_is_due(self):
        repo = FakeRepository()
        repo.claim_next_job = lambda *_args, **_kwargs: None
        worker = PostgresWorker(repo, lambda _job: None)
        self.assertFalse(worker.run_once())


if __name__ == "__main__":
    unittest.main()
