from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import drift
from postgres_downloader import LeaseBoundJobRepository, build_postgres_processor


class FakeRepository:
    def __init__(self):
        self.updates = []
        self.ready = []
        self.allow_update = True
        self.allow_ready = True

    def get(self, job_id):
        return {"id": job_id, "status": "downloading"}

    def update_owned(self, job_id, lease_owner, updates):
        self.updates.append((job_id, lease_owner, updates))
        return self.allow_update

    def mark_ready_owned(self, job_id, lease_owner, updates):
        self.ready.append((job_id, lease_owner, updates))
        return self.allow_ready


class LeaseBoundRepositoryTests(unittest.TestCase):
    def test_progress_updates_require_current_lease_owner(self):
        repository = FakeRepository()
        bound = LeaseBoundJobRepository(repository, "worker-a")
        bound.update("job-a", {"progress": 20})
        self.assertEqual(repository.updates[0][1], "worker-a")

        repository.allow_update = False
        with self.assertRaises(drift.WorkerLeaseLost):
            bound.update("job-a", {"progress": 30})

    def test_ready_transition_requires_current_lease_owner(self):
        repository = FakeRepository()
        bound = LeaseBoundJobRepository(repository, "worker-a")
        self.assertTrue(bound.mark_ready("job-a", {"filename": "video.mp4"}))
        repository.allow_ready = False
        with self.assertRaises(drift.WorkerLeaseLost):
            bound.mark_ready("job-a", {"filename": "stale.mp4"})

    def test_processor_passes_lease_context_and_scopes_storage(self):
        repository = FakeRepository()
        media_store = object()
        lost = threading.Event()
        job = {
            "id": "job-a", "url": "https://youtube.com/watch?v=example",
            "quality": "best", "lease_owner": "worker-a",
            "_lease_lost_event": lost,
        }
        processor = build_postgres_processor(repository, media_store=media_store)
        with patch.object(drift, "run_download") as run:
            processor(job)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["lease_lost_event"], lost)
        self.assertIsInstance(kwargs["repository"], LeaseBoundJobRepository)
        self.assertEqual(kwargs["repository"].lease_owner, "worker-a")
        self.assertIs(drift.DOWNLOAD_MEDIA_STORE_CONTEXT.get(), None)

    def test_processor_rejects_job_without_lease_owner(self):
        processor = build_postgres_processor(FakeRepository())
        with self.assertRaises(ValueError):
            processor({"id": "job-a", "url": "https://example.com", "quality": "best"})


if __name__ == "__main__":
    unittest.main()
