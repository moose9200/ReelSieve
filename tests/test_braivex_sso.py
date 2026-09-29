"""Continue with Braivex (29 Sep 2026): /auth/braivex/start, the callback and the sunset of customer passwords.

No network: accounts.braivex.com is an HTTPX MockTransport serving a JWKS built from an RSA key generated here,
and every assertion is signed with that key. Real isolated PostgreSQL. Contract and claim checks:
/Users/hemant/braivex-accounts/docs/PRODUCT-INTEGRATION.md and packages/verify-ts/index.ts.
"""
import base64
import itertools
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import auth, braivex_sso, plans, server, store

ALICE, BOB = 'alice@example.test', 'bob@example.test'
SAM, SUB = 'sam@example.test', 'gid://shopify/Customer/7712345678901'
SITE = 'https://www.reelsieve.braivex.com'
CALLBACK = SITE + '/auth/braivex/callback'
BROKER = 'https://accounts.test'

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)   # once per run: 2048-bit keygen is not free
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_serial = itertools.count()


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def jwk_of(key, kid):
    numbers = key.public_key().public_numbers()
    as_bytes = lambda v: v.to_bytes((v.bit_length() + 7) // 8, 'big')  # noqa: E731
    return {'kty': 'RSA', 'use': 'sig', 'alg': 'RS256', 'kid': kid,
            'n': b64(as_bytes(numbers.n)), 'e': b64(as_bytes(numbers.e))}


class Broker:
    """Just enough of accounts.braivex.com: its published keys, and a counter so caching can be proved."""

    def __init__(self):
        self.kid, self.key, self.fetches, self.status = 'test-key-1', KEY, 0, 200

    def handle(self, req):
        assert str(req.url) == BROKER + '/.well-known/jwks.json', str(req.url)
        self.fetches += 1
        return httpx.Response(self.status, json={'keys': [jwk_of(self.key, self.kid)]})

    def assertion(self, kid=None, sign_with=None, **claims):
        now = int(time.time())
        body = {'iss': BROKER, 'aud': 'reelsieve', 'sub': SUB, 'email': SAM, 'email_verified': True,
                'given_name': 'Sam', 'family_name': 'Rowe', 'iat': now, 'nbf': now, 'exp': now + 120,
                'jti': f'synthetic-jti-{next(_serial)}', **claims}
        head = {'alg': 'RS256', 'kid': kid or self.kid, 'typ': 'JWT'}
        signing = f"{b64(json.dumps(head).encode())}.{b64(json.dumps(body).encode())}"
        sig = (sign_with or self.key).sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f'{signing}.{b64(sig)}'


@pytest.fixture(autouse=True)
def fresh_key_cache():
    braivex_sso._JWKS.clear()
    yield
    braivex_sso._JWKS.clear()


@pytest.fixture
def broker(db, monkeypatch):
    fake = Broker()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setenv('BRAIVEX_SSO', 'on')
    monkeypatch.setenv('BRAIVEX_ACCOUNTS_URL', BROKER)
    monkeypatch.setenv('PUBLIC_BASE_URL', SITE)
    monkeypatch.delenv('BRAIVEX_PASSWORD_SUNSET', raising=False)
    return fake


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


@pytest.fixture
def web(owners, broker):
    clients = {name: client_for(tok) for name, tok in owners.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def csrf_of(page):
    """The token every page carries in the meta tag its scripts read, form or no form."""
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', page).group(1)


def cookie_header(response, name):
    return next((v for v in response.headers.get_list('set-cookie') if v.startswith(name + '=')), '')


def start(client, path='/auth/braivex/start?next=/reels', **kw):
    """Begin a sign-in and return (response, the state the broker was given)."""
    r = client.get(path, follow_redirects=False, **kw)
    state = parse_qs(urlsplit(r.headers.get('location', '')).query).get('state', [''])[0]
    return r, state


def finish(client, assertion):
    return client.post('/auth/braivex/callback', data={'assertion': assertion}, follow_redirects=False)


def signed_in_as(client):
    r = client.get('/api/account')
    return r.json()['user'] if r.status_code == 200 else None


def braivex_id(db, email):
    with db.connect() as c:
        row = c.execute('SELECT braivex_customer_id FROM users WHERE email=%s', (email,)).fetchone()
    return row['braivex_customer_id'] if row else None


def make_account(client, business=''):
    """The "name your business" step a brand-new customer lands on."""
    page = client.get('/auth/braivex/workspace')
    return client.post('/auth/braivex/workspace', data={'csrf': csrf_of(page.text), 'business': business},
                       follow_redirects=False)


# ---------------- 1. with the switch off the product is exactly what it was ----------------

def test_with_the_switch_off_nothing_about_sign_in_changes(owners, db, monkeypatch):
    monkeypatch.delenv('BRAIVEX_SSO', raising=False)
    anon = client_for()
    page = anon.get('/login').text
    assert 'Continue with Braivex' not in page and '/auth/braivex/start' not in page
    assert 'Continue with Braivex' not in anon.get('/signup').text and 'name="password"' in anon.get('/signup').text
    assert anon.get('/auth/braivex/start?next=/app').status_code == 404
    assert anon.get('/auth/braivex/workspace').status_code == 404
    assert anon.post('/auth/braivex/callback', data={'assertion': 'x'}).status_code == 404
    assert anon.get('/forgot').status_code == 200
    r = anon.post('/login', data={'csrf': csrf_of(page), 'user': ALICE, 'password': 'synthetic-password'},
                  follow_redirects=False)
    assert r.status_code == 303 and auth.check(r.cookies[auth.COOKIE]) == ALICE


def test_off_is_the_default_and_only_the_word_on_switches_it(monkeypatch):
    monkeypatch.delenv('BRAIVEX_SSO', raising=False)
    assert not braivex_sso.enabled() and braivex_sso.passwords_allowed()
    for value in ('off', 'true', '1', 'ON ', 'on'):
        monkeypatch.setenv('BRAIVEX_SSO', value)
        assert braivex_sso.enabled() == (value.strip().lower() == 'on'), value


# ---------------- 2. starting the sign-in ----------------

def test_start_sets_this_browsers_state_cookie_and_redirects_with_the_registered_callback(web):
    anon = web['anon']
    r, state = start(anon)
    assert r.status_code == 302
    url = urlsplit(r.headers['location'])
    assert f'{url.scheme}://{url.netloc}{url.path}' == BROKER + '/start'
    q = parse_qs(url.query)
    assert q['client'] == ['reelsieve'] and q['return_to'] == [CALLBACK] and 'login_hint' not in q
    assert len(state) >= 16 and re.fullmatch(r'[A-Za-z0-9._~-]+', state)
    cookie = cookie_header(r, 'braivex_sso_state')
    assert 'HttpOnly' in cookie and 'Max-Age=600' in cookie and 'Path=/auth/braivex' in cookie
    assert 'SameSite=lax' in cookie and 'Secure' not in cookie          # http here; https is its own test
    assert anon.cookies['braivex_sso_state'] and state != start(anon)[1]  # a new 32-byte state every time


def test_the_state_cookie_is_secure_over_https_and_the_callback_never_comes_from_the_host_header(web):
    r, _ = start(web['anon'], headers={'x-forwarded-proto': 'https', 'host': 'evil.example.test',
                                       'x-forwarded-host': 'evil.example.test'})
    assert 'Secure' in cookie_header(r, 'braivex_sso_state')
    assert parse_qs(urlsplit(r.headers['location']).query)['return_to'] == [CALLBACK]


def test_the_sign_in_form_passes_the_address_it_already_had_as_a_login_hint(web):
    r, _ = start(web['anon'], '/auth/braivex/start?next=/app&login_hint=Sam%40Example.Test')
    assert parse_qs(urlsplit(r.headers['location']).query)['login_hint'] == [SAM]
    r, _ = start(web['anon'], '/auth/braivex/start?next=/app&login_hint=not-an-address')
    assert 'login_hint' not in parse_qs(urlsplit(r.headers['location']).query)


@pytest.mark.parametrize('target,kept', [('/reels', '/reels'), ('/app?url=x', '/app?url=x'),
                                         ('https://evil.example.test/', '/app'), ('//evil.example.test/', '/app'),
                                         ('/\\evil.example.test', '/app'), ('javascript:alert(1)', '/app')])
def test_next_is_refused_unless_it_is_a_path_on_this_site(web, broker, target, kept):
    client = client_for()
    _, state = start(client, '/auth/braivex/start?next=' + target)
    r = finish(client, broker.assertion(state=state, email=ALICE))
    assert r.status_code == 303 and r.headers['location'] == kept
    client = client_for()                              # and the same for a customer who has to name their business
    _, state = start(client, '/auth/braivex/start?next=' + target)
    new = broker.assertion(state=state, sub='gid://shopify/Customer/8899')
    assert finish(client, new).headers['location'] == '/auth/braivex/workspace'
    assert make_account(client).headers['location'] == kept


# ---------------- 3. what the callback refuses ----------------

def test_an_assertion_without_this_browsers_cookie_signs_nobody_in(web, broker):
    _, state = start(client_for())                       # someone else's sign-in, minted for their state
    attacker = client_for()
    r = finish(attacker, broker.assertion(state=state))
    assert r.status_code == 401 and 'did not start in this browser' in r.text
    assert signed_in_as(attacker) is None


def test_a_state_that_does_not_match_this_browser_is_refused(web, broker):
    client, other = client_for(), client_for()
    _, mine = start(client)
    _, theirs = start(other)
    assert mine != theirs
    assert finish(client, broker.assertion(state=theirs)).status_code == 401
    _, mine = start(client)
    assert finish(client, broker.assertion(state='')).status_code == 401
    _, mine = start(client)
    assert finish(client, broker.assertion()).status_code == 401           # no state claim at all
    assert signed_in_as(client) is None


def test_the_state_cookie_is_spent_whether_the_sign_in_worked_or_not(web, broker):
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, aud='loculens')).status_code == 401
    assert 'braivex_sso_state' not in client.cookies
    assert finish(client, broker.assertion(state=state)).status_code == 401  # the cookie is gone, so this is refused too
    assert signed_in_as(client) is None


BAD = {'minted for another product': lambda n: {'aud': 'loculens'},
       'no audience': lambda n: {'aud': None},
       'issued by someone else': lambda n: {'iss': 'https://accounts.evil.test'},
       'expired': lambda n: {'exp': n - 31},
       'not valid yet': lambda n: {'nbf': n + 31},
       'no exp': lambda n: {'exp': None},
       'email not verified': lambda n: {'email_verified': False},
       'email_verified is a string': lambda n: {'email_verified': 'true'},
       'empty email': lambda n: {'email': ''},
       'no email': lambda n: {'email': None},
       'sub is not a gid': lambda n: {'sub': '7712345678901'},
       'sub is not a customer': lambda n: {'sub': 'gid://shopify/Order/1'},
       'no sub': lambda n: {'sub': None},
       'no jti': lambda n: {'jti': None}}


@pytest.mark.parametrize('flaw', sorted(BAD))
def test_every_claim_the_contract_requires_is_checked(web, broker, flaw):
    client = client_for()
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, **BAD[flaw](int(time.time()))))
    assert r.status_code == 401 and 'Braivex could not sign you in' in r.text
    assert signed_in_as(client) is None


def test_thirty_seconds_of_clock_drift_is_allowed_and_no_more(web, broker):
    for offset, ok in ((-29, True), (-31, False)):
        client = client_for()
        _, state = start(client)
        r = finish(client, broker.assertion(state=state, exp=int(time.time()) + offset))
        assert (r.status_code == 303) is ok, offset


def test_a_tampered_payload_and_a_key_the_broker_never_published_are_refused(web, broker):
    client = client_for()
    _, state = start(client)
    head, body, sig = broker.assertion(state=state).split('.')
    swapped = json.loads(base64.urlsafe_b64decode(body + '=='))
    swapped['email'] = 'someone-else@example.test'
    assert finish(client, f'{head}.{b64(json.dumps(swapped).encode())}.{sig}').status_code == 401
    for make in (lambda s: broker.assertion(state=s, sign_with=OTHER_KEY),
                 lambda s: broker.assertion(state=s, kid='never-published'),
                 lambda s: 'not-a-jws', lambda s: '', lambda s: 'a.b.c'):
        _, state = start(client)
        assert finish(client, make(state)).status_code == 401
    assert signed_in_as(client) is None


def test_the_same_assertion_is_spent_once_and_a_replay_is_refused(web, broker, db):
    first = client_for()
    _, state = start(first)
    good = broker.assertion(state=state, email=ALICE)
    assert finish(first, good).status_code == 303 and signed_in_as(first) == ALICE
    # A second browser replaying the assertion it stole, holding a state cookie that matches it: the spent jti is
    # the only thing left to refuse it.
    second = client_for()
    second.cookies.set('braivex_sso_state', server._seal(600, state=state, next='/app', ref=''), path='/auth/braivex')
    replay = finish(second, good)
    assert replay.status_code == 401 and 'already been used' in replay.text
    assert signed_in_as(second) is None
    with db.connect() as c:
        rows = c.execute('SELECT jti,seen,expires_at FROM braivex_sso_jti').fetchall()
    assert len(rows) == 1 and 599 <= rows[0]['expires_at'] - rows[0]['seen'] <= 601


def test_retention_deletes_spent_assertions_once_they_can_no_longer_be_replayed(web, broker, db):
    from app import retention
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state)).status_code == 303
    assert retention.run()['braivex_sso_jti'] == 0          # still inside the 10 minutes
    with db.connect() as c:
        c.execute('UPDATE braivex_sso_jti SET expires_at=%s', (time.time() - 1,))
    assert retention.run()['braivex_sso_jti'] == 1
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM braivex_sso_jti').fetchone()['n'] == 0


# ---------------- 4. the published keys ----------------

def test_the_key_set_is_fetched_once_and_again_only_when_a_kid_is_unknown(web, broker):
    for _ in range(2):
        client = client_for()
        _, state = start(client)
        assert finish(client, broker.assertion(state=state, email=ALICE)).status_code == 303
    assert broker.fetches == 1                               # cached for ten minutes
    broker.kid, broker.key = 'test-key-2', OTHER_KEY         # the broker rotates its signing key
    braivex_sso._JWKS[BROKER]['at'] -= braivex_sso.JWKS_REFETCH_COOLDOWN + 1
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).status_code == 303
    assert broker.fetches == 2 and signed_in_as(client) == ALICE


def test_a_forged_kid_cannot_make_this_server_fetch_the_key_set_again_and_again(web, broker):
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state)).status_code == 303
    for i in range(5):
        other = client_for()
        _, s = start(other)
        assert finish(other, broker.assertion(state=s, kid=f'forged-{i}')).status_code == 401
    assert broker.fetches == 1


def test_a_broker_that_cannot_be_reached_signs_nobody_in(web, broker):
    broker.status = 503
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state)).status_code == 401
    assert signed_in_as(client) is None and broker.fetches == 1


# ---------------- 5. linking ----------------

def test_a_returning_customer_is_found_by_their_shopify_customer_id_not_their_email(web, broker, db):
    first = client_for()
    _, state = start(first)
    assert finish(first, broker.assertion(state=state)).status_code == 303
    assert make_account(first).status_code == 303 and braivex_id(db, SAM) == SUB
    with db.connect() as c:                                   # they change their email at Shopify
        c.execute('UPDATE users SET email=%s WHERE braivex_customer_id=%s', ('sam.rowe@example.test', SUB))
    again = client_for()
    _, state = start(again)
    assert finish(again, broker.assertion(state=state, email='sam.rowe@example.test')).status_code == 303
    assert signed_in_as(again) == 'sam.rowe@example.test'
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM users').fetchone()['n'] == 3   # alice, bob, sam: nobody new


def test_an_existing_password_account_is_linked_by_its_verified_email_and_keeps_everything(web, broker, db):
    store.ensure_account(ALICE, 'starter')
    store.add_credits(ALICE, 4)
    client = client_for()
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, email='Alice@Example.TEST'))
    assert r.status_code == 303 and signed_in_as(client) == ALICE
    assert braivex_id(db, ALICE) == SUB
    assert store.get_account(ALICE)['plan'] == 'starter' and store.get_account(ALICE)['credits'] == 4
    assert auth.verify(ALICE, 'synthetic-password')           # their password still works until the sunset


def test_an_account_that_is_already_somebody_elses_shopify_customer_is_never_taken_over(web, broker, db):
    with db.connect() as c:
        c.execute('UPDATE users SET braivex_customer_id=%s WHERE email=%s', ('gid://shopify/Customer/111', BOB))
    client = client_for()
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, email=BOB, sub='gid://shopify/Customer/222'))
    assert r.status_code == 401 and 'already linked' in r.text and signed_in_as(client) is None
    assert braivex_id(db, BOB) == 'gid://shopify/Customer/111'


def test_braivex_never_signs_an_operator_in(web, broker, db):
    with db.connect() as c:
        c.execute("UPDATE users SET role='admin' WHERE email=%s", (ALICE,))
    client = client_for()
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, email=ALICE))
    assert r.status_code == 401 and 'signs in with its password' in r.text
    assert signed_in_as(client) is None and braivex_id(db, ALICE) is None


# ---------------- 6. a customer Braivex has verified who has never been here ----------------

def new_customer(broker, **claims):
    """Sign in as somebody with no account, stopping on the "name your business" step."""
    client = client_for()
    _, state = start(client, '/auth/braivex/start?next=/reels')
    return client, finish(client, broker.assertion(state=state, **claims))


def test_a_new_customer_is_asked_to_name_their_business_before_an_account_exists(web, broker, db):
    client, r = new_customer(broker)
    assert r.status_code == 303 and r.headers['location'] == '/auth/braivex/workspace'
    page = client.get('/auth/braivex/workspace')
    assert page.status_code == 200 and SAM in page.text and 'Name your business' in page.text
    assert 'type="password"' not in page.text
    assert auth.identity(SAM) is None and signed_in_as(client) is None      # nothing exists until they confirm
    done = make_account(client, 'Rowe Lets')
    assert done.status_code == 303 and done.headers['location'] == '/reels'
    assert signed_in_as(client) == SAM and braivex_id(db, SAM) == SUB
    assert store.get_account(SAM)['b2b_sender']['business'] == 'Rowe Lets'
    assert 'braivex_sso_new' not in client.cookies


def test_the_new_account_matches_a_password_sign_up_exactly_but_has_no_usable_password(web, broker, db):
    client, _ = new_customer(broker)
    assert make_account(client).status_code == 303
    store.ensure_account(BOB, 'free')                         # the same product, created the old way
    sso, password = plans.account_view(SAM), plans.account_view(BOB)
    assert {k: v for k, v in sso.items() if k != 'user'} == {k: v for k, v in password.items() if k != 'user'}
    assert auth.role(SAM) == 'member' and client.get('/api/users').status_code == 403
    with db.connect() as c:
        row = c.execute('SELECT hash,salt,iterations,role FROM users WHERE email=%s', (SAM,)).fetchone()
    assert row['hash'].startswith('braivex-sso:') and len(row['salt']) == 32 and row['role'] == 'member'
    for guess in ('', 'synthetic-password', row['hash'], 'braivex-sso:'):
        assert not auth.verify(SAM, guess), guess


def test_the_sign_up_page_offers_braivex_and_nothing_else(web):
    page = client_for().get('/signup?plan=starter').text
    assert 'Continue with Braivex' in page and 'name="password"' not in page
    assert 'Braivex sends a 6-digit code to your email' in page
    assert 'href="/auth/braivex/start?next=/upgrade%3Fplan%3Dstarter"' in page   # the plan survives the sign-in


def test_the_session_a_braivex_sign_in_creates_is_the_one_a_password_creates(web, broker, db):
    client, _ = new_customer(broker)
    sso = make_account(client)
    anon = client_for()
    pw = anon.post('/login', data={'csrf': csrf_of(anon.get('/login').text), 'user': ALICE,
                                   'password': 'synthetic-password', 'remember': '1'}, follow_redirects=False)
    flags = lambda r: sorted(p.strip().split('=')[0].lower()                                        # noqa: E731
                             for p in cookie_header(r, auth.COOKIE).split(';')[1:])
    assert flags(sso) == flags(pw) == ['httponly', 'max-age', 'path', 'samesite']
    assert auth.check(sso.cookies[auth.COOKIE]) == SAM and auth.check(pw.cookies[auth.COOKIE]) == ALICE
    with db.connect() as c:                                   # and the same revocation: session_version ends it
        c.execute('UPDATE users SET session_version=session_version+1 WHERE email=%s', (SAM,))
    assert signed_in_as(client) is None


def test_the_step_cannot_be_reached_or_replayed_without_the_verified_claims(web, broker):
    assert client_for().get('/auth/braivex/workspace').status_code == 401
    client, _ = new_customer(broker)
    page = client.get('/auth/braivex/workspace').text
    forged = client_for()
    forged.cookies.set('braivex_sso_new', client.cookies['braivex_sso_new'] + 'x', path='/auth/braivex')
    assert forged.get('/auth/braivex/workspace').status_code == 401
    assert client.post('/auth/braivex/workspace', data={'business': 'x'}).status_code == 403    # CSRF still applies
    assert auth.identity(SAM) is None
    assert client.post('/auth/braivex/workspace', data={'csrf': csrf_of(page)}, follow_redirects=False).status_code == 303
    assert client.get('/auth/braivex/workspace').status_code == 401   # the cookie was spent with the account


def test_an_invite_link_still_attributes_the_invite_when_braivex_creates_the_account(web, broker, db):
    from app import referrals
    code = referrals.code_for(ALICE)
    client = client_for()
    assert client.get('/r/' + code, follow_redirects=False).headers['location'] == '/signup?ref=' + code
    page = client.get('/signup?ref=' + code).text
    href = re.search(r'href="(/auth/braivex/start\?[^"]+)"', page).group(1).replace('&amp;', '&')
    _, state = start(client, href)
    assert finish(client, broker.assertion(state=state)).status_code == 303
    assert make_account(client).status_code == 303
    with db.connect() as c:
        row = c.execute('SELECT referrer_id,referee_id FROM referrals').fetchone()
    assert row['referrer_id'] == db.user_id(ALICE) and row['referee_id'] == db.user_id(SAM)


# ---------------- 7. the sunset of customer passwords ----------------

def yesterday():
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400))


def tomorrow():
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() + 86400))


def test_before_the_sunset_the_password_form_is_still_there_second(web, monkeypatch):
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', '2026-11-05')
    anon = client_for()
    page = anon.get('/login').text
    assert page.index('Continue with Braivex') < page.index('Sign in with your password')
    assert 'Password sign-in ends on 05 Nov 2026. Use Continue with Braivex.' in page
    assert 'name="password"' in page and anon.get('/forgot').status_code == 200
    r = anon.post('/login', data={'csrf': csrf_of(page), 'user': ALICE, 'password': 'synthetic-password'},
                  follow_redirects=False)
    assert r.status_code == 303 and auth.check(r.cookies[auth.COOKIE]) == ALICE


def test_on_the_sunset_day_a_customer_password_no_longer_signs_anyone_in(web, monkeypatch):
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', time.strftime('%Y-%m-%d', time.gmtime()))
    anon = client_for()
    page = anon.get('/login').text
    assert 'Continue with Braivex' in page and 'name="password"' not in page and 'Forgot password?' not in page
    r = anon.post('/login', data={'csrf': csrf_of(page), 'user': ALICE, 'password': 'synthetic-password'},
                  follow_redirects=False)
    assert r.status_code == 403 and 'Password sign-in has ended' in r.text
    assert auth.COOKIE not in r.cookies and signed_in_as(anon) is None


def test_after_the_sunset_password_sign_up_and_password_recovery_are_closed(web, monkeypatch):
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', yesterday())
    anon = client_for()
    signup = anon.get('/signup')
    assert 'name="password"' not in signup.text and 'Continue with Braivex' in signup.text
    csrf = csrf_of(signup.text)
    r = anon.post('/signup', data={'csrf': csrf, 'user': 'new@example.test', 'password': 'a-long-password'},
                  follow_redirects=False)
    assert r.status_code == 403 and auth.identity('new@example.test') is None
    assert anon.get('/forgot', follow_redirects=False).headers['location'] == '/login'
    assert anon.post('/forgot', data={'csrf': csrf, 'user': ALICE}, follow_redirects=False).headers['location'] == '/login'


def test_an_operator_keeps_their_password_after_the_sunset(web, monkeypatch, db):
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', yesterday())
    with db.connect() as c:
        c.execute("UPDATE users SET role='admin' WHERE email=%s", (BOB,))
    anon = client_for()
    # The form is not offered any more, but an operator who posts to it still gets in: break-glass.
    r = anon.post('/login', data={'csrf': csrf_of(anon.get('/login').text), 'user': BOB,
                                  'password': 'synthetic-password'}, follow_redirects=False)
    assert r.status_code == 303 and auth.check(r.cookies[auth.COOKIE]) == BOB
    operator = client_for(r.cookies[auth.COOKIE])
    assert operator.get('/api/users').status_code == 200


def test_the_sunset_only_applies_once_braivex_sign_in_is_on(web, monkeypatch):
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', yesterday())
    monkeypatch.delenv('BRAIVEX_SSO')
    assert braivex_sso.passwords_allowed()
    anon = client_for()
    r = anon.post('/login', data={'csrf': csrf_of(anon.get('/login').text), 'user': ALICE,
                                  'password': 'synthetic-password'}, follow_redirects=False)
    assert r.status_code == 303
    monkeypatch.setenv('BRAIVEX_SSO', 'on')
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', tomorrow())
    assert braivex_sso.passwords_allowed()
    monkeypatch.setenv('BRAIVEX_PASSWORD_SUNSET', 'not-a-date')
    assert braivex_sso.passwords_allowed() and braivex_sso.sunset() is None


# ---------------- 8. the callback is the only route the CSRF check skips ----------------

def test_the_braivex_callback_is_the_only_new_route_exempt_from_the_csrf_check(web):
    exempt = {}
    for route in server.app.routes:
        if 'POST' not in (getattr(route, 'methods', None) or ()):
            continue
        r = web['alice'].post(re.sub(r'\{[^}]+\}', 'x', route.path), data={'probe': '1'}, follow_redirects=False)
        if r.status_code != 403:
            exempt[route.path] = r.status_code
    assert sorted(exempt) == ['/api/billing/webhook/{provider}', '/auth/braivex/callback']
    assert exempt['/auth/braivex/callback'] == 401        # exempt from the token, not from the state cookie


# ---------------- 9. what the account keeps, and erasure ----------------

def test_the_export_shows_the_shopify_customer_number_and_erasure_removes_it(web, broker, db):
    from app import admin
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).status_code == 303
    data = web['alice'].get('/api/account/export').json()
    assert data['users'][0]['braivex_customer_id'] == SUB
    admin.erase(ALICE, ALICE)
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM users WHERE braivex_customer_id=%s', (SUB,)).fetchone()['n'] == 0
    fresh = client_for()                                      # the same Shopify customer can sign up again
    _, state = start(fresh)
    r = finish(fresh, broker.assertion(state=state))
    assert r.status_code == 303 and r.headers['location'] == '/auth/braivex/workspace'
