# Data-protection compliance (UK GDPR, EU GDPR, PECR, India DPDP) - PLAN

Started 26 Sep 2026 23:20 BST. Goal: ReelSieve complies with the data-protection law that applies to it,
with every current feature still working (189 tests stay green, all live routes unchanged in behaviour).

## Acceptance (each line testable; tick only with evidence in the log)
- [ ] Privacy notice at /privacy covers every UK GDPR Art 13 and Art 14 item for each data subject group (customers, outreach prospects, reviewers named in listing reviews), with the verified controller identity, lawful basis per purpose, processors/recipients, transfers, retention per category, rights, complaint route, cookies, and Google API "Limited Use" wording.
- [x] Signed-in user can download all their personal data (JSON) from Account in one click; export covers every table that holds their data; test proves no other user's data appears. Evidence: 31eab43, 122499c; test_export_has_every_table_and_nothing_of_anyone_else (every table with owner_id/target_id/actor_id present, 0 occurrences of the other user); local Playwright click on "Download my data" saved reelsieve-my-data.json (13 keys, 0 password fields, admin's email absent).
- [x] Signed-in user can delete their account from Account (password re-check); Drive grant revoked, jobs cancelled, personal data erased or anonymised in every table; financial order records kept only as the law requires ~~without email~~ (decision: paid orders keep a billing email snapshot for the tax record period); test covers every table. Evidence: 31eab43, a91f11b; test_erase_removes_or_anonymises_every_table (marker string 0 times in every table after erasure; email only in the paid order's billing_email), test_self_service_deletion_needs_the_password_and_DELETE, test_the_last_admin_cannot_erase_themselves; local Playwright: Delete my account -> /login?notice=deleted, DB: users row anonymised, jobs 0, outreach 0, orders 1 (paid, stripped), old email signs in 401, signs up again 303.
- [x] Retention is enforced by code for every category in the retention schedule (job logs/meta, outreach, deactivated accounts, login failures, OAuth states, legacy archive); tests prove old rows go and recent rows stay. Evidence: 20b0184, 122499c; test_retention_removes_what_is_due_and_keeps_what_is_not, test_legacy_archive_is_kept_until_its_keep_days_pass, test_worker_runs_retention_hourly, test_own_privacy_requests_are_in_the_export_and_leave_two_years_after_handling.
- [x] No personal data leaves for a third party without being listed in the notice (e.g. Google Fonts loaded from our own server, 0 requests to fonts.googleapis.com/gstatic.com on any page). Evidence: 6ddf3d6; local Playwright on /, /login, /signup, /privacy: Assistant loaded, external requests = [] ; screenshot .playwright-mcp/font-selfhost-390.png; test_privacy_basics.py (190 passed).
- [ ] Cookies: only strictly necessary cookies are set (session, CSRF); listed in the notice; no banner needed unless research says otherwise.
- [x] Outreach: prospects can object; objections are honoured (suppression list) and outreach records have a retention limit. Evidence: 8752b3e, 122499c, 20b0184; test_do_not_contact_suppresses_the_prospect_for_every_user, test_privacy_request_form_is_public_acknowledged_and_handled_by_admins (objection with profile suppresses at once), outreach rows deleted 12 months after last change (retention test).
- [x] Security (Art 32): password hashing meets current OWASP guidance with transparent rehash on login; existing logins keep working. Evidence: 5696d0d; OWASP cheat sheet fetched 26 Sep 2026: "PBKDF2-HMAC-SHA256: 600,000 iterations (recommended)"; test_new_password_hashes_use_the_owasp_pbkdf2_sha256_work_factor, test_old_hash_still_signs_in_and_is_upgraded_without_signing_anyone_out (200,000-iteration hash signs in, becomes 600,000, session kept).
- [ ] Records for Hemant in the vault: records of processing (Art 30), legitimate interests assessments, DPIA screening, retention schedule, breach runbook, processor/transfer register.
- [ ] Every current feature still works: full test suite green; live smoke (signup, login, Drive connect, job, plans, outreach) after deploy.

## Audit gaps (from compliance/2026-09-26_personal-data-inventory.md; close each with evidence)
- [x] G1 Payer raw IP stored forever in orders.meta; privacy page says IPs never stored (server.py:532,545; billing.py:54-55). Evidence: 5823149; test_new_orders_store_no_payer_ip, test_schema_strips_payer_ip_from_existing_orders_and_is_idempotent, test_order_views_carry_only_what_the_pages_show.
- [ ] G2 Video shows guest name + review text; sample reel shows a reviewer (render_v2.py:214-215; render_v3.py:116). Code done (65c60fc; test_v2_review_card_shows_stars_month_and_text_but_no_name, test_v3_review_overlay_names_no_guest - run with the OpenCV venv: 5 passed). OPEN: app/static/sample/reel.mp4 still shows a reviewer; it needs a re-render.
- [x] G3 No account erasure; "Remove" only deactivates (auth.py:68-86; account.html:37 promises deletion). Evidence: 31eab43 (self-service, admin Erase, `python -m app.admin erase`), 20b0184 (Remove -> erased after 30 days).
- [x] G4 Legacy archive (hashes, tokens, reviews) kept forever, no delete path (archive_legacy.py). Evidence: 20b0184; `python -m app.archive_legacy --purge` (test_archive_purge_command_prints_counts_then_deletes) and automatic purge LEGACY_ARCHIVE_KEEP_DAYS=90 after creation. Not run against production.
- [x] G5 Outreach keeps scraped host names/profile ids forever; no objection route; misleading "list themselves" copy (server.py:973-982; outreach.html:19). Evidence: 20b0184 (12-month retention), 8752b3e (suppression), 122499c (public objection form), 18518f8 (copy), e1a58bb (PECR notice).
- [x] G6 Jobs keep host first name, host message, listing details forever (worker.py:195-203). Evidence: 20b0184; host name, host message and review data stripped 30 days after a job finishes (retention test).
- [x] G7 Uvicorn access log prints IPs and query strings incl. OAuth code/state, Stripe session id (start.py:25). Evidence: 5696d0d; test_web_process_writes_no_access_log; local run: 0 access-log lines for ~40 requests. Railway's own HTTP log still records visitor IPs (platform; disclose).
- [x] G8 login_failures hashes purged only on next failure (auth.py:124-127). Evidence: 20b0184; deleted after 24 h hourly (retention test).
- [x] G9 Network hashes reversible with the key; described as irreversible; stored for paid/admin too (store.py:17-28). Evidence: 5936621; test_network_hash_is_stored_only_for_free_videos, test_network_hash_uses_a_key_dedicated_to_that_purpose, test_account_page_describes_the_network_code_as_pseudonymised. legal.html wording is the notice owner's.
- [x] G10 No self-service data export (server.py:1008-1011 only outreach CSV). Evidence: 31eab43.
- [ ] G11 Admin can reset passwords silently; no admin audit trail (server.py:442-450). Trail done (456d3cc; test_admin_actions_leave_an_accountability_trail). OPEN: the user is still not told an admin reset their password (needs email or an in-app notice).
- [x] G12 Drive disconnect leaves share links and delivery records; legacy reels imported public (gdrive.py:282-314) - decision: files are the user's; disclose, erase records on account deletion. Evidence: 31eab43; drive_uploads and Drive credentials removed on erasure (erase test). Disclosure is the notice owner's.
- [x] G13 Listing photos (may show people) sent to Higgsfield, undisclosed (aimotion.py:82-83). Evidence: e1a58bb; test_ai_motion_checkbox_says_photos_go_to_higgsfield; screenshot 2026-09-27_privacy-ai-provider-390.png. Notice listing is the notice owner's.
- [x] G14 Fingerprint described but never collected; server accepts any fp (signup.html:14; server.py:353,712). Evidence: 5936621; test_signup_stores_no_fingerprint_and_no_network_hash_on_the_account, test_schema_clears_fingerprint_hashes_already_stored.
- [x] G15 Pages load Airbnb CDN images directly (visitor IP to Airbnb) (app.js:81,598; job.html:33). Evidence: 6349ed2; test_image_proxy_serves_only_airbnb_cdn_images, test_pages_load_listing_photos_through_the_proxy.
- [x] G16 Test fixtures hold real-looking reviewer names (tests/fixtures/*.json). Evidence: 65c60fc; test_fixtures_hold_no_real_reviewer_names. Git history still holds the old values (decide whether to rewrite).
- [ ] G17 Privacy notice incomplete/inaccurate (legal.html) - owner: main session, after legal research

## Phase 2 - lawful growth (owner decisions taken 27 Sep 2026 12:05 BST, no evasion)
Decisions: keep every feature; remove legal blockers lawfully; no proxies/auto-messaging/fake consent.
- [ ] B2B prospects from the Companies House bulk register (company data only; PECR reg 22 does not cover corporate subscribers; reg 23 identity + opt-out) - feature/b2b-companies
- [ ] "Your own photos" as an ADDITIONAL option (owner 12:17 BST: keep). Airbnb link flow stays frictionless: no required tick box; rights confirmation moves to a one-line note + Terms (adjust feature/own-photos at merge). Research: compliance/2026-09-27_public-data-scraping-legality.md.
- [x] Airbnb fetch safeguards (compliance/2026-09-27_public-data-scraping-legality.md §3 rows 3-5, 10) - feature/airbnb-safeguards. Evidence: pytest 277 passed, 2 skipped; hard stop + 30 min cool-down in PostgreSQL (airbnb_blocks), shared limiter 1 page/s + 10 photos/s (airbnb_rate; 3-process test), AIRBNB_FETCH_ENABLED kill switch, blocked_listings + "Remove my listing from ReelSieve" form type, notice sentences. Reviews page kept: live logged-out /rooms/1673857257882928402 (200, 539,375 bytes) had 0 review texts and 0 review dates. Fetch stage +2.7 s per job (stubbed network: 2.07 s -> 4.77 s). Screenshots .playwright-mcp/2026-09-27_airbnb-safeguards-*-{1440,390}.png, 0 px overflow.
- [ ] Referral programme: link only, no cookie, no messaging by us, +1 video each on first delivered reel, guardrails - feature/referrals
- [ ] Daily invoice backup to an S3 bucket in ap-south-1 (Income-tax Rules 2026 r.46(8)); off until env set - feature/india-invoice-backup
- [ ] Each branch reviewed (security/privacy, legal, correctness) and confirmed findings fixed; merged; privacy notice updated; tests green; deployed; live checks
Owner-only (payment/identity): ICO registration (GBP 52), UK representative, Stripe keys, Higgsfield top-up/DPA, AWS bucket for the backup.

## Facts (Verified - source)
- Controller: "Hemant Kumar Sain, Sole Proprietor trading as Braivex. Registered office: Yog Nagar, Street No 08, Alwar, Rajasthan 301001, India. GSTIN 08HUOPS4021L2ZY" - https://braivex.com/ footer, fetched 26 Sep 2026.
- Hosting: Railway, region sfo (US West) for web, worker, Postgres - railway service list / status, this session.
- IP and device fingerprint stored only as keyed hashes (accounts.ip_hash/fp_hash, usage.ip_hash/fp_hash, login_failures.ip_hash) - app/schema/001_core.sql, app/auth.py:115. Since 5936621: no fingerprint; network hash on free-video usage rows only; login_failures keyed by full IP for 24 h.
- Passwords: PBKDF2-SHA256, 200,000 iterations, per-user salt - app/auth.py:24. Since 5696d0d: 600,000 for new hashes, older ones upgraded at sign-in.
- Cookies set: session cookie (auth.COOKIE) and CSRF cookie, both httponly, samesite=lax, secure on https - app/server.py:157,187.
- Pages load Google Fonts from fonts.googleapis.com / fonts.gstatic.com - app/templates (base.html, landing.html).
- Current /privacy is a 6-bullet summary without controller identity, lawful bases, processors, transfers, retention periods, rights or complaint route - app/templates/legal.html.
- Existing purge: store.purge_signals (90 days), OAuth states deleted on expiry - app/store.py, app/gdrive.py.

## Assumptions (to validate - how, when)
- UK GDPR applies via Art 3(2) (offering services to people in the UK); EU GDPR likewise for EU customers - research subagent, primary sources.
- India DPDP Act 2023 applies to Braivex as an Indian data fiduciary - research subagent, primary sources.

## Unknowns (investigation tasks)
- Railway, Stripe, Higgsfield DPAs and transfer mechanisms (SCCs / UK Addendum) - research subagent.
- Whether an Art 27 UK/EU representative is required, and ICO fee applicability for a non-UK controller - research subagent.
- Every place personal data is stored or sent (job logs, meta, outreach, reviews in videos, legacy archive, Railway logs) - audit subagent.

## Decisions (what + why + what was rejected)
- Paid orders keep a billing email snapshot (orders.billing_email, set when paid) for FINANCIAL_RECORDS_YEARS = 8 from payment, then the row is deleted. Why: CGST s.36 (72 months from the annual-return due date) and Income-tax Rules 2026 r.46 (7 tax years from the end of the tax year) both end within 8 years of any payment date (legal report §6). Rejected: exact per-rule dates (more code, same outcome). Records under appeal/investigation need a manual hold.
- An order the customer reported paid before erasing keeps a billing email snapshot too, so a payment that clears later is recorded against the payer; if it is still unpaid 90 days after erasure it is deleted (code review finding, fixed with test_an_order_reported_paid_before_erasure_keeps_the_payer_email_when_settled_later).
- Erasure keeps the users row (anonymised, erased_at) because orders/usage/admin_events point at it; jobs, outreach, Drive receipts, OAuth states and unpaid orders are deleted; usage keeps counts, drops listing key; its ip_hash stays until the normal 90-day purge (abuse window).
- Signal and suppression keys are derived from SESSION_SECRET with purpose labels. Rotating SESSION_SECRET resets free-tier network counts and stops old objections matching: re-key outreach_suppressions before any rotation.
- Outreach retention is 12 months after the last change (brief); the legal report proposes ~90 days (I) - Hemant to decide.
- Image proxy allowlist is a0.muscache.com only (the only CDN host in listing, search and co-host data); add hosts when seen.

## Evidence log (command -> exit code / number / screenshot path)
- 27 Sep 00:40 BST implementation branch worktree-agent-a901a79c446bb028a (14 commits 5696d0d..18518f8 on c660712): pytest `tests` -> 228 passed, 2 skipped (renderer tests need OpenCV; run in /Users/hemant/Higgsfield-Airbnb/2/.venv: 5 passed); compileall exit 0; node --check app.js exit 0.
- Local run on the synthetic test DB (schema privacy_check, dropped afterwards), Playwright screenshots in /Users/hemant/Higgsfield-Airbnb/.playwright-mcp/: 2026-09-27_privacy-account-1440.png, -account-390.png (no horizontal overflow; new buttons and fields 44-45 px tall), -deleted-390.png, -signup-390.png, -request-390.png, -request-1440.png, -request-ack-1440.png, -settings-1440.png, -outreach-390.png, -ai-provider-390.png. Download clicked (reelsieve-my-data.json checked); Delete clicked (account erased in DB).
- 23:37 audit report received (15 gaps + fixtures); implementation subagent started for G1-G16 (worktree), legal research subagent running.
- 23:25 fonts self-hosted: pytest 190 passed; external requests on 4 pages = 0 (Playwright request listener).

## Open risks
- Legal judgement calls (representative, transfer tools, outreach lawful basis) are documented with sources for Hemant; this is not legal advice.
- 27 Sep 11:55 BST LIVE: web build 9eb607e7ebc0 (= local fingerprint of 2657fa3), worker 65ab9acb. /privacy 200 with controller, Limited Use, ICO link (3/3 strings); /privacy/request 200; HSTS max-age=31536000; 0 Google Fonts refs on /; /api/account/export anon 401, signed-in 200 with 13 sections and no hash/salt; Account shows Download + Delete.
