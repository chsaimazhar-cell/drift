"""Structural contract tests for the API-facing job repository adapters."""
import unittest

from drift import SQLiteJobRepository
from job_repository_contract import JobRepository
from postgres_repository import PostgresJobRepository


class JobRepositoryContractTests(unittest.TestCase):
    def test_sqlite_adapter_exposes_api_repository_contract(self):
        self.assertIsInstance(SQLiteJobRepository(), JobRepository)

    def test_postgres_adapter_exposes_api_repository_contract(self):
        self.assertIsInstance(PostgresJobRepository(lambda: None), JobRepository)

    def test_contract_keeps_cancellation_result_explicit(self):
        self.assertIn("cancel_active", JobRepository.__dict__)
        self.assertIn("insert_if_capacity", JobRepository.__dict__)


if __name__ == "__main__":
    unittest.main()
