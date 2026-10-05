#!/usr/bin/env python3
"""ReelSieve by Braivex — paste an Airbnb URL, get a cinematic reel in your own Google Drive.

Web process only: identity, jobs, billing and Drive credentials live in PostgreSQL; rendering runs
in app.worker. Nothing here writes customer data to local disk.
Run: .venv/bin/uvicorn app.server:app --port 8787   (DATABASE_URL, SESSION_SECRET, TOKEN_ENCRYPTION_KEY)
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.middleware.base import BaseHTTPMiddleware

from app import (admin, airbnb, auth, billing, braivex_sso, cohost, companies, database, fetch, gdrive, hostmsg, invoices,
                 jobs, linkedin, migrate_cloud, photos, plans, referrals, retention, store)
from app import search as listing_search

HERE = Path(__file__).resolve().parent
REQUIRED = ('DATABASE_URL', 'SESSION_SECRET', 'TOKEN_ENCRYPTION_KEY')
CSRF_COOKIE = '__Host-reelsieve_csrf'  # Secure, Path=/, no Domain, like the session cookie
PUBLIC_PREFIXES = ('/static/', '/oauth/google/callback', '/favicon.ico', '/api/billing/webhook/', '/r/', '/auth/braivex/')
# /setup, /forgot and /reset no longer exist (04 Oct 2026). They stay public so an old link or bookmark gets a plain 404,
# not a sign-in redirect.
PUBLIC_EXACT = ('/', '/login', '/signup', '/setup', '/forgot', '/reset', '/healthz', '/privacy', '/terms', '/privacy/request',
                '/robots.txt', '/sitemap.xml', '/llms.txt')
DAILY_CAP = int(os.getenv('OUTREACH_DAILY_CAP', '5'))
TRUSTED_HOPS = int(os.getenv('TRUSTED_PROXY_HOPS', '1'))
COHOST_MESSAGE = ("Hi {name} — I make short cinematic walkthrough videos for short-let "
                  "listings, built from the photos and reviews already on them. I made one for a {city} property this week and thought of you.\n\n"
                  "Happy to make one for {listing_title} free so you can see it — no strings, no card. If it is useful I do them at volume for operators.\n\n"
                  "If you'd rather I sent it elsewhere, tell me where and I will.")
SETTINGS = [('HF_KEY', True, 'Higgsfield API key — enables AI camera motion (billable)'),
            ('GOOGLE_CLIENT_ID', False, 'OAuth client ID (Web application) customers connect their Drive through'),
            ('GOOGLE_CLIENT_SECRET', True, 'OAuth client secret'),
            ('GDRIVE_FOLDER', False, 'Drive folder name created in each customer\'s Drive'),
            ('PUBLIC_BASE_URL', False, 'Public address of this app; OAuth and payment return links use it'),
            ('CHECKOUT_STARTER', False, 'Reusable checkout link for Starter'),
            ('CHECKOUT_COMMERCIAL', False, 'Reusable checkout link for Commercial'),
            ('BILLING_WEBHOOK_SECRET', True, 'Secret your payment provider signs webhooks with'),
            ('STRIPE_SECRET_KEY', True, 'Stripe secret or restricted key (Checkout Sessions: write); card checkout needs this and the webhook secret'),
            ('STRIPE_WEBHOOK_SECRET', True, 'Signing secret (whsec_…) of the Stripe webhook endpoint for checkout.session.completed'),
            ('INVOICE_BACKUP_BUCKET', False, 'Worker service: S3 bucket in India for the daily invoice backup (Income-tax Rules 2026 r.46(8)), with a lifecycle rule deleting invoices/ within 90 days and versioning off; the backup is off until the bucket and both keys are set'),
            ('INVOICE_BACKUP_REGION', False, 'Worker service: bucket region, ap-south-1 (Mumbai, the default) or ap-south-2 (Hyderabad)'),
            ('INVOICE_BACKUP_ACCESS_KEY_ID', True, 'Worker service: access key ID of an IAM user allowed only s3:PutObject on invoices/* when If-None-Match is sent, and s3:GetLifecycleConfiguration on the bucket'),
            ('INVOICE_BACKUP_SECRET_ACCESS_KEY', True, 'Worker service: secret access key of that IAM user'),
            ('INVOICE_BACKUP_ENDPOINT', False, 'Worker service, optional: https:// S3 endpoint. An AWS one must name the region (https://s3.ap-south-1.amazonaws.com); any other needs INVOICE_BACKUP_ENDPOINT_IN_INDIA'),
            ('INVOICE_BACKUP_ENDPOINT_IN_INDIA', False, 'Worker service: set to 1 to confirm a non-AWS INVOICE_BACKUP_ENDPOINT keeps files on servers in India; the app cannot check this'),
            ('BILLING_NOTE', False, 'Line shown to customers who choose invoice'),
            ('DEFAULT_MESSAGE', False, 'Default host message template'),
            ('AIRBNB_FETCH_ENABLED', False, 'Airbnb fetching: 1 on, 0 off. Off stops every request to Airbnb and its photo '
                                            'CDN: new listing-link reels, reels in progress, Find a listing, co-host search and listing photos'),
            ('AIRBNB_BLOCK_COOLDOWN_MIN', False, 'Minutes all Airbnb fetching pauses after Airbnb blocks a request'),
            ('AIRBNB_PAGE_RPS', False, 'Airbnb page requests per second, one budget for the web and every worker'),
            ('AIRBNB_IMAGE_RPS', False, 'Airbnb photo requests per second, one budget for the web and every worker'),
            ('AIRBNB_BROWSER_RPS', False, 'Script and data requests per second of the headless reviews page, one budget for every worker')]
SETTING_DEFAULTS = airbnb.DEFAULTS
# Airbnb lookups one account may have running at once: searches and co-host lookups, and photos through /img.
# A person's browser runs one search at a time and loads a screen of thumbnails; a script running more is refused.
AT_ONCE = {'lookup': 2, 'img': 24}
_running, _running_lock = {}, threading.Lock()
# "Remove my listing" requests from the public form that block at once, per email address and in total, in 24 hours.
REMOVALS_PER_EMAIL, REMOVALS_PER_DAY = 3, 20


def validate_config():
    """Refuse to start without the cloud database and secrets; there is no local fallback."""
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        raise RuntimeError('Missing required configuration: ' + ', '.join(missing))
    gdrive._fernet()
    airbnb.validate()


@contextmanager
def at_once(user, group):
    """Refuse (429) when this account already has AT_ONCE[group] Airbnb lookups of this group running.
    ponytail: counted per web process; with several web replicas each allows its own AT_ONCE."""
    key = (user, group)
    with _running_lock:
        if _running.get(key, 0) >= AT_ONCE[group]:
            raise HTTPException(429, 'You already have Airbnb lookups running. Wait for them to finish.')
        _running[key] = _running.get(key, 0) + 1
    try:
        yield
    finally:
        with _running_lock:
            _running[key] -= 1
            if not _running[key]:
                del _running[key]


CANONICAL = 'https://www.reelsieve.braivex.com'


def check_public_base():
    """Production must name its own address: OAuth, payment and Braivex return links are built from it. Logs an error
    and lets the start go on: a wrong value breaks those links, refusing to start would break everything."""
    if os.getenv('RAILWAY_ENVIRONMENT_NAME') == 'production' and \
            (os.getenv('PUBLIC_BASE_URL') or '').strip().rstrip('/') != CANONICAL:
        print(json.dumps({'error': f'PUBLIC_BASE_URL must be {CANONICAL} in production'}), flush=True)


@asynccontextmanager
async def lifespan(_app):
    """The web process is the only one that migrates (the worker waits for the schema), and so the only one that runs
    the one-time legacy import: with LEGACY_MIGRATION_ENABLED=1, LEGACY_SOURCE_DIR is imported once (a completion
    marker makes later starts a no-op; a changed source or any ownership doubt stops the start)."""
    validate_config()
    check_public_base()
    database.initialize()
    if os.getenv('LEGACY_MIGRATION_ENABLED') == '1':
        if not os.getenv('LEGACY_SOURCE_DIR'):
            raise SystemExit('LEGACY_MIGRATION_ENABLED=1 needs LEGACY_SOURCE_DIR')
        try:
            print(json.dumps({'legacy_migration': migrate_cloud.run(os.environ['LEGACY_SOURCE_DIR'], apply=True)},
                             sort_keys=True), flush=True)
        except migrate_cloud.MigrationError as e:
            raise SystemExit(str(e))
    store._suppression_key()  # freeze the do-not-contact key before anything can rotate SESSION_SECRET
    yield


app = FastAPI(title='ReelSieve by Braivex', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
tpl = Jinja2Templates(directory=HERE / 'templates')
tpl.env.autoescape = True


def public_base():
    b = (os.getenv('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if not b and os.getenv('RAILWAY_PUBLIC_DOMAIN'):
        b = 'https://' + os.environ['RAILWAY_PUBLIC_DOMAIN']
    return b


def site_url():
    """Canonical origin for crawlers and canonical links: configured, never taken from the request's Host header."""
    return public_base() or 'https://www.reelsieve.braivex.com'


tpl.env.globals['site_url'] = site_url
tpl.env.globals['airbnb_enabled'] = airbnb.enabled
tpl.env.globals['airbnb_disabled'] = airbnb.DISABLED
tpl.env.globals['current_year'] = lambda: time.strftime('%Y', time.gmtime())  # the footer's copyright line
tpl.env.filters['day'] = lambda ts: time.strftime('%d %b %Y', time.gmtime(ts or 0))
tpl.env.filters['when'] = lambda ts: time.strftime('%d %b %Y %H:%M UTC', time.gmtime(ts or 0))


def _secure(request):
    return request.url.scheme == 'https' or 'https' in request.headers.get('x-forwarded-proto', '')


def _ip(request):
    """Client address from the RIGHT of X-Forwarded-For: only entries our own proxies appended are trusted."""
    parts = [x.strip() for x in request.headers.get('x-forwarded-for', '').split(',') if x.strip()]
    if parts:
        return parts[max(0, len(parts) - max(1, TRUSTED_HOPS))]
    return request.client.host if request.client else '?'


def _role_admin(user):
    return bool(user) and auth.role(user) == 'admin'


def _who(session):
    """(signed-in email or None, is an operator). Blocking."""
    user = auth.check(session)
    return user, _role_admin(user)


def _csrf_basis(request):
    """Signed-in: the session. Anonymous: a random per-browser nonce, never a shared constant."""
    return request.cookies.get(auth.COOKIE) or ('anon:' + getattr(request.state, 'csrf_nonce', ''))


def csrf_for(request):
    return auth.csrf_token(_csrf_basis(request))


tpl.env.globals['csrf_for'] = csrf_for


def _safe_next(target, default='/app'):
    """A path on this site, or default. No // anywhere in the path (a query may still carry an encoded listing URL)."""
    t = target or ''
    if not t.startswith('/') or '//' in t.partition('?')[0] or '\\' in t or any(ord(ch) < 32 or ord(ch) == 127 for ch in t):
        return default
    return t


class Gate(BaseHTTPMiddleware):
    """Sign-in gate plus one CSRF policy for every state-changing request (webhook excepted: it is HMAC-signed)."""

    async def dispatch(self, request, call_next):
        airbnb.max_wait.set(airbnb.WEB_MAX_WAIT)  # this request's task only: a web request never queues long for Airbnb
        path = request.url.path
        nonce = request.cookies.get(CSRF_COOKIE, '')
        fresh = '' if nonce else secrets.token_urlsafe(24)
        request.state.csrf_nonce = nonce or fresh
        session = request.cookies.get(auth.COOKIE, '')
        # Database reads: in the thread pool, so a slow database never holds every other request on the event loop.
        user, request.state.is_admin = await run_in_threadpool(_who, session) if session else (None, False)
        request.state.user = user
        if not user and not (path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT):
            if path.startswith('/api/'):
                return JSONResponse({'detail': 'Sign in required'}, status_code=401)
            target = path + ('?' + request.url.query if request.url.query else '')
            return RedirectResponse('/login?next=' + quote(target), status_code=303)
        # Exempt: the payment webhook (HMAC-signed) and the Braivex callback (a cross-site POST from
        # accounts.braivex.com, proved by the assertion's signature and by the state cookie this browser holds).
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not path.startswith('/api/billing/webhook/') \
                and path != braivex_sso.CALLBACK_PATH:
            sent = request.headers.get('x-csrf-token', '')
            # /api/ callers are the page's scripts, which always send the header: the body is never read here.
            if not sent and not path.startswith('/api/') and \
                    request.headers.get('content-type', '').startswith(('application/x-www-form-urlencoded', 'multipart/form-data')):
                try:
                    request.scope['_form'] = await _small_form(request)
                except HTTPException as e:
                    return HTMLResponse(e.detail, status_code=e.status_code)
                sent = request.scope['_form'].get('csrf') or ''
            basis = _csrf_basis(request)
            if (fresh and not session) or not auth.csrf_ok(basis, sent):
                if path.startswith('/api/'):
                    return JSONResponse({'detail': 'Form expired — reload the page and try again'}, status_code=403)
                return HTMLResponse('Invalid or expired form token — reload and try again', status_code=403)
        response = await call_next(request)
        if fresh:
            response.set_cookie(CSRF_COOKIE, fresh, httponly=True, samesite='lax', secure=True, path='/', max_age=30 * 86400)
        return response


app.add_middleware(Gate)


@app.middleware('http')
async def canonical_host(request, call_next):
    """The bare apex is routed here too, but Braivex sign-in always returns to the canonical www host, so a sign-in
    started on the apex left its host-only state cookie behind and could never finish (05 Oct 2026). Registered
    after Gate so it runs first; 308 keeps a POST a POST."""
    canon = site_url()
    canon_host = canon.split('://', 1)[-1]
    host = (request.headers.get('host') or '').split(':')[0].lower()
    if canon_host.startswith('www.') and host == canon_host[4:]:
        query = request.url.query
        return RedirectResponse(canon + request.url.path + ('?' + query if query else ''), status_code=308)
    return await call_next(request)


SECURITY_HEADERS ={'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'strict-origin-when-cross-origin',
                    'X-Frame-Options': 'DENY'}


@app.middleware('http')
async def security_headers(request, call_next):
    """Registered after Gate, so it wraps it: the Gate's redirects and refusals carry these headers too."""
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if _secure(request):  # browsers only honour HSTS over HTTPS; one year, this host only
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    if os.getenv('SEO_NOINDEX') == '1':  # staging and previews: never compete with the real site in search
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    if request.url.path.startswith('/static/') and response.status_code < 400:
        # File names are not fingerprinted: one day caps how long a deploy's CSS/JS can be stale; then the ETag gives a 304.
        response.headers.setdefault('Cache-Control', 'public, max-age=86400')
    return response


@app.exception_handler(405)
async def method_not_served(request, exc):
    """A method a path does not serve is a route that does not exist: POST /signup (password sign-up, removed
    04 Oct 2026) answers 404 like the other removed routes, not 405."""
    return JSONResponse({'detail': 'Not Found'}, status_code=404)


FORM_MAX = 64 * 1024  # sign-in and privacy forms; files only ever arrive through the photo upload API


async def _small_form(request):
    """An HTML form body: small, read in memory, never a file part, so nothing reaches disk before sign-in and CSRF."""
    length = request.headers.get('content-length', '')
    if not length.isdigit() or not 0 < int(length) <= FORM_MAX:
        raise HTTPException(413, 'This form is too large. Reload the page and try again.')
    try:
        return dict(await request.form(max_files=0, max_fields=50))
    except (MultiPartException, StarletteHTTPException):  # Starlette turns a refused part into its own 400
        raise HTTPException(400, 'This form could not be read. Reload the page and try again.')


async def _form(request):
    return request.scope['_form'] if '_form' in request.scope else await _small_form(request)


def _set_session(resp, request, user, long=True, auth_time=None):
    """auth_time: when the person proved who they are (a Braivex assertion's iat); None is now (an operator password)."""
    tok, ttl = auth.issue(user, long, auth_time)
    resp.set_cookie(auth.COOKIE, tok, max_age=ttl, httponly=True, samesite='lax', secure=True, path='/')
    return resp


def _end_session(resp):
    resp.delete_cookie(auth.COOKIE, path='/', secure=True, httponly=True, samesite='lax')
    return resp


def _require_admin(request):
    if not request.state.is_admin:
        raise HTTPException(403, 'Admin only')


def build_id():
    """Content hash of the app source: proves which code a deployment serves."""
    h = hashlib.sha256()
    for f in sorted(list(HERE.glob('*.py')) + list((HERE / 'schema').glob('*.sql')) + list((HERE / 'templates').glob('*.html'))
                    + sorted((HERE / 'static').glob('*.css')) + [HERE / 'static' / 'app.js']):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


BUILD = build_id()
# Static files are cached for a day; the build in each asset URL makes a deploy fetch fresh CSS/JS at once.
tpl.env.globals['asset_version'] = BUILD


@app.get('/favicon.ico')
def favicon():
    return FileResponse(HERE / 'static' / 'brand' / 'favicon.ico', media_type='image/x-icon')


@app.get('/healthz')
def healthz():
    try:
        with database.connect() as c:
            c.execute('SELECT 1')
    except Exception:
        return JSONResponse({'ok': False, 'build': BUILD, 'db': False}, status_code=503)
    return {'ok': True, 'build': BUILD, 'db': True}


# ---------------- search and AI answer engines ----------------

PRIVATE_PATHS = ('/app', '/api/', '/jobs/', '/reels', '/outreach', '/settings', '/account', '/upgrade', '/oauth/', '/logout')
# Google: list only the URLs you want in search results. /login is a bare sign-in page, so it is noindex and absent.
INDEXABLE = {'/': 'landing.html', '/signup': 'signup.html', '/privacy': 'legal.html', '/terms': 'legal.html'}
LLMS_TXT = """# ReelSieve

> ReelSieve, made by Braivex, turns an Airbnb listing link into a cinematic walkthrough video built from the listing's own photos and real guest reviews.

It works from an Airbnb listing link, the kind with /rooms/ in the address, or from {min_photos} to {max_photos} photos the customer uploads.
Link reels have an intro, a rating card when the listing has reviews, the rooms in walking order with captions, a real guest review card and an outro.
Photo reels have an intro, the rooms in walking order with captions and an outro, and can show one guest quote the customer provides.
There are two styles: 16:9 cinematic and 9:16 vertical.
Every listing photo is scored and the best frame for each room is used.
Paid plans can add AI camera motion.

Finished videos go to the customer's own Google Drive, and ReelSieve keeps no copy.
The only Drive permission it asks for is drive.file.
Videos stay private unless the customer chooses to share them.

ReelSieve also drafts a message for the listing's host, with no links in it.
The customer sends it from their own Airbnb account.
ReelSieve never sends messages itself.

Plans are one-off packs, priced in US dollars:

{plans}

If a render fails, the credit is refunded.

ReelSieve is independent and is not endorsed by or associated with Airbnb, Inc.

## Pages

- [Home]({base}/): What ReelSieve does
- [Sign up]({base}/signup): Create an account
- [Privacy]({base}/privacy): What ReelSieve stores and for how long
- [Terms]({base}/terms): Terms of use

## Optional

- [Braivex](https://braivex.com): The studio behind ReelSieve
"""


@app.get('/robots.txt')
def robots_txt():
    if os.getenv('SEO_NOINDEX') == '1':
        return PlainTextResponse('User-agent: *\nDisallow: /\n')
    # Disallow lines come first so first-match parsers agree with Google's longest-match rule.
    rules = ''.join(f'Disallow: {p}\n' for p in PRIVATE_PATHS)
    return PlainTextResponse(f'User-agent: *\n{rules}Allow: /\n\nSitemap: {site_url()}/sitemap.xml\n')


@app.get('/sitemap.xml')
def sitemap_xml():
    """lastmod is each page template's file date; a fresh build can move it even when the page did not change."""
    def entry(path, template):
        day = time.strftime('%Y-%m-%d', time.gmtime((HERE / 'templates' / template).stat().st_mtime))
        return f'<url><loc>{escape(site_url() + path)}</loc><lastmod>{day}</lastmod></url>'
    urls = ''.join(entry(p, t) for p, t in INDEXABLE.items())
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n', media_type='application/xml')


@app.get('/llms.txt')
def llms_txt():
    """https://llmstxt.org format. Prices come from plans.PLANS on every request."""
    lines = '\n'.join(f"- {p['name']}: {p['price_label']} for {p['videos']} videos" if p['price_usd'] is not None
                      else f"- {p['name']}: priced on request" for p in plans.public_plans())
    return PlainTextResponse(LLMS_TXT.format(plans=lines, base=site_url(), min_photos=photos.MIN_PHOTOS,
                                             max_photos=photos.MAX_PHOTOS))


# ---------------- identity ----------------
# Customers sign in and sign up only with Braivex Accounts (04 Oct 2026). The one password form left is an operator's
# break-glass sign-in: admins only, 5 tries per network per 10 minutes, hashed and slowed off the event loop.

LOGIN_FAIL_DELAY = 0.6  # seconds a wrong operator password waits for its answer


@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request, next: str = '/app', notice: str = ''):
    if request.state.user:
        return RedirectResponse(_safe_next(next), status_code=303)
    msg = {'out': 'You have been signed out.', 'deleted': 'Your account has been deleted.'}.get(notice, '')
    return tpl.TemplateResponse(request, 'login.html', {'next': _safe_next(next), 'notice': msg})


def _operator_login(request, f):
    """(response, failed). Blocking (database, PBKDF2): runs in the thread pool."""
    u, p, nxt = (f.get('user') or '').strip(), f.get('password') or '', _safe_next(f.get('next'))
    ip = _ip(request)
    ctx = lambda err, code: tpl.TemplateResponse(request, 'login.html', {'next': nxt, 'user': u, 'error': err, 'operator': True},  # noqa: E731
                                                 status_code=code)
    attempt = auth.reserve_attempt(ip)  # counted before the hash, so parallel guesses cannot outrun the limit
    if attempt is None:
        return ctx('Too many attempts — wait 10 minutes', 429), False
    try:
        ok = auth.verify(u, p)  # operators only: a customer's old password never verifies
    except BaseException:
        auth.release_attempt(attempt)
        raise
    if not ok:
        return ctx('Wrong email or password', 401), True  # the reserved row stays as the failure
    auth.clear_fails(ip)
    store.note_signin(u, ip)
    return _set_session(RedirectResponse(nxt, status_code=303), request, u, f.get('remember') == '1'), False


@app.post('/login')
async def login_post(request: Request):
    response, failed = await run_in_threadpool(_operator_login, request, await _form(request))
    if failed:
        await asyncio.sleep(LOGIN_FAIL_DELAY)  # slows guessing without holding the event loop or a worker thread
    return response


@app.post('/logout')
def logout():
    return _end_session(RedirectResponse('/login?notice=out', status_code=303))


def _ref(code):
    code = (code or '').strip().lower()
    return code if referrals.CODE.match(code) else ''


@app.get('/r/{code}')
def referral_link(code: str):
    """Invite link. The code goes on in the address to a hidden signup field: no cookie, no browser storage (PECR reg 6)."""
    code = _ref(code)
    return RedirectResponse('/signup' + ('?ref=' + code if code else ''), status_code=303)


@app.get('/signup', response_class=HTMLResponse)
def signup_page(request: Request, plan: str = '', url: str = '', ref: str = ''):
    if request.state.user:
        return RedirectResponse('/app', status_code=303)
    return tpl.TemplateResponse(request, 'signup.html', {'plan': plan, 'url': url[:500], 'ref': _ref(ref), 'plans': plans.public_plans()})


# ---------------- Continue with Braivex ----------------
# accounts.braivex.com verifies the mailbox with a 6-digit code through Shopify customer accounts and posts a signed,
# 120-second assertion back here. Contract: /Users/hemant/braivex-accounts/docs/PRODUCT-INTEGRATION.md (29 Sep 2026).

# __Host- cookies: Secure, Path=/ and no Domain, so neither a sibling subdomain nor plain HTTP can plant or read them.
STATE_COOKIE = '__Host-braivex_sso_state'   # this browser's sign-in, 10 minutes
NEW_COOKIE = '__Host-braivex_sso_new'       # the verified claims of a customer with no account yet, 15 minutes
CLAIM_COOKIE = '__Host-braivex_sso_claim'   # the verified claims of someone claiming an older account, 15 minutes
FLOW_COOKIES = (STATE_COOKIE, NEW_COOKIE, CLAIM_COOKIE)
NEW_TTL = 15 * 60


def _sso_refused(request, message, nxt='/app', status=401):
    """The sign-in page again, saying what went wrong and nothing about why. Never signs anyone in, and ends the
    sign-in it was part of."""
    r = tpl.TemplateResponse(request, 'login.html', {'next': _safe_next(nxt), 'error': message}, status_code=status)
    for name in FLOW_COOKIES:
        _drop(r, name)
    return r


def _flow_cookie(response, name, ttl, **data):
    response.set_cookie(name, auth.seal(name, ttl, **data), max_age=ttl, httponly=True, samesite='lax', secure=True,
                        path='/')
    return response


def _drop(response, name):
    response.delete_cookie(name, path='/', secure=True, httponly=True, samesite='lax')
    return response


@app.get('/auth/braivex/start')
def braivex_start(request: Request, next: str = '/app', login_hint: str = '', ref: str = ''):
    """Mint this browser's state, remember where it was going, and hand the sign-in to Braivex Accounts."""
    state = secrets.token_urlsafe(32)
    query = {'client': braivex_sso.CLIENT, 'return_to': braivex_sso.callback_url(site_url()), 'state': state}
    hint = auth.norm(login_hint)
    if auth.EMAIL.match(hint):
        query['login_hint'] = hint
    r = RedirectResponse(braivex_sso.accounts_url() + '/start?' + urlencode(query), status_code=302)
    return _flow_cookie(r, STATE_COOKIE, 600, state=state, next=_safe_next(next), ref=_ref(ref))


@app.post(braivex_sso.CALLBACK_PATH)
async def braivex_callback(request: Request):
    """The only route the CSRF check skips: this POST is a cross-site form submit from accounts.braivex.com, and
    what proves it is the assertion's signature plus the state cookie this browser was given."""
    form = await _form(request)
    return await run_in_threadpool(_braivex_finish, request, form)


def _braivex_finish(request, form):
    """Blocking (the key set fetch, the database): runs in the thread pool."""
    ip = _ip(request)
    if auth.too_many(ip, 'sso'):  # refused assertions, per address (IPv6: per /64), as the password form counts them
        return _sso_refused(request, 'Too many sign-in attempts — wait 10 minutes', status=429)
    sealed = auth.unseal(STATE_COOKIE, request.cookies.get(STATE_COOKIE, ''))  # read once, then gone whatever happens below
    if not sealed:
        return _sso_refused(request, 'That sign-in did not start in this browser. Try again.')
    nxt, ref = _safe_next(sealed.get('next')), _ref(sealed.get('ref'))
    try:
        claims = braivex_sso.verify(form.get('assertion') or '', sealed.get('state') or '')
    except braivex_sso.BraivexAssertionError as e:
        # The reason only (PyJWT/verifier wording, never the token or an address): the 05 Oct outage was invisible here.
        print(json.dumps({'braivex_sso_refused': str(e)[:200]}), flush=True)
        auth.record_fail(ip, 'sso')
        return _sso_refused(request, 'Braivex could not sign you in. Try again.', nxt)
    if not braivex_sso.spend_jti(claims['jti']):
        auth.record_fail(ip, 'sso')
        return _sso_refused(request, 'That sign-in has already been used. Try again.', nxt)
    email, sub = auth.norm(claims['email']), claims['sub']
    row = auth.by_braivex(sub) or auth.identity(email)
    if not row:
        # Somebody Braivex has verified who has never had a ReelSieve account: name the business, then sign up.
        r = _drop(RedirectResponse('/auth/braivex/workspace', status_code=303), STATE_COOKIE)
        return _flow_cookie(r, NEW_COOKIE, NEW_TTL, email=email, sub=sub, next=nxt, ref=ref, iat=claims['iat'])
    if row['role'] != 'member':
        # Braivex sign-in never grants operator rights, so an operator account keeps its own path.
        return _sso_refused(request, 'This account signs in with its password.', nxt)
    if row['braivex_customer_id'] not in (None, sub):  # sub never changes: a row linked to another is never re-pointed
        return _sso_refused(request, 'That Braivex account is already linked elsewhere. Email hello@braivex.com.', nxt)
    if row['braivex_customer_id'] is None and not row['email_trusted'] and store.has_data(row['email']):
        # An account made with a password, which never proved the mailbox, that holds something: hand it over only
        # when the person Braivex just verified says so.
        r = _drop(RedirectResponse('/auth/braivex/claim', status_code=303), STATE_COOKIE)
        return _flow_cookie(r, CLAIM_COOKIE, NEW_TTL, email=row['email'], sub=sub, next=nxt, iat=claims['iat'])
    return _sign_in_linked(request, row, sub, nxt, STATE_COOKIE, claims['iat'])


def _sign_in_linked(request, row, sub, nxt, cookie, auth_time):
    """Link (a takeover when the row has no Shopify customer yet), then sign in. Blocking."""
    linked = auth.link_braivex(row['email'], sub)
    if not linked:
        return _sso_refused(request, 'That Braivex account is already linked elsewhere. Email hello@braivex.com.', nxt)
    grant = linked[1]  # a first link already dropped the stored Drive grant with the link itself
    if grant:
        try:  # best effort: nothing here can bring the grant back; an unrevoked one is the prior holder's to remove
            if not gdrive.revoke(grant):
                print('{"drive": "revoke not confirmed after a first Braivex link"}', flush=True)
        except Exception:
            print('{"drive": "revoke failed after a first Braivex link"}', flush=True)
    store.note_signin(row['email'], _ip(request))
    return _drop(_set_session(RedirectResponse(nxt, status_code=303), request, row['email'], True, auth_time), cookie)


@app.get('/auth/braivex/claim', response_class=HTMLResponse)
def braivex_claim(request: Request):
    claim = auth.unseal(CLAIM_COOKIE, request.cookies.get(CLAIM_COOKIE, ''))
    if not claim:
        return _sso_refused(request, 'That sign-in has expired. Start again.')
    return tpl.TemplateResponse(request, 'braivex_claim.html', {
        'email': claim['email'], 'account': plans.account_view(claim['email']),
        'drive': gdrive.status(claim['email'])['connected']})


@app.post('/auth/braivex/claim')
def braivex_claim_post(request: Request):
    """The explicit Claim (CSRF-checked like every form). Plain def: FastAPI runs it in the thread pool."""
    claim = auth.unseal(CLAIM_COOKIE, request.cookies.get(CLAIM_COOKIE, ''))
    if not claim:
        return _sso_refused(request, 'That sign-in has expired. Start again.')
    row, nxt = auth.identity(claim['email']), _safe_next(claim.get('next'))
    if not row or row['role'] != 'member' or row['braivex_customer_id'] not in (None, claim['sub']):
        return _sso_refused(request, 'That Braivex account is already linked elsewhere. Email hello@braivex.com.', nxt)
    return _sign_in_linked(request, row, claim['sub'], nxt, CLAIM_COOKIE, claim.get('iat'))


def _workspace_page(request, new, status=200, error=''):
    return tpl.TemplateResponse(request, 'braivex_workspace.html', {'email': new['email'], 'error': error},
                                status_code=status)


@app.get('/auth/braivex/workspace', response_class=HTMLResponse)
def braivex_workspace(request: Request):
    new = auth.unseal(NEW_COOKIE, request.cookies.get(NEW_COOKIE, ''))
    if not new:
        return _sso_refused(request, 'That sign-in has expired. Start again.')
    return _workspace_page(request, new)


@app.post('/auth/braivex/workspace')
async def braivex_workspace_post(request: Request):
    form = await _form(request)
    return await run_in_threadpool(_braivex_create, request, form)


def _braivex_create(request, f):
    """The only place a customer account is made (04 Oct 2026), so the sign-up guards run here: disposable addresses
    and the free videos per network. No password: Braivex holds the sign-in. Blocking: runs in the thread pool."""
    new = auth.unseal(NEW_COOKIE, request.cookies.get(NEW_COOKIE, ''))
    if not new:
        return _sso_refused(request, 'That sign-in has expired. Start again.')
    business, ip = (f.get('business') or '').strip()[:120], _ip(request)
    guard = plans.signup_guard(new['email'], ip)
    if guard:
        return _workspace_page(request, new, 400, guard)
    try:
        auth.create_user(new['email'], braivex_customer_id=new['sub'])
    except ValueError as e:
        made = auth.by_braivex(new['sub'])
        if not made or made['email'] != auth.norm(new['email']):
            return _workspace_page(request, new, 400, str(e))
        # The same sign-up sent twice (a double click, two tabs) lost the race to the first request, which made the
        # account and does the rest: sign in to that one, never a second account and never a server error.
        return _drop(_set_session(RedirectResponse(_safe_next(new.get('next')), status_code=303), request,
                                  made['email'], True, new.get('iat')), NEW_COOKIE)
    store.ensure_account(new['email'], 'free')
    store.note_signin(new['email'], ip)
    referrals.attribute(new['email'], new.get('ref'))
    if business:
        store.set_b2b_sender(new['email'], '', business, new['email'])
    r = _set_session(RedirectResponse(_safe_next(new.get('next')), status_code=303), request, new['email'], True,
                     new.get('iat'))
    return _drop(r, NEW_COOKIE)


@app.get('/api/account')
def api_account(request: Request):
    return plans.account_view(request.state.user)


@app.get('/api/account/export')
def account_export(request: Request):
    """Download my data (UK/EU GDPR Art 15 and 20): every table's rows for the signed-in owner, as JSON."""
    data = {'exported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'privacy_notice': site_url() + '/privacy',
            **store.export(request.state.user)}
    return Response(json.dumps(data, indent=1, default=str), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="reelsieve-my-data.json"', 'Cache-Control': 'private, no-store'})


REAUTH_SECONDS = 600  # deleting an account needs a sign-in at most this old


@app.post('/api/account/delete')
async def account_delete(request: Request):
    """Delete my account (Art 17 / DPDP s12) after a typed DELETE and a sign-in from the last 10 minutes: customers
    have no password to re-check, so a fresh Braivex sign-in (the assertion's iat, kept in the session) is the proof."""
    b = await request.json()
    if (b.get('confirm') or '').strip() != 'DELETE':
        raise HTTPException(400, 'Type DELETE to confirm')
    signed = auth.unseal('session', request.cookies.get(auth.COOKIE, '')) or {}
    if time.time() - signed.get('at', 0) > REAUTH_SECONDS:
        if request.state.is_admin:
            return JSONResponse({'detail': 'Sign out and sign in again with the operator password, then delete within '
                                           '10 minutes.'}, status_code=403)
        return JSONResponse({'detail': 'To confirm it is you, sign in with Braivex again, then delete within 10 minutes.',
                             'reauth': '/auth/braivex/start?next=/settings'}, status_code=403)
    try:
        warning = await run_in_threadpool(admin.erase, request.state.user, request.state.user)  # calls Google
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _end_session(JSONResponse({'ok': True, 'warning': warning, 'redirect': '/login?notice=deleted'}))


@app.post('/api/users/plan')
async def api_user_plan(request: Request):
    _require_admin(request)
    b = await request.json()
    u, pl = (b.get('user') or '').strip().lower(), b.get('plan') or 'free'
    if pl not in plans.PLANS:
        raise HTTPException(400, 'Unknown plan')
    cr = b.get('credits')
    try:
        credits = int(cr) if cr not in (None, '') else (plans.PLANS[pl]['videos'] or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, 'Credits must be a whole number')
    if not 0 <= credits <= 100000:
        raise HTTPException(400, 'Credits must be between 0 and 100000')
    if u not in {x['user'] for x in auth.users()}:
        raise HTTPException(404, 'No such user')
    store.ensure_account(u)
    store.set_plan(u, pl, credits, note='set by admin')
    store.admin_event('plan', request.state.user, u, plan=pl, credits=credits)
    return {'ok': True, 'account': plans.account_view(u)}


@app.get('/api/users')
def api_users(request: Request):
    _require_admin(request)
    accs = {a['user']: a for a in store.all_accounts()}
    return {'users': [{**u, **{k: accs.get(u['user'], {}).get(k) for k in ('plan', 'credits', 'blocked')}} for u in auth.users()],
            'me': request.state.user, 'plan_keys': plans.ORDER}


@app.post('/api/users')
async def api_users_add(request: Request):
    _require_admin(request)
    b = await request.json()
    role = 'admin' if b.get('role') == 'admin' else 'member'
    try:
        # A customer gets no password: they sign in with Braivex at this address. An operator needs one (break-glass),
        # hashed in the thread pool: 600,000 PBKDF2 rounds must not hold the event loop.
        await run_in_threadpool(auth.create_user, b.get('user', ''), (b.get('password') or '') if role == 'admin' else None, role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'users': auth.users()}


@app.post('/api/users/delete')
async def api_users_del(request: Request):
    """Deactivate: stop the owner's jobs and revoke their Drive grant; business history is retained."""
    _require_admin(request)
    target = ((await request.json()).get('user') or '').strip().lower()
    try:
        warning = await run_in_threadpool(admin.deactivate, target, request.state.user)  # calls Google
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'users': auth.users(), 'warning': warning}


@app.post('/api/users/erase')
async def api_users_erase(request: Request):
    """Erase, unlike Remove: personal data deleted or anonymised; paid orders kept for the tax record period."""
    _require_admin(request)
    target = ((await request.json()).get('user') or '').strip().lower()
    try:
        warning = await run_in_threadpool(admin.erase, target, request.state.user)  # calls Google
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'users': auth.users(), 'warning': warning}


@app.post('/api/users/password')
async def api_users_pw(request: Request):
    _require_admin(request)
    b = await request.json()
    try:
        await run_in_threadpool(auth.set_password, b.get('user', ''), b.get('password', ''))  # PBKDF2: off the event loop
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.admin_event('password_reset', request.state.user, b.get('user', ''))
    return {'ok': True}


# ---------------- pages ----------------

def default_message():
    return os.getenv('DEFAULT_MESSAGE') or hostmsg.DEFAULT_MESSAGE


@app.get('/', response_class=HTMLResponse)
def landing(request: Request):
    return tpl.TemplateResponse(request, 'landing.html', {
        'signed_in': bool(request.state.user), 'user': request.state.user, 'plans': plans.public_plans(),
        'sample_video': os.getenv('SAMPLE_VIDEO_URL') or None,
        'sample_poster': os.getenv('SAMPLE_POSTER_URL') or None, 'limits': photos})


def _notice_facts():
    """What the privacy notice says about payments and the India backup, read from the configuration it describes."""
    from urllib.parse import urlsplit
    links = sorted({urlsplit(billing.checkout_link(p)).hostname for p in ('starter', 'commercial') if billing.checkout_link(p)})
    backup = invoices.config() or {}
    return {'payments': {'stripe': billing.stripe_enabled(), 'links': links},
            'backup_aws': not backup.get('endpoint') or '.amazonaws.com' in backup['endpoint']}  # AWS unless a non-AWS endpoint is set


@app.get('/privacy', response_class=HTMLResponse)
def privacy(request: Request):
    return tpl.TemplateResponse(request, 'legal.html', {'kind': 'privacy', **_notice_facts()})


@app.get('/terms', response_class=HTMLResponse)
def terms(request: Request):
    return tpl.TemplateResponse(request, 'legal.html', {'kind': 'terms'})


def _request_page(request, status=200, **ctx):
    return tpl.TemplateResponse(request, 'privacy_request.html', {'types': store.PRIVACY_REQUEST_TYPES, 'f': {}, **ctx},
                                status_code=status)


@app.get('/privacy/request', response_class=HTMLResponse)
def privacy_request_page(request: Request):
    """Rights requests and complaints from anyone, signed in or not (UK DPA 2018 s.164A; Art 12: one month)."""
    return _request_page(request)


@app.post('/privacy/request')
async def privacy_request_post(request: Request):
    f = {k: (v or '').strip() for k, v in (await _form(request)).items() if k != 'csrf'}
    net = store.net_of(_ip(request))  # per /24 or /64: a new IPv6 address each time is still one network
    if auth.too_many(net, 'privacy'):
        return _request_page(request, 429, f=f, error='Too many requests from this network. Try again in 10 minutes, or email hello@braivex.com.')
    profile = linkedin.airbnb_profile(f.get('airbnb_profile', ''))
    company = companies.number(f.get('company_number'))
    removal = f.get('type') == 'listing_removal'
    listing = jobs.listing_id(f.get('listing_url', ''))
    error = ('Choose what the request is about' if f.get('type') not in store.PRIVACY_REQUEST_TYPES else
             'Enter a valid email address so we can reply' if not auth.EMAIL.match(f.get('email', '')) else
             'Paste the link to your Airbnb listing (airbnb.co.uk/rooms/<number>)' if removal and not listing else
             'Tell us what you would like us to do' if not f.get('details') and not removal else
             'Paste the link to your Airbnb profile (airbnb.co.uk/users/show/<number>), or leave it empty'
             if f.get('airbnb_profile') and not profile else
             'Enter the 8-character company number from Companies House (for example 01234567 or SC123456), or leave it empty'
             if f.get('company_number') and not company else
             'Paste the link to your Airbnb listing (airbnb.co.uk/rooms/<number>), or leave it empty'
             if f.get('listing_url') and not listing else None)
    if error:
        return _request_page(request, 400, f=f, error=error)
    auth.record_fail(net, 'privacy')  # counts submissions, not failures
    details = f.get('details') or 'Remove my listing from ReelSieve.'
    ref, received = store.add_privacy_request(f['type'], auth.norm(f['email']), f.get('name', '')[:200] or None, details[:4000],
                                              profile.rsplit('/', 1)[-1] if profile else None, company_number=company,
                                              listing_id=listing, user=request.state.user)
    # An objection to direct marketing is honoured at once, for every user. Nothing proves who sent it, so each entry
    # carries the request reference: Settings shows it next to the request and an admin can undo an abusive one.
    if f['type'] == 'objection' and profile:
        store.suppress({'airbnb_profile': profile}, ref=ref)
    if f['type'] == 'objection' and company:
        store.suppress({'company_number': company}, ref=ref)
    if removal:
        # Anyone can send this form and nothing proves the listing is theirs, so the block is at once but provisional:
        # it lapses at the reply deadline unless an admin confirms it, and a few per address and per day block at once
        # (the rest wait for the admin's check).
        mine, total = store.recent_removals(auth.norm(f['email']), received - 86400)
        if mine <= REMOVALS_PER_EMAIL and total <= REMOVALS_PER_DAY:
            store.block_listing(listing, 'Removal request ' + ref, expires_at=store.one_month_after(received))
    return _request_page(request, ack={'ref': ref, 'received': received, 'due': store.one_month_after(received),
                                       'listing': listing if removal else None,
                                       'listing_blocked': bool(removal and store.blocked_ids([listing]))})


@app.post('/api/privacy-requests/handled')
async def privacy_request_handled(request: Request):
    _require_admin(request)
    ref = ((await request.json()).get('ref') or '').strip()
    if not store.handle_privacy_request(ref):
        raise HTTPException(404, 'No open request with that reference')
    store.admin_event('privacy_request_handled', request.state.user, None, ref=ref)
    return {'ok': True}


@app.post('/api/privacy-requests/unsuppress')
async def privacy_request_unsuppress(request: Request):
    """Admin: a privacy request turned out abusive, so the do-not-contact entries it made are removed (logged)."""
    _require_admin(request)
    ref = str((await request.json()).get('ref') or '').strip()
    n = store.undo_request_suppressions(ref) if ref else 0
    if not n:
        raise HTTPException(404, 'No do-not-contact entries came from that request')
    store.admin_event('request_unsuppress', request.state.user, None, ref=ref, rows=n)
    return {'ok': True, 'removed': n}


@app.post('/api/blocked-listings')
async def blocked_listing_add(request: Request):
    """Admin: no reels of this listing, and not in co-host or Outreach results. Takes a /rooms/<id> link or the number."""
    _require_admin(request)
    b = await request.json()
    raw = str(b.get('listing') or '').strip()
    lid = raw if re.fullmatch(r'\d{1,20}', raw) else jobs.listing_id(raw)
    if not lid:
        raise HTTPException(400, 'Paste an Airbnb listing link (airbnb.…/rooms/<number>) or its number')
    store.block_listing(lid, str(b.get('reason') or '').strip()[:300] or 'Added by an admin')
    store.admin_event('listing_block', request.state.user, None, listing=lid)
    return {'ok': True, 'listing_id': lid}


@app.post('/api/blocked-listings/remove')
async def blocked_listing_remove(request: Request):
    _require_admin(request)
    lid = str((await request.json()).get('listing_id') or '').strip()
    if not store.unblock_listing(lid):
        raise HTTPException(404, 'That listing is not blocked')
    store.admin_event('listing_unblock', request.state.user, None, listing=lid)
    return {'ok': True}


@app.post('/api/blocked-listings/confirm')
async def blocked_listing_confirm(request: Request):
    """Admin: a public removal request checked and upheld, so its block no longer lapses."""
    _require_admin(request)
    lid = str((await request.json()).get('listing_id') or '').strip()
    if not store.confirm_listing_block(lid):
        raise HTTPException(404, 'No provisional block for that listing')
    store.admin_event('listing_confirm', request.state.user, None, listing=lid)
    return {'ok': True}


@app.post('/api/airbnb/resume')
def airbnb_resume(request: Request):
    """Admin: after checking why Airbnb refused us, start Airbnb fetching again (a hard stop never ends on its own)."""
    _require_admin(request)
    airbnb.resume()
    store.admin_event('airbnb_resume', request.state.user, None)
    return {'ok': True}


def _upgrade_page(request, plan='', order=None, note='We send the invoice within a few hours and add your credits the moment it clears.', **extra):
    u = request.state.user
    return tpl.TemplateResponse(request, 'upgrade.html', {
        'plan': plan, 'plans': plans.public_plans(), 'link': billing.checkout_link(plan) if plan else '', 'order': order,
        'card': billing.stripe_enabled(), 'billing_note': os.getenv('BILLING_NOTE') or note,
        'csrf': csrf_for(request), 'account': plans.account_view(u) if u else None,
        'orders': billing.orders(u, limit=10) if u else [], **extra})  # orders(None) is every customer's


@app.get('/upgrade', response_class=HTMLResponse)
def upgrade(request: Request, plan: str = '', ref: str = '', cancelled: int = 0):
    """No plan: the in-app plans page. A plan: how to pay for it. A ref: that order's state (owner or admin only)."""
    o = billing.get_order_for(ref, request.state.user, request.state.is_admin) if ref else None
    return _upgrade_page(request, plan if plan in plans.PLANS else '', o, cancelled=bool(cancelled))


@app.get('/upgrade/paid', response_class=HTMLResponse)
def upgrade_paid(request: Request, ref: str = '', session_id: str = ''):
    """Return from a payment page. Stripe: confirm with Stripe now; the signed webhook stays the guarantee."""
    u = request.state.user
    o = billing.get_order_for(ref, u, request.state.is_admin) if ref else None
    if o and o['user'] == u and o['provider'] == 'stripe':
        if session_id and o['status'] == 'pending' and billing.stripe_enabled():
            try:
                s = billing.stripe_session(session_id)
                if (s.get('metadata') or {}).get('order_ref') == o['ref']:
                    billing.fulfil_stripe_session(s, by='stripe-return')
            except (ValueError, RuntimeError):
                pass  # stays pending; the webhook settles it
            o = billing.get_order(o['ref'])
    elif o and o['user'] == u:
        o = billing.mark_reported(ref)
    return _upgrade_page(request, (o or {}).get('plan', ''), o, 'We confirm the payment and add your credits, usually within a few hours.',
                         link='', reported=True)


# ---------------- billing ----------------

@app.post('/api/billing/start')
async def billing_start(request: Request):
    """Create the order first, then a pay URL carrying its reference — that maps the payment to this tenant."""
    b = await request.json()
    pl = (b.get('plan') or '').strip()
    if pl not in ('starter', 'commercial'):
        raise HTTPException(400, 'Choose Starter or Commercial')
    base = public_base() or str(request.base_url).rstrip('/')
    if billing.stripe_enabled():
        try:
            o, url = billing.start_stripe_checkout(request.state.user, pl, base)
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return {'ok': True, 'order': billing.view(o), 'pay_url': url}
    if not billing.checkout_link(pl):
        raise HTTPException(400, 'No payment link configured for that plan — request an invoice instead')
    try:
        o = billing.create_order(request.state.user, pl, 'link', (b.get('note') or '')[:400])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'ok': True, 'order': billing.view(o), 'pay_url': billing.pay_url(pl, o['ref'], base)}


@app.post('/api/billing/request')
async def billing_request(request: Request):
    b = await request.json()
    pl = (b.get('plan') or '').strip()
    if pl not in ('starter', 'commercial'):
        raise HTTPException(400, 'Choose Starter or Commercial')
    try:
        o = billing.create_order(request.state.user, pl, b.get('provider') or 'invoice', (b.get('note') or '')[:400])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'ok': True, 'order': billing.view(o)}


@app.get('/api/billing/orders')
def billing_orders(request: Request, all: int = 0):
    if all and request.state.is_admin:
        return {'orders': billing.orders(), 'pending': billing.pending_count()}
    return {'orders': billing.orders(request.state.user)}


@app.post('/api/billing/settle')
async def billing_settle(request: Request):
    _require_admin(request)
    b = await request.json()
    try:
        o = billing.settle((b.get('ref') or '').strip(), by='admin')
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.admin_event('order_settle', request.state.user, o['user'], ref=o['ref'])
    return {'ok': True, 'order': billing.view(o), 'account': plans.account_view(o['user'])}


@app.post('/api/billing/cancel')
async def billing_cancel(request: Request):
    _require_admin(request)
    b = await request.json()
    o = billing.cancel((b.get('ref') or '').strip(), b.get('note') or 'cancelled')
    store.admin_event('order_cancel', request.state.user, o['user'], ref=o['ref'])
    return {'ok': True, 'order': billing.view(o)}


@app.post('/api/billing/link')
async def billing_link(request: Request):
    """Attach a single-use payment link to one order: the link itself is the tenant mapping."""
    _require_admin(request)
    b = await request.json()
    try:
        o = billing.set_pay_link((b.get('ref') or '').strip(), b.get('url') or '')
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.admin_event('order_link', request.state.user, o['user'], ref=o['ref'])
    return {'ok': True, 'order': billing.view(o)}


@app.post('/api/billing/webhook/{provider}')
async def billing_webhook(provider: str, request: Request):
    """Provider-agnostic: HMAC-SHA256 over the raw body, our order ref anywhere in the payload. Stripe has its own scheme."""
    raw = await request.body()
    if provider == 'stripe':
        return _stripe_webhook(raw, request.headers.get('stripe-signature', ''))
    sig = (request.headers.get('x-signature') or request.headers.get('x-razorpay-signature') or
           request.headers.get('x-skydo-signature') or request.headers.get('x-dodo-signature') or
           request.headers.get('x-webhook-signature') or '')
    if not billing.verify(provider, raw, sig):
        raise HTTPException(401, 'Bad signature')
    try:
        payload = json.loads(raw or b'{}')
    except ValueError:
        raise HTTPException(400, 'Bad payload')
    ref = billing.ref_from_payload(payload)
    if not ref:
        raise HTTPException(400, 'No order reference in payload')
    order = billing.get_order(ref)
    if not order:
        raise HTTPException(404, 'No such order')
    # The signature says the provider sent it; these checks say the money arrived for this order (28 Sep review F1).
    problems = billing.settlement_problems(payload, order)
    if problems:
        raise HTTPException(400, 'Payload does not confirm payment of ' + ref + ': ' + ', '.join(problems))
    try:
        o = billing.settle(ref, by=f'webhook:{provider}', provider=provider)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, 'ref': o['ref'], 'status': o['status']}


STRIPE_EVENTS = ('checkout.session.completed', 'checkout.session.async_payment_succeeded')


def _stripe_webhook(raw, signature):
    """Signed, fresh and ours, or nothing happens. A replay settles nothing new: settle() grants once per order."""
    if not billing.stripe_enabled() or not billing.stripe_signature_ok(raw, signature):
        raise HTTPException(400, 'Bad signature')
    try:
        event = json.loads(raw)
        session = event['data']['object'] if event.get('type') in STRIPE_EVENTS else None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, 'Bad payload')
    if not isinstance(session, dict):
        return {'ok': True, 'ignored': 'event type'}
    try:
        o = billing.fulfil_stripe_session(session, by='stripe-webhook')
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not o:
        return {'ok': True, 'ignored': 'not a ReelSieve order'}
    return {'ok': True, 'ref': o['ref'], 'status': o['status']}


# ---------------- jobs ----------------

def listing_id_of(url):
    m = re.search(r'/rooms/(\d+)', url or '')
    return m.group(1) if m else None


def search_phrase(listing):
    title = re.split(r'\s[|·-]\s', (listing.get('title') or ''))[0].strip()
    city = (listing.get('city') or '').strip()
    return ' '.join(x for x in [title, city, 'video walkthrough ReelSieve'] if x).strip() or 'ReelSieve'


def job_view(j, receipts=None):
    """Everything the UI shows about one job. Links come only from the owner's confirmed Drive receipts."""
    p, m = j['params'] or {}, j['meta'] or {}
    own = p.get('source') == 'photos'  # the customer's own photos: no listing link, host or Airbnb page
    listing = {**({'url': None, 'title': p.get('title'), 'city': p.get('location')} if own else {'url': j['url']}),
               **(m.get('listing') or {})}
    listing['location'] = listing.get('city')
    recs = receipts if receipts is not None else {}
    primary = recs.get('primary')
    shared = bool(primary and primary['sharing'] == 'public')
    link = primary['webViewLink'] if shared else None
    phrase = search_phrase(listing)
    msg = m.get('message') or p.get('message') or default_message()
    if '{reel_link}' in msg and '{search_phrase}' not in msg and 'ReelSieve' not in msg:
        msg = default_message()
    host = (listing.get('host') or '').strip()
    final = (msg.replace('{host_name}', host or 'there').replace('Hi there!', 'Hi!')
             .replace('{listing_title}', listing.get('title') or 'your listing').replace('{city}', listing.get('city') or '')
             .replace('{search_phrase}', phrase).replace('{reel_link}', link or '(reel link not shared yet)'))
    lid = listing_id_of(j['url'])
    # A host who objected (their Airbnb profile, or their name on this listing) is never offered as someone to contact.
    hid = str(listing.get('host_id') or '')
    quiet = not own and bool(hid or host) and not store.unsuppressed(
        [{'airbnb_profile': f'/users/show/{hid}' if hid.isdigit() else '', 'name': host, 'listing_url': j['url']}])
    if quiet:
        msg = final = ''
    return {
        'id': j['id'], 'status': j['status'], 'progress': j['progress'], 'step': j['step'], 'log': j['log'] or [],
        'error': j['error'], 'listing': listing, 'style': p.get('style', 'v2'), 'duration': m.get('duration'),
        'created': time.strftime('%d %b %Y %H:%M UTC', time.gmtime(j['created'])), 'audit': m.get('audit'),
        'ai_plan': m.get('ai_plan'), 'selection': m.get('selection'), 'photo_scores': m.get('photo_scores'),
        'cancellable': j['status'] in ('queued', 'running') and not j['cancel_requested'], 'cancel_requested': j['cancel_requested'],
        'deliveries': [{'variant': v, 'name': r['name'], 'drive_link': r['webViewLink'], 'sharing': r['sharing']} for v, r in sorted(recs.items())],
        'stream_url': f"/api/jobs/{j['id']}/video" if primary else None,
        'download_url': f"/api/jobs/{j['id']}/video?download=1" if primary else None,
        'drive_link': primary['webViewLink'] if primary else None, 'shared': shared, 'reel_link': link,
        'poster': img_src(listing['photo'] + ('?im_w=1200' if '?' not in listing['photo'] else '')) if listing.get('photo') else None,
        'host_status': m.get('host_status'), 'host_error': m.get('host_error'),
        'message': msg, 'message_final': final, 'search_phrase': phrase,
        'contact_url': hostmsg.contact_url(lid) if lid and not quiet else None, 'host_suppressed': quiet,
        'source': 'photos' if own else 'listing', 'key': lid or j['url'],
        'delete_inputs': bool(p.get('delete_inputs')), 'inputs': m.get('inputs')}


def _views(user, rows):
    recs = gdrive.receipts_for(user, [r['id'] for r in rows if r['status'] == 'done'])
    return [job_view(r, recs.get(r['id'], {})) for r in rows]


def _own_job(request, jid):
    j = jobs.get(request.state.user, jid)
    if not j:
        raise HTTPException(404)
    return j


def _one(request, j):
    return _views(request.state.user, [j])[0]


@app.get('/app', response_class=HTMLResponse)
def index(request: Request, url: str = '', mode: str = ''):
    u = request.state.user
    return tpl.TemplateResponse(request, 'index.html', {
        'jobs': _views(u, jobs.list_for(u, 12)), 'hf_configured': bool(os.getenv('HF_KEY')), 'gdrive': gdrive.status(u),
        'default_message': default_message(), 'prefill_url': url[:500], 'account': plans.account_view(u),
        'photos_mode': mode == 'photos' and not url, 'limits': photos})


@app.post('/api/jobs')
async def create_job(request: Request):
    b = await request.json()
    try:
        j = jobs.admit(request.state.user, b.get('url'), b, request.headers.get('idempotency-key') or b.get('idempotency_key'),
                       _ip(request))
    except jobs.AdmissionError as e:
        raise HTTPException(e.status, str(e))
    return {'id': j['id'], 'account': plans.account_view(request.state.user)}


PHOTO_BODY_MAX = photos.MAX_TOTAL + 1024 * 1024  # the photos plus the typed fields and multipart framing
TOO_BIG = 'Your photos add up to more than 250 MB. Choose fewer or smaller photos.'
PHOTO_SLOT_WAIT = 20         # seconds a request waits for a free upload slot before it is told to try again
PHOTO_MIN_RATE = 64 * 1024   # bytes a second an upload must average (after the grace period) to keep its slot
PHOTO_READ_GRACE = 30        # seconds before that rate counts: connection set-up and a slow first chunk
PHOTO_DEADLINE = float(os.getenv('PHOTO_UPLOAD_DEADLINE', '600'))  # seconds one upload may take in all, however it trickles
PHOTO_PER_NETWORK = int(os.getenv('PHOTO_UPLOADS_PER_NETWORK', '1'))  # uploads at once from one /24 or /64
# ponytail: each upload is held in memory once (up to ~250 MB); two at a time bounds the web process. Raise with its memory.
_photo_slots = asyncio.Semaphore(int(os.getenv('PHOTO_UPLOAD_SLOTS', '2')))
_uploading = set()  # owners with an upload in progress in this (single) web process: one at a time each
_uploading_nets = {}  # network -> uploads in progress from it


class _Photo:
    """One photo part, kept as the bytes arrive: MultiPartParser.parse writes each chunk here instead of to a spooled
    file, so the body is held once and nothing is copied out again. Past photos.MAX_BYTES it is refused at once."""
    def __init__(self, filename):
        self.filename, self.data = filename, bytearray()

    async def write(self, chunk):
        self.data += chunk
        if len(self.data) > photos.MAX_BYTES:
            raise MultiPartException('A photo is larger than 15 MB')

    async def seek(self, _offset):  # parse() rewinds each finished file; there is nothing to rewind
        pass


class _PhotoForm(MultiPartParser):
    """The photo upload's parser: fields stay small (max_part_size), photo parts go straight into _Photo buffers, so no
    part is spooled and nothing is written to disk."""

    def on_headers_finished(self):
        super().on_headers_finished()
        part = self._current_part
        if part.file is not None:
            self._files_to_close_on_error.pop().close()  # the empty spooled file the base class made is never used
            part.file = _Photo(part.file.filename)


async def _capped(stream, limit):
    """The body, refused past `limit` bytes, once it falls behind PHOTO_MIN_RATE, or at PHOTO_DEADLINE however it
    trickles: a stalled or slow client cannot hold an upload slot (uvicorn itself has no body-read timeout)."""
    got, start, chunks = 0, time.monotonic(), stream.__aiter__()
    while True:
        due = min(start + PHOTO_READ_GRACE + got / PHOTO_MIN_RATE, start + PHOTO_DEADLINE)
        try:
            async with asyncio.timeout(max(0.01, due - time.monotonic())):
                chunk = await chunks.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise HTTPException(408, 'The upload was too slow and has stopped. Check your connection, or choose fewer or '
                                     'smaller photos, and try again.')
        got += len(chunk)
        if got > limit:
            raise HTTPException(413, TOO_BIG)
        yield chunk


@app.post('/api/jobs/photos')
async def create_photo_job(request: Request):
    """'Your own photos': one multipart request with the photos and the typed facts (app.jobs.admit_photos)."""
    length = request.headers.get('content-length', '')
    if length.isdigit() and int(length) > PHOTO_BODY_MAX:
        raise HTTPException(413, TOO_BIG)
    user, ip = request.state.user, _ip(request)  # signed in: the Gate refused anyone else before this runs
    net = store.net_of(ip)
    if user in _uploading:
        raise HTTPException(429, 'You are already uploading photos for a reel. Wait for that upload to finish.')
    _uploading.add(user)
    counted = False
    try:
        # the cheap refusals (Drive, credit) before this upload takes a slot or a byte of its body is read
        try:
            await run_in_threadpool(jobs.precheck_photos, user, request.headers.get('idempotency-key'), ip)
        except jobs.AdmissionError as e:
            raise HTTPException(e.status, str(e))
        if _uploading_nets.get(net, 0) >= PHOTO_PER_NETWORK:
            raise HTTPException(429, 'Someone on your network is already uploading photos. Try again when that upload has finished.')
        _uploading_nets[net], counted = _uploading_nets.get(net, 0) + 1, True
        try:
            async with asyncio.timeout(PHOTO_SLOT_WAIT):
                await _photo_slots.acquire()
        except TimeoutError:
            raise HTTPException(503, 'Photo uploads are busy right now. Try again in a minute.')
        try:
            try:
                form = await _PhotoForm(request.headers, _capped(request.stream(), PHOTO_BODY_MAX),
                                        max_files=photos.MAX_PHOTOS + 1, max_fields=photos.MAX_PHOTOS + 20,
                                        max_part_size=64 * 1024).parse()
            except MultiPartException:
                raise HTTPException(400, f'Choose {photos.MIN_PHOTOS} to {photos.MAX_PHOTOS} photos, each under 15 MB, '
                                         'and try again')
            files = [(f.filename or '', f.data) for f in form.getlist('photos') if isinstance(f, _Photo)]  # no copy
            fields = {k: form.get(k) for k in ('title', 'location', 'highlights', 'style', 'ai_resolution')}
            fields.update(delete_inputs=form.get('delete_inputs') != 'false',  # ai_motion: never on own photos (admit_photos)
                          quotes_real=form.get('quotes_real') == 'true', rooms=form.getlist('room'),
                          quotes=[{'text': t, 'stars': s} for t, s in zip(form.getlist('quote_text'), form.getlist('quote_stars'))])
            del form
            try:
                j = await run_in_threadpool(jobs.admit_photos, user, fields, files, request.headers.get('idempotency-key'), ip)
            except jobs.AdmissionError as e:
                raise HTTPException(e.status, str(e))
        finally:
            _photo_slots.release()
    finally:
        _uploading.discard(user)
        if counted:
            _uploading_nets[net] -= 1
            if not _uploading_nets[net]:
                del _uploading_nets[net]
    return {'id': j['id'], 'account': plans.account_view(user)}


@app.get('/api/jobs/{jid}')
def job_api(request: Request, jid: str):
    return _one(request, _own_job(request, jid))


@app.get('/jobs/{jid}', response_class=HTMLResponse)
def job_page(request: Request, jid: str):
    return tpl.TemplateResponse(request, 'job.html', {'job': _one(request, _own_job(request, jid))})


@app.post('/api/jobs/{jid}/cancel')
def job_cancel(request: Request, jid: str):
    j = jobs.cancel(request.state.user, jid)
    if not j:
        raise HTTPException(404)
    return _one(request, j)


@app.post('/api/jobs/{jid}/opened-in-browser')
async def opened_in_browser(jid: str, request: Request):
    """The customer opened the host's contact form in their own browser; the message was copied client-side."""
    _own_job(request, jid)
    msg = ((await request.json()).get('message') or '').strip()[:2000]
    j = jobs.set_meta(request.state.user, jid, host_status='draft', host_error='opened in your browser — paste and press Send',
                      **({'message': msg} if msg else {}))
    return _one(request, j)


@app.post('/api/jobs/{jid}/share')
async def job_share(jid: str, request: Request):
    """Owner-invoked anyone-with-link sharing. The link is shown only after Google confirms it."""
    j = _own_job(request, jid)
    if j['status'] != 'done':
        raise HTTPException(400, 'Reel not ready')
    b = await request.json()
    try:
        gdrive.set_sharing(request.state.user, jid, bool(b.get('public')), 'primary')
    except ValueError:
        raise HTTPException(404)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return _one(request, j)


@app.get('/api/jobs/{jid}/video')
def job_video(request: Request, jid: str, variant: str = 'primary', download: int = 0):
    """Stream the owner's delivered reel from their Drive. The file ID only ever comes from their receipt."""
    j = _own_job(request, jid)
    if variant not in ('primary', '720p'):
        raise HTTPException(404)
    stream = gdrive.open_stream(request.state.user, jid, variant, request.headers.get('range'))
    try:
        r = stream.__enter__()
    except ValueError:
        raise HTTPException(404)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    def body():
        try:
            yield from r.iter_bytes()
        finally:
            stream.__exit__(None, None, None)
    headers = {k: r.headers[k] for k in ('content-length', 'content-range', 'accept-ranges') if k in r.headers}
    # Our own reels only, so the type is ours to state: Drive's header is never forwarded to the browser (28 Sep review F7).
    headers['content-type'] = 'video/mp4'
    headers['cache-control'] = 'private, no-store'
    if download:
        name = re.sub(r'[^A-Za-z0-9._-]+', '-', ((j['meta'] or {}).get('listing') or {}).get('title') or 'reel')[:60].strip('-')
        headers['content-disposition'] = f'attachment; filename="{name or "reel"}-{variant}.mp4"'
    return StreamingResponse(body(), status_code=r.status_code, headers=headers)


def library(user):
    groups, order = {}, []
    for v in _views(user, jobs.list_for(user, 200)):
        lid = v['key']  # the listing, or for own-photo reels the photo set
        if lid not in groups:
            groups[lid] = {'listing': {**v['listing'], 'id': lid}, 'jobs': [], 'latest': v, 'poster': v['poster']}
            order.append(lid)
        g = groups[lid]
        g['jobs'].append(v)
        if v['listing'].get('title') and not g['listing'].get('title'):
            g['listing'].update({k: x for k, x in v['listing'].items() if x})
        g['poster'] = g['poster'] or v['poster']
    return [groups[k] for k in order]


@app.get('/reels', response_class=HTMLResponse)
def reels_page(request: Request):
    return tpl.TemplateResponse(request, 'reels.html', {'groups': library(request.state.user)})


@app.get('/api/reels/index')
def reels_index(request: Request):
    """listing id → finished reels (search results' 'Reel ready' marker). Scoped to the signed-in owner."""
    out = {}
    for v in _views(request.state.user, jobs.list_for(request.state.user, 200)):
        lid = listing_id_of(v['listing']['url'])
        if lid and v['status'] == 'done':
            out.setdefault(lid, []).append({'id': v['id'], 'created': v['created'], 'drive_link': v['drive_link']})
    return out


# ---------------- listing photos ----------------

# ponytail: the one Airbnb CDN host seen in listing, search and co-host data; add a host here when another appears.
IMG_HOSTS = ('a0.muscache.com',)
IMG_MAX = 8 * 1024 * 1024
IMG_MAGIC = ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'), (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif'))


def img_src(url):
    """Pages show listing photos through /img, so a visitor's browser never contacts Airbnb's CDN."""
    return '/img?u=' + quote(url, safe='') if url else None


def _image_type(body):
    """From the bytes, not the upstream header: only raster formats a browser shows (never SVG or HTML)."""
    for magic, kind in IMG_MAGIC:
        if body.startswith(magic):
            return kind
    if body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/avif' if body[4:12] in (b'ftypavif', b'ftypavis') else None


@app.get('/img')
def image_proxy(request: Request, u: str = ''):
    """Signed-in only (the Gate). https to IMG_HOSTS only, every redirect re-checked (app.fetch), size-capped."""
    try:
        with at_once(request.state.user, 'img'):
            # WebP, not AVIF: the CDN answers AVIF when asked, which Safari before 16 cannot show.
            _, body = fetch.get(u, headers={'User-Agent': listing_search.UA['User-Agent'], 'Accept': 'image/webp,image/jpeg,image/png'},
                                timeout=20, max_bytes=IMG_MAX, hosts=IMG_HOSTS, record_blocks=False)  # the user chose u
    except airbnb.Unavailable as e:
        raise HTTPException(503, str(e))
    except ValueError:
        raise HTTPException(400, 'Not an allowed image')
    except httpx.HTTPError:
        raise HTTPException(502, 'Image unavailable')
    kind = _image_type(body)
    if not kind:
        raise HTTPException(400, 'Not an allowed image')
    return Response(body, media_type=kind, headers={'Cache-Control': 'private, max-age=86400'})


# ---------------- listing search ----------------

_places_cache = {}


@app.get('/api/places')
def api_places(q: str = ''):
    """Location autocomplete (Photon / OpenStreetMap, no key). Returns 'City, Country' values Airbnb's search accepts."""
    import httpx
    q = q.strip()[:80]
    if len(q) < 2:
        return {'items': []}
    if q.lower() in _places_cache:
        return _places_cache[q.lower()]
    try:
        r = httpx.get('https://photon.komoot.io/api/', params={'q': q, 'limit': 10, 'lang': 'en', 'osm_tag': 'place'},
                      headers={'User-Agent': 'ListingReel/1.0 (braivex.com)'}, timeout=8)
        feats = r.json().get('features', [])
    except Exception:
        return {'items': []}
    out, seen = [], set()
    for f in feats:
        pr = f.get('properties', {})
        name, country = pr.get('name'), pr.get('country')
        region = pr.get('state') or pr.get('county') or ''
        if not name or not country or pr.get('osm_value') not in ('city', 'town', 'village', 'suburb', 'borough', 'quarter',
                                                                  'neighbourhood', 'island', 'county', 'state', 'municipality'):
            continue
        value = f'{name}, {country}'
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        c = f.get('geometry', {}).get('coordinates') or [None, None]
        out.append({'label': ', '.join(x for x in [name, region if region and region != name else '', country] if x),
                    'value': value, 'lat': c[1], 'lng': c[0]})
        if len(out) >= 6:
            break
    res = {'items': out}
    if len(_places_cache) < 2000:
        _places_cache[q.lower()] = res
    return res


def _airbnb_on():
    """Kill switch: features that read Airbnb refuse with the same notice the page shows."""
    if not airbnb.enabled():
        raise HTTPException(503, airbnb.DISABLED)


def _unblocked(res):
    """A listing taken down from ReelSieve never shows in Find a listing (so neither do its photos)."""
    items = store.without_blocked_listings(res.get('items') or [])
    return {**res, 'items': items, **({'count': len(items)} if 'count' in res else {})}


@app.get('/api/search')
def api_search(request: Request, location: str, checkin: str = '', checkout: str = '', adults: int = 2, offset: int = 0, pages: int = 3):
    """In-app listing picker: public Airbnb search results (no login)."""
    _airbnb_on()
    if not location.strip():
        raise HTTPException(400, 'Enter a location')
    with at_once(request.state.user, 'lookup'):
        try:
            return _unblocked(listing_search.search(location[:120], checkin or None, checkout or None, adults, offset, min(max(pages, 1), 5)))
        except airbnb.Unavailable as e:
            raise HTTPException(503, str(e))
        except Exception:
            raise HTTPException(502, 'Search failed — try again')


@app.get('/api/search/more')
def api_search_more(request: Request, location: str, page: int, checkin: str = '', checkout: str = '', adults: int = 2):
    _airbnb_on()
    with at_once(request.state.user, 'lookup'):
        try:
            return _unblocked(listing_search.search_page(location[:120], checkin or None, checkout or None, adults, page))
        except airbnb.Unavailable as e:
            raise HTTPException(503, str(e))
        except Exception:
            raise HTTPException(502, 'Load more failed — try again')


# ---------------- Google Drive ----------------

def _redirect_uri(request):
    base = public_base()
    if not base:
        # Development only: Google matches redirect URIs exactly and 127.0.0.1 is not localhost.
        base = str(request.base_url).rstrip('/').replace('127.0.0.1', 'localhost')
    return base + '/oauth/google/callback'


def _drive_home(request):
    return '/settings' if request.state.is_admin else '/account'


@app.get('/oauth/google/start')
def google_start_get(request: Request):
    return RedirectResponse(_drive_home(request), status_code=303)


@app.post('/oauth/google/start')
def google_start(request: Request):
    """Each account connects its own Google account; the state is bound to this exact session."""
    try:
        url = gdrive.auth_url(_redirect_uri(request), request.state.user, request.cookies.get(auth.COOKIE, ''))
    except (ValueError, RuntimeError) as e:
        return RedirectResponse(_drive_home(request) + '?flash=' + quote(str(e)), status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get('/oauth/google/callback')
def google_callback(request: Request, code: str = '', state: str = '', error: str = ''):
    dest = _drive_home(request)
    if not request.state.user:
        return RedirectResponse('/login?next=' + quote(dest), status_code=303)
    if error or not code:
        return RedirectResponse(dest + '?flash=' + quote('Google sign-in was cancelled — nothing changed'), status_code=303)
    try:
        gdrive.exchange(code, state, _redirect_uri(request), request.state.user, request.cookies.get(auth.COOKIE, ''))
    except (ValueError, RuntimeError) as e:
        return RedirectResponse(dest + '?flash=' + quote('Google Drive connect failed: ' + str(e)[:300]), status_code=303)
    return RedirectResponse(dest + '?saved=1', status_code=303)


@app.get('/api/gdrive/status')
def gdrive_status(request: Request):
    return gdrive.status(request.state.user)


@app.post('/api/gdrive/disconnect')
def gdrive_disconnect(request: Request):
    """Photos the customer asked us to delete go first, while the grant still reaches them (as deactivate and erase)."""
    warnings = [admin.drop_photo_inputs(database.user_id(request.state.user))]
    try:
        gdrive.disconnect(request.state.user)
    except RuntimeError as e:
        warnings.append(str(e))
    return {**gdrive.status(request.state.user), 'warning': ' '.join(w for w in warnings if w) or None}


# ---------------- outreach (drafted here, sent by the customer) ----------------

@app.get('/outreach', response_class=HTMLResponse)
def outreach_page(request: Request):
    u = request.state.user
    rows = [{**r, 'link_label': linkedin.link_label(r['url']), 'airbnb_profile': linkedin.airbnb_profile_of(r)}
            for r in store.outreach_rows(u)]
    return tpl.TemplateResponse(request, 'outreach.html', {
        'csrf': csrf_for(request), 'stats': store.outreach_stats(u), 'cities': store.cities(u), 'rows': rows,
        'default_message': os.getenv('COHOST_MESSAGE') or COHOST_MESSAGE, 'linkedin_default': linkedin.CONNECT_DEFAULT,
        'daily_cap': DAILY_CAP, 'cap': DAILY_CAP, 'sent_today': store.sent_today(u),
        'b2b_template': companies.TEMPLATE, 'b2b_categories': companies.CATEGORIES, 'b2b_snapshot': companies.meta().get('snapshot'),
        'b2b_sender': (store.get_account(u) or {}).get('b2b_sender') or {}})


def outreach_allowed(items):
    """People who objected never reappear, nor do listings taken down from ReelSieve."""
    return store.unsuppressed(store.without_blocked_listings(items))


@app.get('/api/outreach/cohosts')
def api_cohosts(request: Request, city: str = ''):
    _airbnb_on()
    if not city.strip():
        raise HTTPException(400, 'Enter a city')
    with at_once(request.state.user, 'lookup'):
        try:
            res = cohost.discover(city.strip()[:120])
        except airbnb.Unavailable as e:
            raise HTTPException(503, str(e))
        except Exception:
            raise HTTPException(502, 'Lookup failed — try again')
    return {**res, 'items': outreach_allowed(res.get('items') or [])}


@app.get('/api/outreach/linkedin')
def api_linkedin(request: Request, city: str = '', role: str = 'property manager'):
    _airbnb_on()
    if not city.strip():
        raise HTTPException(400, 'Enter a city')
    with at_once(request.state.user, 'lookup'):
        try:
            res = linkedin.build(city.strip()[:120], (role.strip() or 'property manager')[:80])
        except airbnb.Unavailable as e:
            raise HTTPException(503, str(e))
        except Exception:
            raise HTTPException(502, 'Lookup failed — try again')
    return {**res, 'items': outreach_allowed(res.get('items') or [])}


@app.post('/api/outreach/queue')
async def api_queue(request: Request):
    b, u = await request.json(), request.state.user
    ch = b.get('channel') if b.get('channel') in ('cohost', 'linkedin') else 'cohost'
    ids = [store.add_outreach(u, ch, str(it.get('name') or '')[:200], str(it.get('url') or '')[:500], str(it.get('city') or '')[:120],
                              str(it.get('message') or '')[:3000],
                              meta={**{k: it.get(k) for k in ('id', 'listing_title', 'company') if k in it},
                                    'listing_url': str(it.get('listing_url') or '')[:300],
                                    'airbnb_profile': linkedin.airbnb_profile(it.get('airbnb_profile'))})
           for it in outreach_allowed([it for it in (b.get('items') or [])[:25] if isinstance(it, dict)])]
    return {'ok': True, 'ids': ids, 'rows': store.outreach_rows(u), 'stats': store.outreach_stats(u)}


@app.get('/api/outreach/companies')
def api_companies(place: str = '', category: str = '', page: int = 1):
    """UK property companies from the Companies House register (business to business; app/companies.py)."""
    try:
        return companies.search(place, category, page)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post('/api/outreach/companies/queue')
async def api_company_queue(request: Request):
    b, u = await request.json(), request.state.user
    try:
        rid = companies.queue(u, b.get('company_number'), b.get('template'), b.get('sender'))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'ok': True, 'id': rid, 'stats': store.outreach_stats(u)}


def _suppress(item, user):
    """Do not contact, for every user: recorded against the account marking it and limited per day (store.suppress)."""
    if not store.suppress(item, user):
        raise HTTPException(429, 'You have reached the daily limit for marking prospects as do not contact. Do not contact '
                                 'this one meanwhile, and mark it again tomorrow. If they want it done today, they can use '
                                 'our privacy request form.')


@app.post('/api/outreach/companies/suppress')
async def api_company_suppress(request: Request):
    """Do not contact: the company never appears in anyone's results again (a keyed hash of its number is kept).
    Only companies in the register snapshot, recorded against this account and capped per day, so no account can wipe
    the list for everyone; python -m app.admin unsuppress undoes one account's marks."""
    n = companies.number((await request.json()).get('company_number'))
    if not n:
        raise HTTPException(400, 'That is not a company number')
    if not companies.exists(n):
        raise HTTPException(404, 'That company is not in the register snapshot')
    _suppress({'company_number': n}, request.state.user)
    return {'ok': True}


@app.post('/api/outreach/suppress')
async def api_out_suppress(request: Request):
    """Do not contact: the prospect objected. Suppressed for every user (hashes only) and this row deleted."""
    b, u = await request.json(), request.state.user
    r = store.outreach_get(int(b.get('id') or 0), u)
    if not r:
        raise HTTPException(404)
    try:
        meta = json.loads(r.get('meta') or '{}')
    except ValueError:
        meta = {}
    _suppress({**(meta if isinstance(meta, dict) else {}), 'name': r['name'], 'url': r['url']}, u)
    store.outreach_delete(r['id'], u)
    return {'ok': True, 'stats': store.outreach_stats(u)}


@app.post('/api/outreach/status')
async def api_out_status(request: Request):
    b, u = await request.json(), request.state.user
    r = store.outreach_get(int(b.get('id') or 0), u)
    if not r:
        raise HTTPException(404)
    st = b.get('status') or 'queued'
    if st not in ('queued', 'sent', 'replied', 'won', 'skipped'):
        raise HTTPException(400, 'Bad status')
    store.outreach_set(r['id'], u, status=st, **({'sent_at': time.time()} if st == 'sent' and not r.get('sent_at') else {}))
    return {'ok': True, 'stats': store.outreach_stats(u)}


@app.post('/api/outreach/note')
async def api_out_note(request: Request):
    b, u = await request.json(), request.state.user
    r = store.outreach_get(int(b.get('id') or 0), u)
    if not r:
        raise HTTPException(404)
    store.outreach_set(r['id'], u, note=(b.get('note') or '')[:500])
    return {'ok': True}


@app.get('/api/outreach/export.csv')
def api_out_csv(request: Request):
    return PlainTextResponse(linkedin.csv_rows(store.outreach_rows(request.state.user)), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename="reelsieve-outreach.csv"'})


# ---------------- settings ----------------

EVENT_LABELS = {'plan': 'Plan or credits changed', 'password_reset': 'Password reset', 'deactivate': 'Removed (deactivated)',
                'erase': 'Account erased', 'order_settle': 'Order marked paid', 'order_cancel': 'Order cancelled',
                'order_link': 'Pay link set', 'privacy_request_handled': 'Privacy request handled',
                'invoice_export': 'Invoice CSV downloaded',
                'unsuppress': 'Do-not-contact marks undone', 'request_unsuppress': 'Do-not-contact from a privacy request undone',
                'listing_block': 'Listing blocked', 'listing_unblock': 'Listing unblocked', 'listing_confirm': 'Listing removal confirmed',
                'airbnb_resume': 'Airbnb fetching resumed'}


def settings_view():
    """Cloud-managed configuration, read-only: secrets show only whether they are set."""
    out = []
    for k, secret, hint in SETTINGS:
        v, default = (os.getenv(k) or '').strip(), SETTING_DEFAULTS.get(k)
        out.append({'key': k, 'configured': bool(v or default), 'secret': secret, 'hint': hint,
                    'value': '' if secret else (v or (default + ' (default)' if default else '')),
                    'state': ('On' if airbnb.enabled() else 'Off') if k == 'AIRBNB_FETCH_ENABLED' else
                             'Default' if default and not v else None})
    return out


@app.get('/settings', response_class=HTMLResponse)
def settings(request: Request, saved: int = 0, flash: str = ''):
    if not request.state.is_admin:
        return RedirectResponse('/account' + (('?flash=' + quote(flash)) if flash else ('?saved=1' if saved else '')), status_code=303)
    return tpl.TemplateResponse(request, 'settings.html', {
        'settings': settings_view(), 'gdrive': gdrive.status(request.state.user), 'saved': bool(saved), 'flash': flash[:400],
        'events': store.admin_events(50), 'event_labels': EVENT_LABELS,
        'requests': store.open_privacy_requests(), 'request_types': store.PRIVACY_REQUEST_TYPES, 'now': time.time(),
        'form_marks': store.request_suppressions(),
        'backup': invoices.status(),
        'referral_totals': referrals.totals(), 'referral_limit': referrals.MONTHLY_LIMIT,
        'companies': companies.status(),
        'blocked': store.blocked_listings(), 'airbnb_state': airbnb.state(),
        'redirect_uri': _redirect_uri(request), 'webhook_base': (public_base() or str(request.base_url).rstrip('/'))})


@app.get('/api/settings')
def settings_api(request: Request):
    _require_admin(request)
    return {s['key'].lower(): {'configured': s['configured']} for s in settings_view()}


@app.get('/api/invoices/export.csv')
def api_invoices_csv(request: Request):
    """Every paid order, as the daily India backup writes it, for the accountant. Admins only; each download is logged."""
    _require_admin(request)
    body, n = invoices.export()
    store.admin_event('invoice_export', request.state.user, rows=n)
    name = f"reelsieve-invoices-{time.strftime('%Y-%m-%d', time.gmtime())}.csv"
    return Response(body, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store'})


@app.get('/account', response_class=HTMLResponse)
def account_page(request: Request, saved: int = 0, flash: str = ''):
    return tpl.TemplateResponse(request, 'account.html', {
        'account': plans.account_view(request.state.user), 'plans': plans.public_plans(),
        'gdrive': gdrive.status(request.state.user), 'saved': bool(saved), 'flash': flash[:400],
        'records_years': retention.FINANCIAL_RECORDS_YEARS, 'team_changes': store.team_changes(request.state.user),
        'referral': {'link': site_url() + '/r/' + referrals.code_for(request.state.user),
                     'rewarded': referrals.rewarded_count(request.state.user), 'monthly_limit': referrals.MONTHLY_LIMIT}})
