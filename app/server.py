#!/usr/bin/env python3
"""ReelSieve by Braivex — paste an Airbnb URL, get a cinematic reel in your own Google Drive.

Web process only: identity, jobs, billing and Drive credentials live in PostgreSQL; rendering runs
in app.worker. Nothing here writes customer data to local disk.
Run: .venv/bin/uvicorn app.server:app --port 8787   (DATABASE_URL, SESSION_SECRET, TOKEN_ENCRYPTION_KEY)
"""
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import admin, airbnb, auth, billing, cohost, database, fetch, gdrive, hostmsg, jobs, linkedin, plans, retention, store
from app import search as listing_search

HERE = Path(__file__).resolve().parent
REQUIRED = ('DATABASE_URL', 'SESSION_SECRET', 'TOKEN_ENCRYPTION_KEY')
CSRF_COOKIE = 'reelsieve_csrf'
PUBLIC_PREFIXES = ('/static/', '/oauth/google/callback', '/favicon.ico', '/api/billing/webhook/')
PUBLIC_EXACT = ('/', '/login', '/signup', '/setup', '/forgot', '/healthz', '/privacy', '/terms', '/privacy/request',
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


@asynccontextmanager
async def lifespan(_app):
    validate_config()
    database.initialize()
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


def _csrf_basis(request):
    """Signed-in: the session. Anonymous: a random per-browser nonce, never a shared constant."""
    return request.cookies.get(auth.COOKIE) or ('anon:' + getattr(request.state, 'csrf_nonce', ''))


def csrf_for(request):
    return auth.csrf_token(_csrf_basis(request))


tpl.env.globals['csrf_for'] = csrf_for


def _safe_next(target, default='/app'):
    t = target or ''
    if not t.startswith('/') or t.startswith('//') or '\\' in t or any(ord(ch) < 32 or ord(ch) == 127 for ch in t):
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
        user = auth.check(session) if session else None
        request.state.user = user
        request.state.is_admin = _role_admin(user)
        if not user and not (path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT):
            if path.startswith('/api/'):
                return JSONResponse({'detail': 'Sign in required'}, status_code=401)
            target = path + ('?' + request.url.query if request.url.query else '')
            return RedirectResponse('/login?next=' + quote(target), status_code=303)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not path.startswith('/api/billing/webhook/'):
            sent = request.headers.get('x-csrf-token', '')
            if not sent and request.headers.get('content-type', '').startswith(('application/x-www-form-urlencoded', 'multipart/form-data')):
                form = await request.form()
                sent = form.get('csrf') or ''
                request.scope['_form'] = dict(form)
            basis = _csrf_basis(request)
            if (fresh and not session) or not auth.csrf_ok(basis, sent):
                if path.startswith('/api/'):
                    return JSONResponse({'detail': 'Form expired — reload the page and try again'}, status_code=403)
                return HTMLResponse('Invalid or expired form token — reload and try again', status_code=403)
        response = await call_next(request)
        if fresh:
            response.set_cookie(CSRF_COOKIE, fresh, httponly=True, samesite='lax', secure=_secure(request), max_age=30 * 86400)
        return response


app.add_middleware(Gate)

SECURITY_HEADERS = {'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'strict-origin-when-cross-origin',
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


async def _form(request):
    return request.scope.get('_form') or dict(await request.form())


def _set_session(resp, request, user, long=True):
    tok, ttl = auth.issue(user, long)
    resp.set_cookie(auth.COOKIE, tok, max_age=ttl, httponly=True, samesite='lax', secure=_secure(request))
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
# Google: list only the URLs you want in search results. /login and /forgot are bare forms, so they are noindex and absent.
INDEXABLE = {'/': 'landing.html', '/signup': 'signup.html', '/privacy': 'legal.html', '/terms': 'legal.html'}
LLMS_TXT = """# ReelSieve

> ReelSieve, made by Braivex, turns an Airbnb listing link into a cinematic walkthrough video built from the listing's own photos and real guest reviews.

For now it accepts only Airbnb listing links, the kind with /rooms/ in the address.
Each video has an intro, a rating card, the rooms in walking order with captions, a real guest review card and an outro.
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
    return PlainTextResponse(LLMS_TXT.format(plans=lines, base=site_url()))


# ---------------- identity ----------------

@app.api_route('/setup', methods=['GET', 'POST'])
def setup_closed():
    """Operators are provisioned out of band; the public can only ever create member accounts."""
    return RedirectResponse('/signup', status_code=303)


@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request, next: str = '/app', notice: str = ''):
    if request.state.user:
        return RedirectResponse(_safe_next(next), status_code=303)
    msg = {'out': 'You have been signed out.', 'created': 'Account created — sign in.',
           'pw': 'Password changed — sign in with the new one.',
           'deleted': 'Your account has been deleted.'}.get(notice, '')
    return tpl.TemplateResponse(request, 'login.html', {'next': _safe_next(next), 'notice': msg})


@app.post('/login')
async def login_post(request: Request):
    f = await _form(request)
    u, p, nxt = (f.get('user') or '').strip(), f.get('password') or '', _safe_next(f.get('next'))
    ip = _ip(request)
    ctx = lambda err, code: tpl.TemplateResponse(request, 'login.html', {'next': nxt, 'user': u, 'error': err}, status_code=code)  # noqa: E731
    if auth.too_many(ip):
        return ctx('Too many attempts — wait 10 minutes', 429)
    if not auth.verify(u, p):
        auth.record_fail(ip)
        time.sleep(0.6)
        return ctx('Wrong email or password', 401)
    auth.clear_fails(ip)
    return _set_session(RedirectResponse(nxt, status_code=303), request, u, f.get('remember') == '1')


@app.post('/logout')
def logout():
    r = RedirectResponse('/login?notice=out', status_code=303)
    r.delete_cookie(auth.COOKIE)
    return r


@app.get('/forgot', response_class=HTMLResponse)
def forgot(request: Request):
    return tpl.TemplateResponse(request, 'forgot.html', {})


@app.get('/signup', response_class=HTMLResponse)
def signup_page(request: Request, plan: str = '', url: str = ''):
    if request.state.user:
        return RedirectResponse('/app', status_code=303)
    return tpl.TemplateResponse(request, 'signup.html', {'plan': plan, 'url': url[:500], 'plans': plans.public_plans()})


@app.post('/signup')
async def signup_post(request: Request):
    f = await _form(request)
    u, p1, p2 = (f.get('user') or '').strip(), f.get('password') or '', f.get('password2') or ''
    plan, url, ip = (f.get('plan') or 'free').strip(), (f.get('url') or '').strip()[:500], _ip(request)
    ctx = lambda err: tpl.TemplateResponse(request, 'signup.html', {'user': u, 'plan': plan, 'url': url, 'error': err, 'plans': plans.public_plans()}, status_code=400)  # noqa: E731
    if p2 and p1 != p2:
        return ctx('Passwords do not match')
    guard = plans.signup_guard(u, ip)
    if guard:
        return ctx(guard)
    try:
        auth.create_user(u, p1, 'member')
    except ValueError as e:
        return ctx(str(e))
    store.ensure_account(u, 'free')
    nxt = '/app' + (('?url=' + quote(url)) if url else '')
    if plan in ('starter', 'commercial'):
        nxt = '/upgrade?plan=' + plan
    return _set_session(RedirectResponse(nxt, status_code=303), request, u, True)


@app.post('/api/account/password')
async def change_password(request: Request):
    b = await request.json()
    if not auth.verify(request.state.user, b.get('current') or ''):
        raise HTTPException(400, 'Current password is wrong')
    try:
        auth.set_password(request.state.user, b.get('new') or '')
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({'ok': True, 'relogin': '/login?notice=pw'})
    resp.delete_cookie(auth.COOKIE)
    return resp


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


@app.post('/api/account/delete')
async def account_delete(request: Request):
    """Delete my account (Art 17 / DPDP s12) after a password re-check and a typed DELETE."""
    b = await request.json()
    if (b.get('confirm') or '').strip() != 'DELETE':
        raise HTTPException(400, 'Type DELETE to confirm')
    if not auth.verify(request.state.user, b.get('password') or ''):
        raise HTTPException(400, 'Password is wrong')
    try:
        warning = admin.erase(request.state.user, request.state.user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({'ok': True, 'warning': warning, 'redirect': '/login?notice=deleted'})
    resp.delete_cookie(auth.COOKIE)
    return resp


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
    try:
        auth.create_user(b.get('user', ''), b.get('password', ''), 'admin' if b.get('role') == 'admin' else 'member')
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'users': auth.users()}


@app.post('/api/users/delete')
async def api_users_del(request: Request):
    """Deactivate: stop the owner's jobs and revoke their Drive grant; business history is retained."""
    _require_admin(request)
    target = ((await request.json()).get('user') or '').strip().lower()
    try:
        warning = admin.deactivate(target, request.state.user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'users': auth.users(), 'warning': warning}


@app.post('/api/users/erase')
async def api_users_erase(request: Request):
    """Erase, unlike Remove: personal data deleted or anonymised; paid orders kept for the tax record period."""
    _require_admin(request)
    target = ((await request.json()).get('user') or '').strip().lower()
    try:
        warning = admin.erase(target, request.state.user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'users': auth.users(), 'warning': warning}


@app.post('/api/users/password')
async def api_users_pw(request: Request):
    _require_admin(request)
    b = await request.json()
    try:
        auth.set_password(b.get('user', ''), b.get('password', ''))
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
        'products': plans.PRODUCTS, 'sample_video': os.getenv('SAMPLE_VIDEO_URL') or None,
        'sample_poster': os.getenv('SAMPLE_POSTER_URL') or None})


@app.get('/privacy', response_class=HTMLResponse)
def privacy(request: Request):
    return tpl.TemplateResponse(request, 'legal.html', {'kind': 'privacy'})


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
    ip = _ip(request)
    if auth.too_many(ip, 'privacy'):
        return _request_page(request, 429, f=f, error='Too many requests from this network. Try again in 10 minutes, or email hello@braivex.com.')
    profile = linkedin.airbnb_profile(f.get('airbnb_profile', ''))
    removal = f.get('type') == 'listing_removal'
    listing = jobs.listing_id(f.get('listing_url', ''))
    error = ('Choose what the request is about' if f.get('type') not in store.PRIVACY_REQUEST_TYPES else
             'Enter a valid email address so we can reply' if not auth.EMAIL.match(f.get('email', '')) else
             'Paste the link to your Airbnb listing (airbnb.co.uk/rooms/<number>)' if removal and not listing else
             'Tell us what you would like us to do' if not f.get('details') and not removal else
             'Paste the link to your Airbnb profile (airbnb.co.uk/users/show/<number>), or leave it empty'
             if f.get('airbnb_profile') and not profile else
             'Paste the link to your Airbnb listing (airbnb.co.uk/rooms/<number>), or leave it empty'
             if f.get('listing_url') and not listing else None)
    if error:
        return _request_page(request, 400, f=f, error=error)
    auth.record_fail(ip, 'privacy')  # counts submissions, not failures
    details = f.get('details') or 'Remove my listing from ReelSieve.'
    ref, received = store.add_privacy_request(f['type'], auth.norm(f['email']), f.get('name', '')[:200] or None, details[:4000],
                                              profile.rsplit('/', 1)[-1] if profile else None, listing)
    if f['type'] == 'objection' and profile:
        store.suppress({'airbnb_profile': profile})  # stop outreach to them at once, for every user
    if removal:
        store.block_listing(listing, 'Removal request ' + ref)  # no more reels of it, for every user, at once
    return _request_page(request, ack={'ref': ref, 'received': received, 'due': store.one_month_after(received),
                                       'listing': listing if removal else None})


@app.post('/api/privacy-requests/handled')
async def privacy_request_handled(request: Request):
    _require_admin(request)
    ref = ((await request.json()).get('ref') or '').strip()
    if not store.handle_privacy_request(ref):
        raise HTTPException(404, 'No open request with that reference')
    store.admin_event('privacy_request_handled', request.state.user, None, ref=ref)
    return {'ok': True}


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
    listing = {'url': j['url'], **(m.get('listing') or {})}
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
        'youtube_title': phrase.replace(' ReelSieve', ' — by ReelSieve'),
        'contact_url': hostmsg.contact_url(lid) if lid else None}


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
def index(request: Request, url: str = ''):
    u = request.state.user
    return tpl.TemplateResponse(request, 'index.html', {
        'jobs': _views(u, jobs.list_for(u, 12)), 'hf_configured': bool(os.getenv('HF_KEY')), 'gdrive': gdrive.status(u),
        'default_message': default_message(), 'prefill_url': url[:500], 'account': plans.account_view(u)})


@app.post('/api/jobs')
async def create_job(request: Request):
    b = await request.json()
    try:
        j = jobs.admit(request.state.user, b.get('url'), b, request.headers.get('idempotency-key') or b.get('idempotency_key'),
                       _ip(request))
    except jobs.AdmissionError as e:
        raise HTTPException(e.status, str(e))
    return {'id': j['id'], 'account': plans.account_view(request.state.user)}


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
    headers = {k: r.headers[k] for k in ('content-type', 'content-length', 'content-range', 'accept-ranges') if k in r.headers}
    headers['cache-control'] = 'private, no-store'
    if download:
        name = re.sub(r'[^A-Za-z0-9._-]+', '-', ((j['meta'] or {}).get('listing') or {}).get('title') or 'reel')[:60].strip('-')
        headers['content-disposition'] = f'attachment; filename="{name or "reel"}-{variant}.mp4"'
    return StreamingResponse(body(), status_code=r.status_code, headers=headers)


def library(user):
    groups, order = {}, []
    for v in _views(user, jobs.list_for(user, 200)):
        lid = listing_id_of(v['listing']['url']) or v['listing']['url']
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
                                timeout=20, max_bytes=IMG_MAX, hosts=IMG_HOSTS)
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


@app.get('/api/search')
def api_search(request: Request, location: str, checkin: str = '', checkout: str = '', adults: int = 2, offset: int = 0, pages: int = 3):
    """In-app listing picker: public Airbnb search results (no login)."""
    _airbnb_on()
    if not location.strip():
        raise HTTPException(400, 'Enter a location')
    with at_once(request.state.user, 'lookup'):
        try:
            return listing_search.search(location[:120], checkin or None, checkout or None, adults, offset, min(max(pages, 1), 5))
        except airbnb.Unavailable as e:
            raise HTTPException(503, str(e))
        except Exception:
            raise HTTPException(502, 'Search failed — try again')


@app.get('/api/search/more')
def api_search_more(request: Request, location: str, page: int, checkin: str = '', checkout: str = '', adults: int = 2):
    _airbnb_on()
    with at_once(request.state.user, 'lookup'):
        try:
            return listing_search.search_page(location[:120], checkin or None, checkout or None, adults, page)
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
    warning = None
    try:
        gdrive.disconnect(request.state.user)
    except RuntimeError as e:
        warning = str(e)
    return {**gdrive.status(request.state.user), 'warning': warning}


# ---------------- outreach (drafted here, sent by the customer) ----------------

@app.get('/outreach', response_class=HTMLResponse)
def outreach_page(request: Request):
    u = request.state.user
    rows = [{**r, 'link_label': linkedin.link_label(r['url']), 'airbnb_profile': linkedin.airbnb_profile_of(r)}
            for r in store.outreach_rows(u)]
    return tpl.TemplateResponse(request, 'outreach.html', {
        'csrf': csrf_for(request), 'stats': store.outreach_stats(u), 'cities': store.cities(u), 'rows': rows,
        'default_message': os.getenv('COHOST_MESSAGE') or COHOST_MESSAGE, 'linkedin_default': linkedin.CONNECT_DEFAULT,
        'daily_cap': DAILY_CAP, 'cap': DAILY_CAP, 'sent_today': store.sent_today(u)})


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
    store.suppress({**(meta if isinstance(meta, dict) else {}), 'name': r['name'], 'url': r['url']})
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
                'listing_block': 'Listing blocked', 'listing_unblock': 'Listing unblocked'}


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
        'blocked': store.blocked_listings(), 'airbnb_block': airbnb.last_block(),
        'redirect_uri': _redirect_uri(request), 'webhook_base': (public_base() or str(request.base_url).rstrip('/'))})


@app.get('/api/settings')
def settings_api(request: Request):
    _require_admin(request)
    return {s['key'].lower(): {'configured': s['configured']} for s in settings_view()}


@app.get('/account', response_class=HTMLResponse)
def account_page(request: Request, saved: int = 0, flash: str = ''):
    return tpl.TemplateResponse(request, 'account.html', {
        'account': plans.account_view(request.state.user), 'plans': plans.public_plans(),
        'gdrive': gdrive.status(request.state.user), 'saved': bool(saved), 'flash': flash[:400],
        'records_years': retention.FINANCIAL_RECORDS_YEARS, 'team_changes': store.team_changes(request.state.user)})
