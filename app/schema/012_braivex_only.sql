-- Braivex is the only customer sign-in (04 Oct 2026). Runs at every start: every statement is safe to re-run.

-- Self-service password reset is gone with customer passwords, so its links go too. The rows held only the hash of
-- one-hour links that no route can spend any more.
DROP TABLE IF EXISTS password_resets;

-- Once, at the deploy of this release (the migrations row is the marker): every customer password hash is deleted and
-- every customer session ends, so no session made with a password before Braivex became the only sign-in survives.
-- Operators (admins) keep their break-glass password and their sessions.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM migrations WHERE name = '2026-10-04 braivex-only') THEN
        UPDATE users SET salt = '', hash = '', session_version = session_version + 1 WHERE role <> 'admin';
        INSERT INTO migrations(name, applied_at, source_digest, counts)
            VALUES ('2026-10-04 braivex-only', extract(epoch FROM now()), 'app/schema/012_braivex_only.sql', '{}'::jsonb);
    END IF;
END $$;
