-- Security review 28 Sep 2026 (F2). Runs at every start: every statement is safe to re-run.

-- A privacy request sent while signed in belongs to that account. "Download my data" reads this column, never the
-- typed email address: anyone can type someone else's address into the public form, and used to see their request.
-- NULL for requests sent by people with no account (the admins still answer them in Settings).
ALTER TABLE privacy_requests ADD COLUMN IF NOT EXISTS owner_id TEXT REFERENCES users(id);
CREATE INDEX IF NOT EXISTS ix_privacy_requests_owner ON privacy_requests(owner_id) WHERE owner_id IS NOT NULL;
