CREATE TABLE IF NOT EXISTS drive_oauth_states (
    state_hash TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    session_hash TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_drive_oauth_states_owner ON drive_oauth_states(owner_id);
CREATE INDEX IF NOT EXISTS ix_drive_oauth_states_expiry ON drive_oauth_states(expires_at);
-- One row per owner for its whole life: generation only ever increases, so a stale
-- refresh or upload from an earlier connection can never match a later one.
CREATE TABLE IF NOT EXISTS drive_connections (
    owner_id TEXT PRIMARY KEY REFERENCES users(id),
    generation INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('connected', 'reconnect_required', 'disconnected')),
    credentials TEXT,
    google_sub TEXT,
    google_email TEXT,
    scope TEXT,
    folder_id TEXT,
    connected_at DOUBLE PRECISION,
    updated DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS drive_uploads (
    owner_id TEXT NOT NULL REFERENCES users(id),
    job_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    file_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed')),
    name TEXT,
    web_view_link TEXT,
    size BIGINT,
    sharing TEXT NOT NULL DEFAULT 'private' CHECK (sharing IN ('private', 'public')),
    permission_id TEXT,
    created DOUBLE PRECISION NOT NULL,
    confirmed_at DOUBLE PRECISION,
    PRIMARY KEY (owner_id, job_id, variant)
);
