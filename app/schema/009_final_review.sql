-- Final pre-deploy review (27 Sep 2026). Runs at every start: every statement is safe to re-run.

-- A do-not-contact entry made through the public privacy form carries that request's reference, so an admin sees it
-- next to the request and can undo it if the request was abusive (Settings > Privacy requests; an admin event).
ALTER TABLE outreach_suppressions ADD COLUMN IF NOT EXISTS request_ref TEXT;
CREATE INDEX IF NOT EXISTS ix_outreach_suppressions_request ON outreach_suppressions (request_ref) WHERE request_ref IS NOT NULL;

-- Erasure clears owner_id but keeps a keyed hash of the erased email (never the email) on the marks that account made,
-- so python -m app.admin unsuppress <email> can still undo misuse. Cleared with owner_id 90 days after the mark.
ALTER TABLE outreach_suppressions ADD COLUMN IF NOT EXISTS owner_hash TEXT;
CREATE INDEX IF NOT EXISTS ix_outreach_suppressions_owner_hash ON outreach_suppressions (owner_hash) WHERE owner_hash IS NOT NULL;
