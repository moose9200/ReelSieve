CREATE TABLE IF NOT EXISTS migrations (
    name TEXT PRIMARY KEY,
    applied_at DOUBLE PRECISION NOT NULL,
    source_digest TEXT NOT NULL,
    counts JSONB NOT NULL
);
