"""Apply reviewed Drift PostgreSQL SQL migrations.

Usage:
    DRIFT_DATABASE_URL='postgresql://...' python scripts/migrate_postgres.py

Migrations are applied explicitly; application startup never runs DDL.
Each migration must create/update drift_schema_migrations within its SQL file.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MIGRATION_DIR = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
MIGRATION_NAME = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")

# Stable, application-specific PostgreSQL advisory lock key.
LOCK_KEY = 0x44524946544D4947


def discover_migrations():
    migrations = []
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise RuntimeError(f"Unexpected PostgreSQL migration filename: {path.name}")
        migrations.append((int(match.group(1)), path))
    versions = [version for version, _ in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate PostgreSQL migration version detected.")
    if versions and versions != list(range(1, versions[-1] + 1)):
        raise RuntimeError("PostgreSQL migration sequence contains a gap.")
    return migrations


def apply_migrations(connection):
    migrations = discover_migrations()
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cursor.execute(
                "SELECT to_regclass('public.drift_schema_migrations') AS table_name"
            )
            exists = cursor.fetchone()[0] is not None
            current = 0
            if exists:
                cursor.execute("SELECT COALESCE(MAX(version), 0) FROM drift_schema_migrations")
                current = int(cursor.fetchone()[0])
            known = {version for version, _ in migrations}
            if current and current not in known:
                raise RuntimeError(
                    f"Database schema version {current} has no matching migration file."
                )
            for version, path in migrations:
                if version <= current:
                    continue
                if version != current + 1:
                    raise RuntimeError("Refusing to skip a PostgreSQL migration version.")
                sql = path.read_text(encoding="utf-8")
                cursor.execute(sql, prepare=False)
                cursor.execute(
                    "SELECT 1 FROM drift_schema_migrations WHERE version = %s",
                    (version,),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError(
                        f"Migration {path.name} did not record its schema version."
                    )
                current = version
    return current


def main():
    database_url = os.environ.get("DRIFT_DATABASE_URL")
    if not database_url:
        print("DRIFT_DATABASE_URL is required.", file=sys.stderr)
        return 2
    try:
        import psycopg
    except ImportError:
        print(
            "PostgreSQL migration dependency missing. Install requirements-test-postgres.txt "
            "or the approved production psycopg package.",
            file=sys.stderr,
        )
        return 2

    try:
        with psycopg.connect(database_url) as connection:
            version = apply_migrations(connection)
        print(f"Drift PostgreSQL schema is at migration version {version}.")
        return 0
    except Exception as exc:
        # Avoid printing connection strings or driver diagnostics that may contain secrets.
        print(f"PostgreSQL migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
