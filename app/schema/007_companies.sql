-- UK property companies for Outreach (business to business), from Companies House's Free Company Data Product.
-- Company-level data only: never officers, people, street address or "care of" names (app/companies.py).
-- Every monthly snapshot upserts its rows and deletes companies absent from it. Runs at every start; safe to re-run.
CREATE TABLE IF NOT EXISTS companies (
    company_number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    town TEXT,
    postcode TEXT,
    country TEXT,
    sic_codes TEXT[] NOT NULL,
    incorporated DATE,
    snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_companies_town ON companies (upper(town), name);
CREATE INDEX IF NOT EXISTS ix_companies_postcode ON companies (postcode text_pattern_ops);
CREATE INDEX IF NOT EXISTS ix_companies_sic ON companies USING GIN (sic_codes);
CREATE INDEX IF NOT EXISTS ix_companies_name ON companies (name, company_number);

-- Small operational facts, e.g. key 'companies': the last snapshot ingested and when.
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value JSONB NOT NULL);
