-- Invite programme guards against inviting yourself (app/referrals.py). Runs at every start: safe to re-run.

-- Keyed hash of the network (/24 or /64, never the address) of each sign-up or sign-in: one row per network, with
-- the latest time. Used only for the self-invite check, because paid videos keep no network hash. Deleted after
-- 90 days (store.purge_signals) and on erasure; in the account export.
CREATE TABLE IF NOT EXISTS signin_networks (
    owner_id TEXT NOT NULL REFERENCES users(id),
    ip_hash TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (owner_id, ip_hash)
);
CREATE INDEX IF NOT EXISTS ix_signin_networks_ts ON signin_networks(ts);

-- Keyed hash of the Google account a rewarded invited account delivered to: one Google account earns one invite
-- reward. Cleared with the invited side on erasure; goes with the row under retention.
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS google_hash TEXT;
CREATE INDEX IF NOT EXISTS ix_referrals_google ON referrals(google_hash) WHERE google_hash IS NOT NULL;
