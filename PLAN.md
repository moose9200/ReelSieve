# Data-protection compliance (UK GDPR, EU GDPR, PECR, India DPDP) - PLAN

Started 26 Sep 2026 23:20 BST. Goal: ReelSieve complies with the data-protection law that applies to it,
with every current feature still working (189 tests stay green, all live routes unchanged in behaviour).

## Acceptance (each line testable; tick only with evidence in the log)
- [ ] Privacy notice at /privacy covers every UK GDPR Art 13 and Art 14 item for each data subject group (customers, outreach prospects, reviewers named in listing reviews), with the verified controller identity, lawful basis per purpose, processors/recipients, transfers, retention per category, rights, complaint route, cookies, and Google API "Limited Use" wording.
- [ ] Signed-in user can download all their personal data (JSON) from Account in one click; export covers every table that holds their data; test proves no other user's data appears.
- [ ] Signed-in user can delete their account from Account (password re-check); Drive grant revoked, jobs cancelled, personal data erased or anonymised in every table; financial order records kept only as the law requires, without email; test covers every table.
- [ ] Retention is enforced by code for every category in the retention schedule (job logs/meta, outreach, deactivated accounts, login failures, OAuth states, legacy archive); tests prove old rows go and recent rows stay.
- [x] No personal data leaves for a third party without being listed in the notice (e.g. Google Fonts loaded from our own server, 0 requests to fonts.googleapis.com/gstatic.com on any page). Evidence: 6ddf3d6; local Playwright on /, /login, /signup, /privacy: Assistant loaded, external requests = [] ; screenshot .playwright-mcp/font-selfhost-390.png; test_privacy_basics.py (190 passed).
- [ ] Cookies: only strictly necessary cookies are set (session, CSRF); listed in the notice; no banner needed unless research says otherwise.
- [ ] Outreach: prospects can object; objections are honoured (suppression list) and outreach records have a retention limit.
- [ ] Security (Art 32): password hashing meets current OWASP guidance with transparent rehash on login; existing logins keep working.
- [ ] Records for Hemant in the vault: records of processing (Art 30), legitimate interests assessments, DPIA screening, retention schedule, breach runbook, processor/transfer register.
- [ ] Every current feature still works: full test suite green; live smoke (signup, login, Drive connect, job, plans, outreach) after deploy.

## Audit gaps (from compliance/2026-09-26_personal-data-inventory.md; close each with evidence)
- [ ] G1 Payer raw IP stored forever in orders.meta; privacy page says IPs never stored (server.py:532,545; billing.py:54-55)
- [ ] G2 Video shows guest name + review text; sample reel shows a reviewer (render_v2.py:214-215; render_v3.py:116)
- [ ] G3 No account erasure; "Remove" only deactivates (auth.py:68-86; account.html:37 promises deletion)
- [ ] G4 Legacy archive (hashes, tokens, reviews) kept forever, no delete path (archive_legacy.py)
- [ ] G5 Outreach keeps scraped host names/profile ids forever; no objection route; misleading "list themselves" copy (server.py:973-982; outreach.html:19)
- [ ] G6 Jobs keep host first name, host message, listing details forever (worker.py:195-203)
- [ ] G7 Uvicorn access log prints IPs and query strings incl. OAuth code/state, Stripe session id (start.py:25)
- [ ] G8 login_failures hashes purged only on next failure (auth.py:124-127)
- [ ] G9 Network hashes reversible with the key; described as irreversible; stored for paid/admin too (store.py:17-28)
- [ ] G10 No self-service data export (server.py:1008-1011 only outreach CSV)
- [ ] G11 Admin can reset passwords silently; no admin audit trail (server.py:442-450)
- [ ] G12 Drive disconnect leaves share links and delivery records; legacy reels imported public (gdrive.py:282-314) - decision: files are the user's; disclose, erase records on account deletion
- [ ] G13 Listing photos (may show people) sent to Higgsfield, undisclosed (aimotion.py:82-83)
- [ ] G14 Fingerprint described but never collected; server accepts any fp (signup.html:14; server.py:353,712)
- [ ] G15 Pages load Airbnb CDN images directly (visitor IP to Airbnb) (app.js:81,598; job.html:33)
- [ ] G16 Test fixtures hold real-looking reviewer names (tests/fixtures/*.json)
- [ ] G17 Privacy notice incomplete/inaccurate (legal.html) - owner: main session, after legal research

## Facts (Verified - source)
- Controller: "Hemant Kumar Sain, Sole Proprietor trading as Braivex. Registered office: Yog Nagar, Street No 08, Alwar, Rajasthan 301001, India. GSTIN 08HUOPS4021L2ZY" - https://braivex.com/ footer, fetched 26 Sep 2026.
- Hosting: Railway, region sfo (US West) for web, worker, Postgres - railway service list / status, this session.
- IP and device fingerprint stored only as keyed hashes (accounts.ip_hash/fp_hash, usage.ip_hash/fp_hash, login_failures.ip_hash) - app/schema/001_core.sql, app/auth.py:115.
- Passwords: PBKDF2-SHA256, 200,000 iterations, per-user salt - app/auth.py:24.
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

## Evidence log (command -> exit code / number / screenshot path)
- 23:37 audit report received (15 gaps + fixtures); implementation subagent started for G1-G16 (worktree), legal research subagent running.
- 23:25 fonts self-hosted: pytest 190 passed; external requests on 4 pages = 0 (Playwright request listener).

## Open risks
- Legal judgement calls (representative, transfer tools, outreach lawful basis) are documented with sources for Hemant; this is not legal advice.
