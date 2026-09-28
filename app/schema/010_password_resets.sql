-- Self-service password reset (28 Sep 2026). Runs at every start: every statement is safe to re-run.

-- One row per live reset link. The link carries 32 random bytes; only their SHA-256 hash is stored, so the database
-- alone cannot reset anyone's password. A link is single use (the row is deleted when it is used) and asking again
-- deletes the account's earlier rows, so only the newest link works. Expired rows go in app/retention.py, and every
-- row of an account goes when it is erased or when its password changes. The account export shows the times only.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    created DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_password_resets_owner ON password_resets(owner_id);
CREATE INDEX IF NOT EXISTS ix_password_resets_expiry ON password_resets(expires_at);
