-- UK property companies for Outreach (business to business), from Companies House's Free Company Data Product.
-- Company-level data only, and only what search needs: never officers, people, street address, "care of" names, the
-- full postcode (its outward code, e.g. BH1, is enough), country or incorporation date (app/companies.py).
-- suppression_key: the keyed hash of 'company:<number>' (store.suppression_keys), worked out in Python at ingest, so
-- search anti-joins outreach_suppressions on an index and the key itself never reaches the database.
-- Every monthly snapshot upserts its rows and deletes companies absent from it. Runs at every start; safe to re-run.
CREATE TABLE IF NOT EXISTS companies (
    company_number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    town TEXT,
    postcode_district TEXT,
    sic_codes TEXT[] NOT NULL,
    snapshot TEXT NOT NULL,
    suppression_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_companies_town ON companies (upper(town), name);
CREATE INDEX IF NOT EXISTS ix_companies_district ON companies (postcode_district text_pattern_ops);
CREATE INDEX IF NOT EXISTS ix_companies_sic ON companies USING GIN (sic_codes);
CREATE INDEX IF NOT EXISTS ix_companies_name ON companies (name, company_number);
CREATE INDEX IF NOT EXISTS ix_companies_suppression_key ON companies (suppression_key);
CREATE INDEX IF NOT EXISTS ix_companies_unkeyed ON companies (company_number) WHERE suppression_key IS NULL;

-- Small operational facts, e.g. key 'companies': the last snapshot ingested and when.
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value JSONB NOT NULL);
