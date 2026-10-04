# ReelSieve — by Braivex

Paste an Airbnb listing URL → a cinematic reel (intro, trust card, depth-parallax walkthrough, real review card, outro "by Braivex.com") delivered **privately to the customer's own Google Drive**, plus a link-free message the customer sends to the host from their own Airbnb inbox.

## Architecture (cloud only)
- **Web** (`app/server.py`, FastAPI): accounts, jobs, billing, outreach tracker, Drive connection. Writes nothing to local disk.
- **PostgreSQL**: identities, sessions, plans/usage, orders, outreach, jobs, encrypted Drive credentials, OAuth state, Drive delivery receipts. Schema in `app/schema/*.sql`, applied at start.
- **Worker** (`app/worker.py`): claims jobs (`SKIP LOCKED`, fenced leases), renders each in its own process group under `RENDER_TMP_DIR`, uploads every output to the owner's Drive, deletes the scratch. Interrupted jobs are failed and refunded, never re-run.
- **Google Drive**: each account connects its own Google account (`drive.file` scope). Reels are private until the owner creates a public link on the job page.
- `python -m app.start` validates config, applies the schema, optionally runs the one-time legacy import, then supervises web + worker.

## Run locally
```bash
uv venv .venv --python 3.12
VIRTUAL_ENV=$PWD/.venv uv pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env   # fill in a local PostgreSQL URL and generated secrets; never commit it
set -a; source .env; set +a
.venv/bin/python -m app.start
```
There is no SQLite/JSON fallback: without `DATABASE_URL`, `SESSION_SECRET` and a Fernet `TOKEN_ENCRYPTION_KEY` the app refuses to start.

## Tests
```bash
TEST_DATABASE_URL=postgresql://user@127.0.0.1:5432/reelsieve_test .venv/bin/python -m pytest -q tests
```
Each test gets its own disposable PostgreSQL schema. Google is an HTTPX mock; renders are tiny fake child processes. No test calls a paid provider or sends a message.

## Configuration
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (Railway: the private URL). Required. |
| `DATABASE_SCHEMA` | Optional schema, e.g. `reelsieve_staging` for an isolated staging service. |
| `SESSION_SECRET` | Signs sessions and CSRF tokens; also keys network hashes. Required. The do-not-contact key was derived from it once and is now stored encrypted in `app_meta`, so rotating it never voids an objection. |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for Drive credentials, separate from the session secret. Required. `TOKEN_ENCRYPTION_OLD_KEYS` (comma-separated) keeps old keys readable during rotation. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Web-application OAuth client customers connect their Drive through. |
| `PUBLIC_BASE_URL` | Public address (OAuth redirect and payment return links). Falls back to `RAILWAY_PUBLIC_DOMAIN`. |
| `BRAIVEX_ACCOUNTS_URL` | Optional. Issuer of Continue with Braivex, the only customer sign-in (default `https://accounts.braivex.com`). The callback registered there for `reelsieve` must equal `<PUBLIC_BASE_URL>/auth/braivex/callback`. |
| `WORKER_ENABLED` | `1` (default) runs the render worker in the container; `0` for web-only staging. |
| `RENDER_TMP_DIR` | Disposable scratch for renders (default `/tmp/reelsieve`). |
| `HF_KEY` | Higgsfield `key-id:key-secret` — enables AI camera motion (billable). |
| `CHECKOUT_STARTER`, `CHECKOUT_COMMERCIAL`, `BILLING_WEBHOOK_SECRET`, `BILLING_NOTE` | Payments without Stripe: reusable payment links, generic signed webhook, invoice note. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Card payments on hosted Stripe Checkout. Both must be set; otherwise the plans page falls back to invoice / payment link. Webhook endpoint `<PUBLIC_BASE_URL>/api/billing/webhook/stripe`, events `checkout.session.completed` and `checkout.session.async_payment_succeeded`. |
| `GDRIVE_FOLDER`, `DEFAULT_MESSAGE` | Drive folder name; default host message. |
| `LEGACY_MIGRATION_ENABLED`, `LEGACY_SOURCE_DIR` | One-time import of the old volume (see below). |
| `INVOICE_BACKUP_BUCKET`, `INVOICE_BACKUP_REGION`, `INVOICE_BACKUP_ACCESS_KEY_ID`, `INVOICE_BACKUP_SECRET_ACCESS_KEY` (optional `INVOICE_BACKUP_ENDPOINT`, `INVOICE_BACKUP_ENDPOINT_IN_INDIA`) | Worker service only: daily India backup of paid orders (see below). Off until bucket and both keys are set. |

Settings in the app are read-only; change values in the hosting environment and redeploy.

## Google Drive OAuth
Google Cloud Console → Credentials → OAuth client (Web application). Authorised redirect URI: `<PUBLIC_BASE_URL>/oauth/google/callback` (Settings shows the exact value). Scopes: `drive.file`, `openid`, `email`. The consent screen must be published for external users before customers outside the test-user list can connect.

Security properties: OAuth state is one-time, expires in 10 minutes and only completes in the session that started it; the granted Drive scope and Google identity are checked before a connection is stored; credentials are encrypted and bound to their owner; disconnect disables access first and reports a failed revocation instead of hiding it.

## India invoice backup (Income-tax Rules 2026 r.46(8))
The worker uploads every paid order as one CSV per UTC day to `invoices/YYYY/MM/YYYY-MM-DD.csv` (`invoices/<schema>/…` for a non-`public` `DATABASE_SCHEMA`). Before each upload it reads the bucket's lifecycle rules and refuses to upload unless an enabled, prefix-only rule deletes the file within 90 days (the privacy notice promises this); the PUT carries `If-None-Match: *`, so a stored day is never replaced. Settings shows the state from the run records.

1. S3 bucket in `ap-south-1` (Mumbai) or `ap-south-2` (Hyderabad), no dots in the name, versioning **off**.
2. Lifecycle rule: `{"Rules":[{"ID":"expire-invoices","Filter":{"Prefix":"invoices/"},"Status":"Enabled","Expiration":{"Days":90}}]}`.
3. IAM user with only this policy (the `s3:if-none-match` condition key is from AWS "Enforce conditional writes"):
   `{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::BUCKET/invoices/*","Condition":{"Null":{"s3:if-none-match":"false"}}},{"Effect":"Allow","Action":"s3:GetLifecycleConfiguration","Resource":"arn:aws:s3:::BUCKET"}]}`
4. Set the `INVOICE_BACKUP_*` variables on the **worker** service. A non-AWS S3 store also needs `INVOICE_BACKUP_ENDPOINT` and `INVOICE_BACKUP_ENDPOINT_IN_INDIA=1` (your confirmation that its servers are in India; the app cannot check it). An AWS endpoint must name the India region, e.g. `https://s3.ap-south-1.amazonaws.com`.

Refunds are made in the payment provider and are not in the CSV; keep the provider's refund reports with the books in India too.

## Sending to the host (by design, never automatic)
Job page → "Message host on Airbnb" copies the message and opens `airbnb.co.uk/contact_host/<id>/send_message` in the customer's own browser; they paste and press **Send message**. ReelSieve never drives an Airbnb session, and outreach rows are drafts the customer sends themselves.

## Pipeline (`app/pipeline.py`)
1. `scrape_listing` — public listing HTML: title, city, rating, review count, guests, superhost, category ratings, amenities, photos with room labels.
2. `scrape_reviews` — headless Chromium on `/rooms/<id>/reviews` (client-rendered): name, stars, date, text, % five-star.
3. `build_manifest` — route order (exterior → living → kitchen → bedroom → bath → garden → spa), captions from real facts only, best short 5★ review.
4. Optional `seedance_clips` — Higgsfield `bytedance/seedance-2.5/image-to-video` per scene via official SDK `subscribe`; failed/nsfw/canceled → parallax fallback.
5. `depth.py` (Depth-Anything-V2-Small) + `render_v2.py` (2.5D parallax, letterbox, grade, animated text, xfade) → 1080p + 720p copy.
6. `hostmsg.open_draft` — contact-host form pre-filled via your Airbnb session; `hostmsg.tunnel_*` — public link.

CLI: `.venv/bin/python app/pipeline.py <airbnb_url> jobs/<name> [--email you@x.com] [--ai-motion]`

`main.py` — Seedance 2.5 text-to-video SDK smoke test (`subscribe`, handles Failed/NSFW/Cancelled).
Skill: `~/.claude/skills/property-to-generator` (same renderer, manual/agent workflow).


## QA guards (never repeat a fixed mistake)
`pipeline.lint_manifest` runs on every job before rendering and fails the job on: duplicate captions, a photo used twice, outro sharing the trust/review background, fewer than 6 distinct photos, repeated facts in intro/outro lines, unsanitised review text (leading ", ·" or "Stayed with kids" tags), brand watermark on, missing image files, or a reel outside 26–36 s.
Offline tests on two real listings: `.venv/bin/python -m pytest -q tests` — run before restarting the LaunchAgent.
Fonts: Canva Sans is auto-used if you install it (`~/Library/Fonts/CanvaSans-*.ttf`); otherwise Inter (free, OFL) → Montserrat → Arial.

## Pacing research (19 Sep 2026)
Shots 3–5 s (over 6 s loses momentum); 8–15 photos per reel; order = exterior → entry → living → kitchen/dining → bedrooms → baths → outdoor, end on the best feature + CTA; slideshow reels 60–180 s.
Sources: [StudioBinder](https://www.studiobinder.com/blog/real-estate-video-production/), [Vimeo](https://vimeo.com/blog/post/real-estate-video-marketing), [Reel-E length guide](https://www.reel-e.ai/blog/real-estate-video-length), [Reel-E photos→video](https://www.reel-e.ai/blog/make-real-estate-video-from-photos), [Virtuance slideshows](https://www.virtuance.com/image-slideshow-videos/), [Runway walkthrough](https://runway.com/resources/real-estate-walkthrough-video).
Applied: 4.5 s per photo, every labelled photo up to 14 grouped by room in strict route order, length scales with photo count.

## v3 renderer (default since 19 Sep 2026) — matches the tutorial output
Vertical 9:16 full-bleed, continuous forward camera with zoom-through transitions into the next room, clean bright grade, kinetic captions (one yellow accent word), title/rating/review/CTA overlays on scenes, plus `-16x9.mp4` (portrait over blurred fill). Set `RENDERER=v2` for the older letterboxed card look. Higgsfield Seedance/Kling clips slot in per scene when credits exist.
Host message: "Message host on Airbnb" copies the message and opens the contact form in the browser you're using (already logged in); paste and press Send. The Playwright helper window is optional.

## Prompt-set mode (no Airbnb listing)
`swiss-home/` — 10-image chained prompt set (exterior → living → open-plan → dining/stairs → kitchen → island → living hero → lanai → rear lawn → drone). Images generated in Gemini (free) with each result as the next reference; `prompts.json` holds the prompts. Then `depth.py images depth` + `render_v3.py manifest.json out/…mp4`. Higgsfield had 0 credits/no unlimited allowance, so Gemini was used.
Gemini download quirk: the "Download full-sized image" button only fires when the image is scrolled into view and hovered first.


## Deployment (Railway)
- GitHub: https://github.com/moose9200/ReelSieve (private). Railway project `listing-reel`, service `ReelSieve`, custom domain https://www.reelsieve.braivex.com.
- Build: `Dockerfile` (CPU torch, Chromium, ffmpeg, Depth-Anything model baked into the image). Start: `python -m app.start` (`railway.json`). Health: `/healthz` checks the database and reports a build fingerprint.
- Deploy from this checkout with an explicit path: `railway up <path-to-this-checkout> --path-as-root --service <ReelSieve|ReelSieve-worker|ReelSieve-staging> --environment production --detach`. Without the path and `--path-as-root`, the CLI uploads the directory where the project was *linked* (a parent folder holding old `main`) and production silently goes back to the file-based app.
- `/healthz` → `{"ok": true, "build": "<fingerprint>", "db": true}`; compare the fingerprint with a local `python -c "from app import server; print(server.BUILD)"` to prove what is deployed.

## One-time legacy import
The old app kept everything on the `/data` volume. With `LEGACY_MIGRATION_ENABLED=1` and `LEGACY_SOURCE_DIR=/data`, the first start imports it before web or worker run: accounts with their original password hashes, balances, orders, outreach, jobs (interrupted ones become failed) and Drive tokens (encrypted). Any record without exactly one known owner halts the start; nothing is assigned to "the first admin". A completion marker makes later starts a no-op, and a changed source is refused. Rehearse first: `python -m app.migrate_cloud /data --dry-run` prints counts only. The source files are never modified or deleted.

## Operator console
No public admin setup exists. Customers have no password: they sign in with Continue with Braivex. Only operators (admins) have one, for break-glass sign-in under "Operator sign-in" on /login. From a shell in the service (e.g. `railway ssh`):
```bash
python -m app.admin list
python -m app.admin set-plan someone@example.com starter 5   # plan + credits, e.g. free credits
read -rs PW && printf '%s\n' "$PW" | python -m app.admin set-password operator@example.com   # operators only; keeps it out of shell history
```

## In-app listing picker (19 Sep 2026)
"Find a listing" on the home page: location + optional dates + guests → `GET /api/search` parses Airbnb's public search page (no login; both page variants: `data-deferred-state` and `data-injector-instances`) → results grid (photo, name, rating, price, badges, photo count) → "Use this listing" fills the URL field. Fixture test in `tests/`.


## AI camera motion (property-video-ai method, 19 Sep 2026)
`app/aimotion.py` — audit → author → generate → assemble.
- **Audit (free, always runs):** photo count, single-shoot colour consistency (mean-RGB spread), depth-axis strength per frame (from the depth maps), wide living space. Verdict PASS / WEAK / REJECT shown on the job page; REJECT skips paid generation.
- **Author:** one labelled prompt per shot — SHOT / MOTION FEEL / FIDELITY / LOOK / NEGATIVE — using the four tested rig moves: DOLLY (interiors), AXIS-LOCK (hallways, must name the far end), CRANE (closer, needs a ceiling), ORBIT (island/table; anti-reversal + "final frame must not resemble the opening frame"). No drone.
- **Generate:** Higgsfield API `bytedance/seedance-2.0/image-to-video`, 5 s, single start frame, no audio, 720p (≈22.5 cr) or 1080p (≈45 cr). Estimate is shown before spending. Never auto-retries an unknown submission.
- **Assemble:** frame-difference motion profile per clip → keep the best 3 s window, drop frozen clips, flag dying tails and orbit reversals; hard cuts (no dissolves) in the full-bleed renderer; captions/CTA overlays ride on the clips.
Unverified end-to-end: the Higgsfield account had 0 credits, so generation returned `not_enough_credits` and every shot fell back to parallax (logged per shot).

## Photo selection (19 Sep 2026)
All listing photos are downloaded, then scored in `app/photoscore.py` (sharpness, exposure, colour match to the set, orientation for the target aspect, resolution; depth axis added for the top 18). Inside each room the best frame goes first and frames under 45 are dropped when alternatives exist. The job page shows "N of M used" with per-photo scores.
