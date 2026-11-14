"""Unit tests for provider-neutral PostgreSQL repository helpers."""
import unittest
from datetime import datetime, timezone

from postgres_repository import PostgresJobRepository


class PostgresRepositoryHelperTests(unittest.TestCase):
    def test_requires_connection_factory(self):
        with self.assertRaises(TypeError):
            PostgresJobRepository(None)

    def test_rejects_invalid_resource_limits(self):
        with self.assertRaises(ValueError):
            PostgresJobRepository(lambda: None, max_active_jobs=0)
        with self.assertRaises(ValueError):
            PostgresJobRepository(lambda: None, max_storage_bytes=-1)

    def test_converts_database_timestamps_to_unix_seconds(self):
        instant = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        job = PostgresJobRepository._job({
            "id": "job-1",
            "created_at": instant,
            "updated_at": instant,
            "available_at": None,
            "lease_expires_at": instant,
            "title": "Example",
        })
        self.assertEqual(job["created_at"], instant.timestamp())
        self.assertEqual(job["updated_at"], instant.timestamp())
        self.assertIsNone(job["available_at"])
        self.assertEqual(job["lease_expires_at"], instant.timestamp())
        self.assertEqual(job["title"], "Example")

    def test_timestamp_columns_use_database_timestamp_conversion(self):
        self.assertEqual(PostgresJobRepository._timestamp_expr("available_at"), "to_timestamp(%s)")
        self.assertEqual(PostgresJobRepository._timestamp_expr("url"), "%s")

    def test_update_column_allowlist_excludes_untrusted_sql_names(self):
        self.assertIn("status", PostgresJobRepository.MUTABLE_COLUMNS)
        self.assertNotIn("id", PostgresJobRepository.MUTABLE_COLUMNS)
        self.assertNotIn("status=%s; DROP TABLE drift_jobs", PostgresJobRepository.MUTABLE_COLUMNS)


if __name__ == "__main__":
    unittest.main()
