-- Business-to-business outreach: accountability and the account's own sender details. Runs at every start; safe to re-run.

-- Which account marked a prospect "Do not contact" (NULL: the privacy request form, or the link has expired). Kept so
-- misuse can be traced and undone (python -m app.admin unsuppress <email>) and capped per day (store.suppress).
-- Set to NULL on account erasure and 90 days after the mark (app/retention.py); the suppression itself stays.
ALTER TABLE outreach_suppressions ADD COLUMN IF NOT EXISTS owner_id TEXT REFERENCES users(id);
CREATE INDEX IF NOT EXISTS ix_outreach_suppressions_owner ON outreach_suppressions (owner_id, ts) WHERE owner_id IS NOT NULL;

-- A company objecting through the public privacy request form gives its Companies House number.
ALTER TABLE privacy_requests ADD COLUMN IF NOT EXISTS company_number TEXT;

-- The name, business name and reply email the account last put on a business email: {"name", "business", "email"}.
-- Kept on the account (never in the browser), exported with it and cleared on erasure (app/admin.py).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS b2b_sender JSONB;
