"""Lease-fenced adapter connecting the existing downloader to PostgreSQL workers.

This is an integration boundary, not a production entrypoint. The API must
use the same PostgreSQL repository and private object store before deployment.
"""
from __future__ import annotations

import drift


class LeaseBoundJobRepository:
    """Expose only job writes fenced by the worker's lease owner."""

    def __init__(self, repository, lease_owner: str):
        if not lease_owner:
            raise ValueError("A worker lease owner is required.")
        self.repository = repository
        self.lease_owner = lease_owner

    def get(self, job_id):
        return self.repository.get(job_id)

    def update(self, job_id, updates):
        if not self.repository.update_owned(job_id, self.lease_owner, updates):
            raise drift.WorkerLeaseLost(
                "The worker no longer owns this job; refusing to write progress."
            )

    def mark_ready(self, job_id, updates):
        if not self.repository.mark_ready_owned(job_id, self.lease_owner, updates):
            raise drift.WorkerLeaseLost(
                "The worker no longer owns this job; refusing to publish its artifact."
            )
        return True


def build_postgres_processor(repository, *, media_store=None):
    """Create a PostgresWorker callback for the current downloader implementation."""
    if repository is None:
        raise TypeError("A PostgreSQL job repository is required.")

    def process(job):
        job_id = job["id"]
        lease_owner = job.get("lease_owner")
        if not lease_owner:
            raise ValueError("Claimed job is missing its lease owner.")
        media_store_token = (
            drift.DOWNLOAD_MEDIA_STORE_CONTEXT.set(media_store)
            if media_store is not None else None
        )
        try:
            drift.run_download(
                job_id,
                job["url"],
                job["quality"],
                lease_lost_event=job.get("_lease_lost_event"),
                repository=LeaseBoundJobRepository(repository, lease_owner),
            )
        finally:
            if media_store_token is not None:
                drift.DOWNLOAD_MEDIA_STORE_CONTEXT.reset(media_store_token)

    return process
