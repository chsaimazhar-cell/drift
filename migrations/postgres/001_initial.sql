-- Drift PostgreSQL baseline schema.
-- Apply this file in a single transaction using a dedicated migration runner.
-- This schema is preparation only; no PostgreSQL driver or provider is configured.

CREATE TABLE IF NOT EXISTS drift_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS drift_queue_guard (
    guard_id SMALLINT PRIMARY KEY CHECK (guard_id = 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO drift_queue_guard (guard_id)
VALUES (1)
ON CONFLICT (guard_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS drift_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'downloading', 'processing', 'ready', 'failed', 'cancelled')
    ),
    phase TEXT NOT NULL,
    progress DOUBLE PRECISION,
    downloaded_bytes BIGINT NOT NULL DEFAULT 0 CHECK (downloaded_bytes >= 0),
    total_bytes BIGINT NOT NULL DEFAULT 0 CHECK (total_bytes >= 0),
    speed DOUBLE PRECISION,
    eta DOUBLE PRECISION,
    url TEXT NOT NULL,
    quality TEXT NOT NULL,
    format TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT,
    thumbnail TEXT,
    filename TEXT,
    file_size BIGINT NOT NULL DEFAULT 0 CHECK (file_size >= 0),
    message TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    job_dir TEXT,
    file_path TEXT,
    duration TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    storage_backend TEXT NOT NULL DEFAULT 'local'
        CHECK (storage_backend IN ('local', 'object')),
    object_key TEXT,
    object_content_type TEXT,
    object_size BIGINT CHECK (object_size IS NULL OR object_size >= 0),
    object_expires_at TIMESTAMPTZ,
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        storage_backend <> 'object'
        OR object_key IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_drift_jobs_due
    ON drift_jobs (available_at, created_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_drift_jobs_expired_leases
    ON drift_jobs (lease_expires_at, created_at)
    WHERE status IN ('downloading', 'processing');

CREATE INDEX IF NOT EXISTS idx_drift_jobs_updated
    ON drift_jobs (updated_at);

CREATE INDEX IF NOT EXISTS idx_drift_jobs_object_expiry
    ON drift_jobs (object_expires_at)
    WHERE storage_backend = 'object';

-- Worker claim transaction pattern (execute in a transaction):
--   SELECT id FROM drift_jobs
--   WHERE status = 'queued' AND available_at <= NOW()
--   ORDER BY available_at, created_at
--   LIMIT 1 FOR UPDATE SKIP LOCKED;
-- Then update that row to status='downloading', set lease_owner and
-- lease_expires_at, increment attempts, and commit in the same transaction.
--
-- API capacity reservation pattern (execute in a transaction):
--   SELECT guard_id FROM drift_queue_guard WHERE guard_id = 1 FOR UPDATE;
-- After acquiring the singleton guard lock, count active jobs and ready bytes,
-- apply configured global limits, insert the job, and commit. This serializes
-- admission decisions across API replicas; do not rely on process-local counts.
--
-- Store timestamps as TIMESTAMPTZ in PostgreSQL. The Python adapter must convert
-- them to/from the existing Unix-second values at the application boundary.
-- Local workspace paths are diagnostic/ephemeral only and must never be treated
-- as durable media storage in a production deployment.

INSERT INTO drift_schema_migrations (version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;
