-- Airbnb fetch safeguards (app/airbnb.py) and listing takedowns. Runs at every start: every statement is safe to re-run.

-- One request budget per kind (pages, photos, the reviews browser's scripts and data calls), shared by the web and
-- every worker: each request reserves the next free slot.
CREATE TABLE IF NOT EXISTS airbnb_rate (bucket TEXT PRIMARY KEY, next_at DOUBLE PRECISION NOT NULL DEFAULT 0);
INSERT INTO airbnb_rate(bucket) VALUES ('page'), ('image'), ('browser') ON CONFLICT (bucket) DO NOTHING;

-- The last time Airbnb refused us; every process pauses Airbnb fetching for AIRBNB_BLOCK_COOLDOWN_MIN after it.
-- Host and status only: never a listing, a search or a person.
CREATE TABLE IF NOT EXISTS airbnb_blocks (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    ts DOUBLE PRECISION NOT NULL,
    status INTEGER NOT NULL,
    host TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- Listings nobody may make a reel of (a host's takedown request, or an admin). Kept until an admin removes it.
CREATE TABLE IF NOT EXISTS blocked_listings (
    listing_id TEXT PRIMARY KEY CHECK (listing_id ~ '^[0-9]{1,20}$'),
    reason TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL
);

-- The listing a privacy request is about ("Remove my listing from ReelSieve").
ALTER TABLE privacy_requests ADD COLUMN IF NOT EXISTS listing_id TEXT;
