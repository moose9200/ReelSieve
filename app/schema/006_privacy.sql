-- Data protection (UK GDPR, EU GDPR, India DPDP). Runs at every start: every statement is safe to re-run.

-- Orders never keep the payer's IP address. Legacy meta may not be valid JSON: anything unreadable that
-- mentions an IP is dropped whole (no code can read it anyway).
DO $$
DECLARE r RECORD; j JSONB;
BEGIN
    FOR r IN SELECT id, meta FROM orders WHERE meta LIKE '%"ip"%' LOOP
        BEGIN
            j := r.meta::jsonb;
        EXCEPTION WHEN others THEN
            j := '{}'::jsonb;
        END;
        UPDATE orders SET meta = (CASE WHEN jsonb_typeof(j) = 'object' THEN j - 'ip' ELSE '{}'::jsonb END)::text
        WHERE id = r.id;
    END LOOP;
END $$;

-- No device fingerprint is collected any more, and the free-tier guard reads network hashes from usage only.
UPDATE accounts SET ip_hash = NULL, fp_hash = NULL WHERE ip_hash IS NOT NULL OR fp_hash IS NOT NULL;
UPDATE usage SET fp_hash = NULL WHERE fp_hash IS NOT NULL;
