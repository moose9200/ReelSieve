# ReelSieve

FastAPI SaaS for property reels. Python 3.12; production on Railway.

## Scope and safety

- User approved cloud PostgreSQL, encrypted per-user Google Drive connections, disposable cloud rendering, migration and deployment on 2026-09-26.
- Never write customer credentials or durable user data to local JSON, SQLite, `.env.local`, or the developer Mac. Synthetic test data is allowed in disposable test databases/directories.
- Every customer operation must use the authenticated owner. No operator Drive or Airbnb session fallback.
- Do not delete legacy user media until cloud copies and ownership are verified and deletion is authorized.
- Do not submit paid generations or send messages as part of testing.
- Preserve existing accounts, password hashes, balances, orders and job owners during migration.
- Secrets must not appear in command output, logs, tests, commits or browser responses.

## Commands

- Tests: `TEST_DATABASE_URL=... .venv/bin/python -m pytest -q tests` (each test uses a disposable schema)
- Run: `python -m app.start` (validates config, supervises web + worker). Only the web process migrates (its lifespan, plus the optional one-time legacy import); the worker and the consoles wait for the schema, never migrate
- Legacy import rehearsal: `python -m app.migrate_cloud <dir> --dry-run` (counts only)
- Operator console: `python -m app.admin list|create-admin|set-password` (password on stdin)
- Syntax: `.venv/bin/python -m compileall -q app tests`
- Local synthetic PostgreSQL for this task: port 55439; DB `reelsieve_test`; no production data.
- Cloud DB tests get `TEST_DATABASE_URL` through environment. Use isolated test schemas and never test against public production schema.
- Production service: `ReelSieve`, Railway project `listing-reel`; use explicit service/environment flags when changing cloud state.

## Project map

- `app/server.py`: routes, sessions, customer UI.
- `app/auth.py`, `app/store.py`, `app/plans.py`, `app/billing.py`: identity and business persistence.
- `app/gdrive.py`: per-user Google OAuth and Drive delivery.
- `app/jobs.py`, `app/worker.py`: durable jobs, fenced leases, disposable render scratch, Drive delivery.
- `app/fetch.py`: public-host guard for user-influenced URLs.
- `app/migrate_cloud.py`, `app/start.py`, `app/admin.py`: legacy import, container entry point, operator console.
- `app/pipeline.py`: rendering orchestration.
- `app/templates/`, `app/static/`: existing visual identity; preserve design.

Planning, decisions and resumable state belong in `/Users/hemant/obsidian-mind/work/active/ReelSieve/`.
