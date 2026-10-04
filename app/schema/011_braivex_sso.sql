-- "Continue with Braivex" (29 Sep 2026). Runs at every start: every statement is safe to re-run.

-- The Shopify customer GID (gid://shopify/Customer/...) the assertion carries as `sub`. It is the stable account
-- identity: an email can change, this cannot. Nullable, because every account created before Braivex sign-in has
-- none, and unique, because one Shopify customer is one ReelSieve account. Cleared when the account is erased,
-- which also frees it for a new sign-up.
ALTER TABLE users ADD COLUMN IF NOT EXISTS braivex_customer_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_braivex_customer ON users(braivex_customer_id) WHERE braivex_customer_id IS NOT NULL;

-- One row per spent assertion. A Braivex assertion is a bearer token for 120 seconds and nothing in the signature
-- can detect reuse, so the `jti` is remembered for the contract's 10 minutes and a repeat is refused. The row holds
-- no personal data: the identifier is minted per assertion by accounts.braivex.com. Expired rows go in
-- app/retention.py, and the primary key is what makes the single use atomic across web replicas.
CREATE TABLE IF NOT EXISTS braivex_sso_jti (
    jti TEXT PRIMARY KEY,
    seen DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_braivex_sso_jti_expiry ON braivex_sso_jti(expires_at);

-- 04 Oct 2026: customers have no password. Any customer hash is wiped at every start, including one written by an
-- older build still serving during a rolling deploy, or after a rollback. Operators (admins) keep theirs.
UPDATE users SET salt = '', hash = '' WHERE role <> 'admin' AND hash <> '';
