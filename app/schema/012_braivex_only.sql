-- Braivex is the only customer sign-in (04 Oct 2026). Ledgered (app/database.py): runs once, at the first start of
-- this release, and never again.

-- Every customer session ends, so none made with a password before Braivex became the only sign-in survives.
-- Operators (admins) keep their break-glass password and their sessions. Customer password hashes are wiped at every
-- start by 011_braivex_sso.sql. password_resets is no longer read or written, but it is not dropped here: the previous
-- build keeps serving during a rolling deploy and still uses it. A later release drops it.
UPDATE users SET session_version = session_version + 1 WHERE role <> 'admin';

-- Controller ruling, 04 Oct 2026: accounts that already exist on our own domains are our own people. Password sign-up
-- never proved any mailbox, but these are trusted for linking: their first Braivex sign-in links them with no Claim
-- step and keeps their orders and invites. Like every older account's first link, it still ends their sessions, wipes
-- the password and drops the Google Drive connection and business sender (app/auth.py link_braivex): a domain does not
-- prove the row was never squatted. Every other existing account takes the Claim path. Only rows that exist now.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_trusted BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE users SET email_trusted = TRUE
    WHERE split_part(email, '@', 2) IN ('braivex.com', 'wbj.team', 'mokshabotanicals.in');
