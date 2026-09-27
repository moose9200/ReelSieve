-- Airbnb fetch safeguards (app/airbnb.py). Runs at every start: every statement is safe to re-run.

-- One request budget per kind, shared by the web and every worker: each request reserves the next free slot.
CREATE TABLE IF NOT EXISTS airbnb_rate (bucket TEXT PRIMARY KEY, next_at DOUBLE PRECISION NOT NULL DEFAULT 0);
INSERT INTO airbnb_rate(bucket) VALUES ('page'), ('image') ON CONFLICT (bucket) DO NOTHING;

-- The last time Airbnb refused us; every process pauses Airbnb fetching for AIRBNB_BLOCK_COOLDOWN_MIN after it.
-- Host and status only: never a listing, a search or a person.
CREATE TABLE IF NOT EXISTS airbnb_blocks (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    ts DOUBLE PRECISION NOT NULL,
    status INTEGER NOT NULL,
    host TEXT NOT NULL,
    reason TEXT NOT NULL
);
