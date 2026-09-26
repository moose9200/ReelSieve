CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    url TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'uploading', 'done', 'failed', 'cancelled')),
    progress INTEGER NOT NULL DEFAULT 2,
    step TEXT NOT NULL DEFAULT 'Queued',
    log JSONB NOT NULL DEFAULT '[]',
    meta JSONB NOT NULL DEFAULT '{}',
    error TEXT,
    drive_generation INTEGER NOT NULL,
    lease_token TEXT,
    lease_until DOUBLE PRECISION,
    worker TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    cleanup_at DOUBLE PRECISION,
    cleanup_error TEXT,
    created DOUBLE PRECISION NOT NULL,
    updated DOUBLE PRECISION NOT NULL,
    finished_at DOUBLE PRECISION,
    UNIQUE (owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_jobs_owner ON jobs(owner_id, created DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_active ON jobs(status, created) WHERE status IN ('queued', 'running', 'uploading');
CREATE INDEX IF NOT EXISTS ix_jobs_cleanup ON jobs(finished_at) WHERE cleanup_at IS NULL;
