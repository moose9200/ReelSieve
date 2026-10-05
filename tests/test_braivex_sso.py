"""Continue with Braivex: /auth/braivex/start, the callback, and (04 Oct 2026) the only customer sign-in.

No network: accounts.braivex.com is a fake urllib opener serving a JWKS built from an RSA key generated here (the
vendored verifier fetches keys with PyJWT's PyJWKClient, which uses urllib), and every assertion is signed with that
key. Real isolated PostgreSQL. Contract and claim checks: braivex-accounts docs/PRODUCT-INTEGRATION.md and
packages/verify-py/braivex_verify.py, vendored as app/braivex_verify.py.
"""
import asyncio
import base64
import hashlib
import io
import itertools
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import auth, braivex_verify, plans, server, store

ALICE, BOB, OPS = 'alice@example.test', 'bob@example.test', 'ops@example.test'
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

    def open(self, req, timeout=None):
        """What PyJWKClient calls on the opener it builds: one GET of the published keys."""
        assert req.full_url == BROKER + '/.well-known/jwks.json', req.full_url
        self.fetches += 1
        if self.status != 200:
            raise urllib.error.URLError(f'broker answered {self.status}')
        return io.BytesIO(json.dumps({'keys': [jwk_of(self.key, self.kid)]}).encode())

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
    braivex_verify._clients.clear()
    yield
    braivex_verify._clients.clear()


@pytest.fixture
def broker(db, monkeypatch):
    fake = Broker()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: fake)
    monkeypatch.setenv('BRAIVEX_ACCOUNTS_URL', BROKER)
    monkeypatch.setenv('PUBLIC_BASE_URL', SITE)
    return fake


def client_for(session=None):
    """Over HTTPS, as in production: the sign-in flow cookies are __Host- cookies, which are Secure."""
    c = TestClient(server.app, base_url='https://testserver')
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


def finish(client, assertion, ip='198.51.100.1'):
    """The broker's cross-site POST back here. ip: the network it comes from (refusals are counted per network)."""
    return client.post('/auth/braivex/callback', data={'assertion': assertion}, headers={'x-forwarded-for': ip},
                       follow_redirects=False)


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


# ---------------- 1. Braivex is the only way a customer signs in or up (04 Oct 2026) ----------------

def legacy_password(db, email, pw='synthetic-password'):
    """A customer account made with a password before 04 Oct 2026, hashed exactly as the old sign-up did."""
    salt = secrets.token_hex(16)
    with db.connect() as c:
        c.execute('UPDATE users SET salt=%s,hash=%s,iterations=%s WHERE email=%s',
                  (salt, auth._hash(pw, salt), auth.ITERATIONS, email))


def test_sign_in_and_sign_up_offer_only_continue_with_braivex_with_no_switch_set(owners, monkeypatch):
    monkeypatch.delenv('BRAIVEX_SSO', raising=False)
    monkeypatch.delenv('BRAIVEX_PASSWORD_SUNSET', raising=False)
    anon = client_for()
    signup = anon.get('/signup').text
    assert 'Continue with Braivex' in signup and 'type="password"' not in signup
    login = anon.get('/login').text
    assert 'Continue with Braivex' in login and '/forgot' not in login
    # the one password field left is the operator's break-glass form, folded away under its own heading
    assert login.count('type="password"') == 1 and login.index('Continue with Braivex') < login.index('Operator sign-in')
    assert anon.get('/auth/braivex/start?next=/app', follow_redirects=False).status_code == 302


REMOVED = [('get', '/forgot'), ('post', '/forgot'), ('get', '/reset'), ('post', '/reset'), ('get', '/setup'),
           ('post', '/setup'), ('post', '/signup')]


@pytest.mark.parametrize('method,path', REMOVED)
def test_customer_password_routes_are_gone(owners, method, path):
    anon = client_for()
    token = csrf_of(anon.get('/login').text)       # a real form post: past the CSRF check, so the route itself answers
    form = {'csrf': token, 'user': 'new@example.test', 'password': 'a-long-password', 'password2': 'a-long-password',
            'token': 'x'}
    r = getattr(anon, method)(path, follow_redirects=False, **({'data': form} if method == 'post' else {}))
    assert r.status_code == 404, r.status_code
    assert auth.identity('new@example.test') is None


def test_changing_a_password_from_the_account_page_is_gone(web, owners):
    r = web['alice'].post('/api/account/password', json={'current': 'synthetic-password', 'new': 'another-long-pass'},
                          headers={'X-CSRF-Token': auth.csrf_token(owners['alice'])})
    assert r.status_code == 404


def test_a_customer_password_no_longer_signs_anyone_in(web, db):
    legacy_password(db, ALICE)
    anon = client_for()
    r = anon.post('/login', data={'csrf': csrf_of(anon.get('/login').text), 'user': ALICE,
                                  'password': 'synthetic-password'}, follow_redirects=False)
    assert r.status_code == 401 and 'Wrong email or password' in r.text and signed_in_as(anon) is None
    assert not auth.verify(ALICE, 'synthetic-password')


def test_an_operator_signs_in_with_a_password_and_five_wrong_tries_close_the_form_for_that_network(web, db, monkeypatch):
    monkeypatch.setattr(server, 'LOGIN_FAIL_DELAY', 0)
    auth.create_user(OPS, 'operator-password', 'admin')
    anon = client_for()
    token = csrf_of(anon.get('/login').text)
    net = {'x-forwarded-for': '198.51.100.7'}
    for _ in range(5):
        assert anon.post('/login', data={'csrf': token, 'user': OPS, 'password': 'wrong-password'}, headers=net).status_code == 401
    assert anon.post('/login', data={'csrf': token, 'user': OPS, 'password': 'operator-password'}, headers=net).status_code == 429
    r = anon.post('/login', data={'csrf': token, 'user': OPS, 'password': 'operator-password'},
                  headers={'x-forwarded-for': '203.0.113.9'}, follow_redirects=False)
    assert r.status_code == 303 and auth.check(r.cookies[auth.COOKIE]) == OPS
    assert client_for(r.cookies[auth.COOKIE]).get('/api/users').status_code == 200


def test_the_operator_password_check_and_its_delay_never_block_the_event_loop(web, db, monkeypatch):
    def on_loop():
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False
    seen, real_verify, real_sleep = [], auth.verify, time.sleep
    monkeypatch.setattr(auth, 'verify', lambda u, p: seen.append(('verify', on_loop())) or real_verify(u, p))
    monkeypatch.setattr(time, 'sleep', lambda s: seen.append(('sleep', on_loop())) or real_sleep(s))
    anon = client_for()
    r = anon.post('/login', data={'csrf': csrf_of(anon.get('/login').text), 'user': OPS, 'password': 'wrong-password'})
    assert r.status_code == 401
    assert ('verify', False) in seen and ('verify', True) not in seen and ('sleep', True) not in seen


def test_a_customer_account_never_gets_a_password_and_only_operators_can_be_given_one(db):
    with pytest.raises(ValueError, match='no password'):
        auth.create_user('new@example.test', 'a-long-password')
    auth.create_user('new@example.test')
    with pytest.raises(ValueError, match='No such operator'):
        auth.set_password('new@example.test', 'a-long-password')
    with pytest.raises(ValueError, match='at least 8'):
        auth.create_user(OPS, 'short', 'admin')
    auth.create_user(OPS, 'operator-password', 'admin')
    auth.set_password(OPS, 'another-operator-password')
    assert auth.verify(OPS, 'another-operator-password') and not auth.verify('new@example.test', '')


def test_an_admin_adds_customers_without_a_password_and_operators_only_with_one(web, db):
    auth.create_user(OPS, 'operator-password', 'admin')
    session = auth.issue(OPS)[0]
    ops, head = client_for(session), {'X-CSRF-Token': auth.csrf_token(session)}
    r = ops.post('/api/users', json={'user': 'new@example.test', 'password': 'typed-anyway', 'role': 'member'}, headers=head)
    assert r.status_code == 200
    with db.connect() as c:
        assert c.execute("SELECT salt,hash FROM users WHERE email='new@example.test'").fetchone() == {'salt': '', 'hash': ''}
    r = ops.post('/api/users/password', json={'user': 'new@example.test', 'password': 'a-long-password'}, headers=head)
    assert r.status_code == 400 and 'No such operator' in r.text
    assert ops.post('/api/users', json={'user': 'ops2@example.test', 'role': 'admin'}, headers=head).status_code == 400
    assert auth.identity('ops2@example.test') is None


VENDORED_SHA256 = '6d8ca8fb43861ea1e53e397ab7ade85c6f1b2ff06ea307a305a0b6228f3bd1f1'   # verify-py at e0a35ec


def test_the_assertion_verifier_is_the_shared_one_byte_for_byte():
    text = (server.HERE / 'braivex_verify.py').read_bytes()
    body = text[text.index(b'"""Braivex Accounts assertion verifier (Python).'):]
    assert hashlib.sha256(body).hexdigest() == VENDORED_SHA256
    header = text[:text.index(body)]
    assert b'e0a35ec' in header and b'packages/verify-py/braivex_verify.py' in header


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
    cookie = cookie_header(r, '__Host-braivex_sso_state')
    assert 'HttpOnly' in cookie and 'Max-Age=600' in cookie and 'Path=/;' in cookie + ';' and 'Domain' not in cookie
    assert 'SameSite=lax' in cookie and 'Secure' in cookie
    assert anon.cookies['__Host-braivex_sso_state'] and state != start(anon)[1]  # a new 32-byte state every time


def test_the_state_cookie_is_secure_over_https_and_the_callback_never_comes_from_the_host_header(web):
    r, _ = start(web['anon'], headers={'x-forwarded-proto': 'https', 'host': 'evil.example.test',
                                       'x-forwarded-host': 'evil.example.test'})
    assert 'Secure' in cookie_header(r, '__Host-braivex_sso_state')
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
    assert '__Host-braivex_sso_state' not in client.cookies
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
    for i, make in enumerate((lambda s: broker.assertion(state=s, sign_with=OTHER_KEY),
                              lambda s: broker.assertion(state=s, kid='never-published'),
                              lambda s: 'not-a-jws', lambda s: '', lambda s: 'a.b.c')):
        _, state = start(client)
        assert finish(client, make(state), ip=f'203.0.113.{i}').status_code == 401
    assert signed_in_as(client) is None


def test_the_same_assertion_is_spent_once_and_a_replay_is_refused(web, broker, db):
    first = client_for()
    _, state = start(first)
    good = broker.assertion(state=state, email=ALICE)
    assert finish(first, good).status_code == 303 and signed_in_as(first) == ALICE
    # A second browser replaying the assertion it stole, holding a state cookie that matches it: the spent jti is
    # the only thing left to refuse it.
    second = client_for()
    second.cookies.set(server.STATE_COOKIE, auth.seal(server.STATE_COOKIE, 600, state=state, next='/app', ref=''), path='/')
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
    assert broker.fetches == 1                               # cached (PyJWKClient: five minutes)
    broker.kid, broker.key = 'test-key-2', OTHER_KEY         # the broker rotates its signing key
    braivex_verify._clients[BROKER]._fetched_at -= braivex_verify.JWKS_REFETCH_COOLDOWN_SECONDS + 1  # past the cooldown
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


def test_a_key_the_broker_withdraws_stops_verifying_once_the_key_set_expires(web, broker):
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).status_code == 303   # signed with test-key-1
    broker.kid, broker.key = 'test-key-2', OTHER_KEY            # the broker withdraws test-key-1 (compromise, rotation)
    cache = braivex_verify._clients[BROKER].jwk_set_cache
    cache.jwk_set_with_timestamp.timestamp -= 301                 # past the five-minute key-set lifespan
    client = client_for()
    _, state = start(client)
    withdrawn = broker.assertion(state=state, email=ALICE, kid='test-key-1', sign_with=KEY)
    assert finish(client, withdrawn).status_code == 401 and broker.fetches == 2


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


def test_a_legacy_password_account_is_taken_over_by_the_braivex_sign_in_with_its_email(web, broker, db, owners):
    legacy_password(db, ALICE)                                # nothing in it yet: taken over at once
    assert auth.check(owners['alice']) == ALICE
    client = client_for()
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, email='Alice@Example.TEST'))
    assert r.status_code == 303 and r.headers['location'] == '/reels'
    assert signed_in_as(client) == ALICE and braivex_id(db, ALICE) == SUB
    assert auth.check(owners['alice']) is None            # whoever signed up with that password is signed out
    with db.connect() as c:
        row = c.execute('SELECT salt,hash FROM users WHERE email=%s', (ALICE,)).fetchone()
    assert row['salt'] == '' and row['hash'] == ''          # and the password is gone, not just unusable


def test_a_claimed_legacy_account_keeps_its_plan_and_credits(web, broker, db, owners):
    store.ensure_account(ALICE, 'starter')
    store.add_credits(ALICE, 4)
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).headers['location'] == '/auth/braivex/claim'
    page = client.get('/auth/braivex/claim').text
    assert 'Starter plan' in page and 'Google Drive not connected' in page
    r = client.post('/auth/braivex/claim', data={'csrf': csrf_of(page)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'] == '/reels' and signed_in_as(client) == ALICE
    assert store.get_account(ALICE)['plan'] == 'starter' and store.get_account(ALICE)['credits'] == 4
    assert '__Host-braivex_sso_claim' not in client.cookies


def test_a_linked_customer_signing_in_again_keeps_their_other_sessions(web, broker, db):
    first = client_for()
    _, state = start(first)
    assert finish(first, broker.assertion(state=state, email=ALICE)).status_code == 303
    again = client_for()
    _, state = start(again)
    assert finish(again, broker.assertion(state=state, email=ALICE)).status_code == 303
    assert signed_in_as(first) == ALICE and signed_in_as(again) == ALICE


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
    with db.connect() as c:                                   # no network or device hash on the account row
        assert c.execute('SELECT ip_hash,fp_hash FROM accounts WHERE owner_id=%s', (db.user_id(SAM),)).fetchone() == \
            {'ip_hash': None, 'fp_hash': None}
    assert '__Host-braivex_sso_new' not in client.cookies


def test_the_new_account_matches_one_made_before_braivex_exactly_and_has_no_password_at_all(web, broker, db):
    client, _ = new_customer(broker)
    assert make_account(client).status_code == 303
    store.ensure_account(BOB, 'free')                         # the same product, an account made before Braivex
    sso, password = plans.account_view(SAM), plans.account_view(BOB)
    assert {k: v for k, v in sso.items() if k != 'user'} == {k: v for k, v in password.items() if k != 'user'}
    assert auth.role(SAM) == 'member' and client.get('/api/users').status_code == 403
    with db.connect() as c:
        row = c.execute('SELECT hash,salt,iterations,role FROM users WHERE email=%s', (SAM,)).fetchone()
    assert row['hash'] == '' and row['salt'] == '' and row['role'] == 'member'
    for guess in ('', 'synthetic-password', 'braivex-sso:'):
        assert not auth.verify(SAM, guess), guess


def test_the_sign_up_page_offers_braivex_and_nothing_else(web):
    page = client_for().get('/signup?plan=starter').text
    assert 'Continue with Braivex' in page and 'name="password"' not in page
    assert 'Braivex sends a 6-digit code to your email' in page
    assert 'href="/auth/braivex/start?next=/upgrade%3Fplan%3Dstarter"' in page   # the plan survives the sign-in


def test_the_session_a_braivex_sign_in_creates_is_the_one_an_operator_password_creates(web, broker, db, monkeypatch):
    client, _ = new_customer(broker)
    sso = make_account(client)
    auth.create_user(OPS, 'operator-password', 'admin')
    anon = client_for()
    pw = anon.post('/login', data={'csrf': csrf_of(anon.get('/login').text), 'user': OPS,
                                   'password': 'operator-password', 'remember': '1'}, follow_redirects=False)
    flags = lambda r: sorted(p.strip().split('=')[0].lower()                                        # noqa: E731
                             for p in cookie_header(r, auth.COOKIE).split(';')[1:])
    assert flags(sso) == flags(pw) == ['httponly', 'max-age', 'path', 'samesite', 'secure']
    assert auth.check(sso.cookies[auth.COOKIE]) == SAM and auth.check(pw.cookies[auth.COOKIE]) == OPS
    with db.connect() as c:                                   # and the same revocation: session_version ends it
        c.execute('UPDATE users SET session_version=session_version+1 WHERE email=%s', (SAM,))
    assert signed_in_as(client) is None


def test_the_step_cannot_be_reached_or_replayed_without_the_verified_claims(web, broker):
    assert client_for().get('/auth/braivex/workspace').status_code == 401
    client, _ = new_customer(broker)
    page = client.get('/auth/braivex/workspace').text
    forged = client_for()
    forged.cookies.set(server.NEW_COOKIE, client.cookies[server.NEW_COOKIE] + 'x', path='/')
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


# ---------------- 7. sign-up guards and deleting an account without a password ----------------

def test_the_sign_up_guards_run_where_braivex_creates_the_account(web, broker, db, monkeypatch):
    client, _ = new_customer(broker, email='someone@mailinator.com', sub='gid://shopify/Customer/5')
    assert make_account(client).status_code == 400 and auth.identity('someone@mailinator.com') is None
    monkeypatch.setattr(store, 'count_usage', lambda **kw: 10 ** 6)     # a network that made a lot of free videos
    client, _ = new_customer(broker)
    r = make_account(client)
    assert r.status_code == 400 and 'This network has made a lot of free videos' in r.text and auth.identity(SAM) is None


def test_deleting_my_account_needs_the_typed_word_not_a_password(web, owners):
    head = {'X-CSRF-Token': auth.csrf_token(owners['bob'])}
    assert web['bob'].post('/api/account/delete', json={'confirm': 'delete'}, headers=head).status_code == 400
    r = web['bob'].post('/api/account/delete', json={'confirm': 'DELETE'}, headers=head)
    assert r.status_code == 200 and auth.identity(BOB) is None



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
    data = client.get('/api/account/export').json()       # her password-era session ended when Braivex took over
    assert data['users'][0]['braivex_customer_id'] == SUB
    admin.erase(ALICE, ALICE)
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM users WHERE braivex_customer_id=%s', (SUB,)).fetchone()['n'] == 0
    fresh = client_for()                                      # the same Shopify customer can sign up again
    _, state = start(fresh)
    r = finish(fresh, broker.assertion(state=state))
    assert r.status_code == 303 and r.headers['location'] == '/auth/braivex/workspace'


# ---------------- 10. one signed-token codec for sessions and the sign-in cookies ----------------

def test_sessions_and_sign_in_cookies_share_one_codec_and_never_pass_for_each_other(web, broker, owners):
    session = owners['alice']
    assert auth.unseal('session', session)['v'] == 1 and auth.check(session) == ALICE
    state = auth.seal(server.STATE_COOKIE, 600, state='s', next='/app', ref='')
    assert auth.check(state) is None                                   # a sign-in cookie is never a session
    client = client_for()
    client.cookies.set(server.NEW_COOKIE, session, path='/')            # nor a session a sign-in cookie
    assert client.get('/auth/braivex/workspace').status_code == 401
    assert auth.unseal('session', auth.seal('session', -1, o='x', v=1)) is None   # expired


def test_a_mangled_cookie_is_refused_not_a_server_error(web, broker):
    for value in ('abc.\u00e9', '\u00e9.\u00e9', '.', 'x.y.z'):
        raw = (server.NEW_COOKIE + '=' + value).encode('latin-1')            # a header byte no browser cookie jar would mint
        assert client_for().get('/auth/braivex/workspace', headers={'cookie': raw}).status_code == 401, value
        assert auth.check(value) is None


# ---------------- 11. same classes, swept (04 Oct 2026) ----------------

def test_a_non_ascii_csrf_token_or_webhook_signature_is_refused_not_a_server_error(web, owners, monkeypatch):
    monkeypatch.setenv('BILLING_WEBHOOK_SECRET', 'synthetic-webhook-secret')   # secrets set, so the comparison runs
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', 'whsec_synthetic')
    head = {'X-CSRF-Token': 'é'.encode('latin-1')}
    assert web['alice'].post('/api/billing/request', json={'plan': 'starter'}, headers=head).status_code == 403
    for provider, header, value in (('skydo', 'x-signature', 'é'), ('stripe', 'stripe-signature', f't={int(time.time())},v1=é')):
        r = web['anon'].post(f'/api/billing/webhook/{provider}', content=b'{}', headers={header: value.encode('latin-1')})
        assert 400 <= r.status_code < 500, (provider, r.status_code)


def test_setting_or_creating_an_operator_password_never_hashes_on_the_event_loop(web, db, monkeypatch):
    def on_loop():
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False
    seen, real = [], auth._hash
    monkeypatch.setattr(auth, '_hash', lambda *a: seen.append(on_loop()) or real(*a))
    auth.create_user(OPS, 'operator-password', 'admin')
    session = auth.issue(OPS)[0]
    ops, head = client_for(session), {'X-CSRF-Token': auth.csrf_token(session)}
    seen.clear()
    assert ops.post('/api/users', json={'user': 'ops2@example.test', 'password': 'second-password', 'role': 'admin'},
                    headers=head).status_code == 200
    assert ops.post('/api/users/password', json={'user': 'ops2@example.test', 'password': 'third-password'},
                    headers=head).status_code == 200
    assert seen and True not in seen


# ---------------- 12. classes confirmed in sibling products (04 Oct 2026) ----------------
# (2) sub is immutable: test_an_account_that_is_already_somebody_elses_shopify_customer_is_never_taken_over.

def test_the_braivex_only_release_ends_every_customer_session_and_password_once(db, owners):
    legacy_password(db, ALICE)
    auth.create_user(OPS, 'operator-password', 'admin')
    with db.connect() as c:                                     # as it was before this release was deployed
        c.execute("DELETE FROM schema_ledger WHERE name='012_braivex_only.sql'")
    alice, bob, ops = auth.issue(ALICE)[0], auth.issue(BOB)[0], auth.issue(OPS)[0]
    db.initialize()                                             # the deploy
    assert auth.check(alice) is None and auth.check(bob) is None and auth.check(ops) == OPS
    with db.connect() as c:
        rows = {r['email']: r for r in c.execute('SELECT email,salt,hash FROM users').fetchall()}
    assert rows[ALICE]['hash'] == '' == rows[ALICE]['salt'] and rows[OPS]['hash'] != ''
    fresh = auth.issue(ALICE)[0]
    db.initialize()                                             # every later start: nobody is signed out again
    assert auth.check(fresh) == ALICE and auth.check(ops) == OPS


def test_every_braivex_sign_in_leaves_the_account_without_a_usable_password(web, broker, db):
    with db.connect() as c:
        c.execute('UPDATE users SET braivex_customer_id=%s WHERE email=%s', (SUB, ALICE))
    client = client_for()                                        # (its start wipes hashes: set one after it)
    legacy_password(db, ALICE)                                   # linked, yet a hash is there (an older build wrote it)
    old = auth.issue(ALICE)[0]
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).status_code == 303
    with db.connect() as c:
        assert c.execute('SELECT hash FROM users WHERE email=%s', (ALICE,)).fetchone()['hash'] == ''
    assert auth.check(old) is None and signed_in_as(client) == ALICE


def test_taking_over_an_account_with_data_needs_an_explicit_claim_and_clears_the_prior_holders_links(
        web, broker, db, owners, google):
    from fakes import connect
    from app import gdrive, referrals
    connect(owners, google)                                      # the prior holder's Google Drive
    store.set_b2b_sender(ALICE, 'Prior Holder', 'Prior Lets', 'prior@example.test')
    code_before = referrals.code_for(ALICE)
    assert referrals.attribute(ALICE, referrals.code_for(BOB)) == 'pending'
    client = client_for()
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, email=ALICE))
    assert r.status_code == 303 and r.headers['location'] == '/auth/braivex/claim'
    assert braivex_id(db, ALICE) is None and signed_in_as(client) is None and auth.check(owners['alice']) == ALICE
    page = client.get('/auth/braivex/claim')
    assert page.status_code == 200 and 'Claim this account' in page.text and ALICE in page.text
    assert client.post('/auth/braivex/claim', data={}).status_code == 403                      # CSRF still applies
    r = client.post('/auth/braivex/claim', data={'csrf': csrf_of(page.text)}, follow_redirects=False)
    assert r.status_code == 303 and signed_in_as(client) == ALICE and braivex_id(db, ALICE) == SUB
    assert auth.check(owners['alice']) is None
    assert not gdrive.status(ALICE)['connected'] and google.calls[-1].url.path == '/revoke'
    assert store.get_account(ALICE)['b2b_sender'] is None and referrals.code_for(ALICE) != code_before
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM referrals WHERE referee_id=%s', (db.user_id(ALICE),)).fetchone()['n'] == 0
    assert client_for().get('/auth/braivex/claim').status_code == 401                            # no claim cookie


def test_sign_in_flow_cookies_are_host_prefixed_and_next_never_holds_a_double_slash(web):
    r, _ = start(client_for())
    cookie = cookie_header(r, '__Host-braivex_sso_state')
    assert 'Secure' in cookie and 'Path=/;' in cookie + ';' and 'HttpOnly' in cookie and 'Domain' not in cookie
    for bad in ('/x//evil.example.test', '/app?\x00', '//evil.example.test', '/\\evil.example.test'):
        assert server._safe_next(bad) == '/app', bad


def test_a_double_submitted_first_sign_up_reuses_the_account_it_made(web, broker, db):
    client, _ = new_customer(broker)
    twin = client_for()
    twin.cookies.set('__Host-braivex_sso_new', client.cookies['__Host-braivex_sso_new'], path='/')
    page = twin.get('/auth/braivex/workspace').text
    assert make_account(client).status_code == 303
    r = twin.post('/auth/braivex/workspace', data={'csrf': csrf_of(page)}, follow_redirects=False)
    assert r.status_code == 303 and signed_in_as(twin) == SAM
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM users WHERE email=%s', (SAM,)).fetchone()['n'] == 1


def test_refused_braivex_sign_ins_are_limited_per_ipv6_64(web, broker):
    for i in range(5):
        client = client_for()
        _, state = start(client)
        r = client.post('/auth/braivex/callback', data={'assertion': broker.assertion(state=state, aud='loculens')},
                        headers={'x-forwarded-for': f'2001:db8:1:2::{i + 1}'})
        assert r.status_code == 401
    client = client_for()
    _, state = start(client)
    r = client.post('/auth/braivex/callback', data={'assertion': broker.assertion(state=state)},
                    headers={'x-forwarded-for': '2001:db8:1:2::99'})                # same /64, another address
    assert r.status_code == 429 and signed_in_as(client) is None
    client = client_for()
    _, state = start(client)
    r = client.post('/auth/braivex/callback', data={'assertion': broker.assertion(state=state)},
                    headers={'x-forwarded-for': '2001:db8:1:3::1'}, follow_redirects=False)  # another /64
    assert r.status_code == 303


# ---------------- 13. review round 1 (04 Oct 2026) ----------------

def on_the_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_r1_deleting_an_account_needs_a_braivex_sign_in_from_the_last_ten_minutes(web, broker, db):
    stale = auth.issue(BOB, auth_time=time.time() - 601)[0]
    head = {'X-CSRF-Token': auth.csrf_token(stale)}
    r = client_for(stale).post('/api/account/delete', json={'confirm': 'DELETE'}, headers=head)
    assert r.status_code == 403 and r.json()['reauth'] == '/auth/braivex/start?next=/settings'
    assert auth.identity(BOB) is not None
    client = client_for()                                        # Braivex authenticated 11 minutes ago: still stale
    _, state = start(client)
    old = int(time.time()) - 660
    assert finish(client, broker.assertion(state=state, email=BOB, iat=old)).status_code == 303
    assert auth.unseal('session', client.cookies[auth.COOKIE])['at'] == old
    session = client.cookies[auth.COOKIE]
    assert client.post('/api/account/delete', json={'confirm': 'DELETE'},
                       headers={'X-CSRF-Token': auth.csrf_token(session)}).status_code == 403
    _, state = start(client)                                     # sign in again: fresh
    assert finish(client, broker.assertion(state=state, email=BOB)).status_code == 303
    session = client.cookies[auth.COOKIE]
    r = client.post('/api/account/delete', json={'confirm': 'DELETE'}, headers={'X-CSRF-Token': auth.csrf_token(session)})
    assert r.status_code == 200 and auth.identity(BOB) is None


def test_r2_this_release_never_drops_a_table_an_older_build_still_uses(db):
    with db.connect() as c:                                      # prod has it: the old container may still read it
        c.execute('CREATE TABLE IF NOT EXISTS password_resets (token_hash TEXT PRIMARY KEY, owner_id TEXT, '
                  'created DOUBLE PRECISION, expires_at DOUBLE PRECISION)')
        c.execute("DO $$ BEGIN IF to_regclass('schema_ledger') IS NOT NULL THEN DELETE FROM schema_ledger; END IF; END $$")
    db.initialize()                                              # every ledgered file runs again, as on a fresh deploy
    with db.connect() as c:
        assert c.execute("SELECT to_regclass('password_resets') IS NOT NULL AS there").fetchone()['there']


def test_r3_session_and_csrf_cookies_are_host_prefixed_and_the_old_names_are_ignored(web, db):
    auth.create_user(OPS, 'operator-password', 'admin')
    anon = client_for()
    page = anon.get('/login')
    nonce = cookie_header(page, '__Host-reelsieve_csrf')
    assert nonce and 'Secure' in nonce and 'Path=/;' in nonce + ';' and 'Domain' not in nonce
    r = anon.post('/login', data={'csrf': csrf_of(page.text), 'user': OPS, 'password': 'operator-password'},
                  follow_redirects=False)
    session = cookie_header(r, '__Host-reelsieve_session')
    assert r.status_code == 303 and 'Secure' in session and 'Path=/;' in session + ';' and 'Domain' not in session
    old = TestClient(server.app, base_url='https://testserver')
    old.cookies.set('reelsieve_session', auth.issue(OPS)[0])
    assert old.get('/api/account').status_code == 401


def test_r4_a_worker_never_migrates_and_refuses_to_start_on_an_older_schema(db, monkeypatch):
    from app import database
    monkeypatch.setattr(database, 'initialize', lambda: pytest.fail('a worker ran the migrations'))
    with db.connect() as c:
        c.execute("DELETE FROM schema_ledger WHERE name='012_braivex_only.sql'")
    with pytest.raises(SystemExit) as stop:
        database.wait_for_schema(0)
    assert stop.value.code != 0 and '012_braivex_only.sql' in str(stop.value) and 'web' in str(stop.value)


def test_r4_the_web_migrates_and_a_worker_on_the_same_schema_starts(db):
    from app import database
    with TestClient(server.app):                                 # the web process's lifespan migrates
        pass
    database.wait_for_schema(0)                                  # nothing missing: returns


def test_r5_an_ipv4_mapped_address_counts_as_its_ipv4_address(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', 'synthetic-test-session-secret-only')
    assert auth._ip_key('::ffff:203.0.113.7') == auth._ip_key('203.0.113.7')
    assert auth._ip_key('::ffff:203.0.113.7') != auth._ip_key('::ffff:198.51.100.7')
    assert store.net_of('::ffff:203.0.113.7') == store.net_of('203.0.113.7') == '203.0.113.0/24'


def test_r6_a_customer_hash_is_wiped_at_every_start_and_sessions_end_only_once(db, owners):
    legacy_password(db, ALICE)                                    # e.g. an older build set one during the deploy
    alice = owners['alice']
    db.initialize()
    with db.connect() as c:
        assert c.execute('SELECT hash FROM users WHERE email=%s', (ALICE,)).fetchone()['hash'] == ''
    assert auth.check(alice) == ALICE                             # the one-time sign-out already ran


def test_r7_a_failed_link_leaves_the_account_holders_drive_connected(web, broker, db, owners, google, monkeypatch):
    from fakes import connect
    from app import gdrive
    connect(owners, google)
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).headers['location'] == '/auth/braivex/claim'
    page = client.get('/auth/braivex/claim').text
    monkeypatch.setattr(auth, 'link_braivex', lambda *a: None)   # lost a race: someone else linked it first
    assert client.post('/auth/braivex/claim', data={'csrf': csrf_of(page)}).status_code == 401
    assert gdrive.status(ALICE)['connected'] and not [c for c in google.calls if c.url.path == '/revoke']


def test_r8_the_per_request_session_check_and_the_google_revoke_run_off_the_event_loop(web, owners, google, monkeypatch):
    from fakes import connect
    from app import gdrive
    connect(owners, google)
    seen, check, disconnect = [], auth.check, gdrive.disconnect_owner
    monkeypatch.setattr(auth, 'check', lambda t: seen.append(('check', on_the_loop())) or check(t))
    monkeypatch.setattr(gdrive, 'disconnect_owner', lambda o: seen.append(('revoke', on_the_loop())) or disconnect(o))
    r = web['alice'].post('/api/account/delete', json={'confirm': 'DELETE'},
                          headers={'X-CSRF-Token': auth.csrf_token(owners['alice'])})
    assert r.status_code == 200
    assert ('check', False) in seen and ('revoke', False) in seen and not [s for s in seen if s[1]]


def test_r9_a_ledgered_file_runs_once_and_an_edited_one_stops_the_start(db):
    with db.connect() as c:
        rows = c.execute('SELECT name,sha256 FROM schema_ledger').fetchall()
    assert [r['name'] for r in rows] == ['012_braivex_only.sql'] and len(rows[0]['sha256']) == 64
    with db.connect() as c:
        c.execute("UPDATE schema_ledger SET sha256=repeat('0', 64) WHERE name='012_braivex_only.sql'")
    with pytest.raises(RuntimeError, match='012_braivex_only.sql changed after it was applied'):
        db.initialize()


def test_an_existing_account_on_our_own_domains_links_without_a_claim_but_its_outbound_grants_go(web, broker, db, google):
    """Controller ruling 04 Oct 2026, amended: accounts that exist at this release on braivex.com, wbj.team and
    mokshabotanicals.in link with no Claim and keep their orders and invites. A domain does not prove the row was not
    squatted, so on the first link of ANY older account the grants that deliver without a sign-in go: the Google
    Drive connection (revoked at Google) and the business sender. Other domains keep the Claim path."""
    from urllib.parse import parse_qs as qs, urlparse
    from app import billing, gdrive, referrals
    pat, guest, friend = 'pat@braivex.com', 'guest@gmail.com', 'friend@example.org'
    for email in (pat, guest):
        auth.create_user(email)
        store.set_b2b_sender(email, 'Pat', 'Braivex', email)
    tok = auth.issue(pat)[0]
    url = gdrive.auth_url('https://app.test/callback', pat, tok)
    gdrive.exchange('synthetic-code', qs(urlparse(url).query)['state'][0], 'https://app.test/callback', pat, tok)
    order = billing.create_order(pat, 'starter')['ref']
    code = referrals.code_for(pat)
    auth.create_user(friend)
    assert referrals.attribute(friend, code) == 'pending'
    with db.connect() as c:                                      # these accounts exist when this release first starts
        c.execute("DELETE FROM schema_ledger WHERE name='012_braivex_only.sql'")
    db.initialize()
    client = client_for()
    legacy_password(db, pat)                                     # (set after the client's own start, which wipes it)
    old = auth.issue(pat)[0]
    _, state = start(client)
    r = finish(client, broker.assertion(state=state, email=pat, sub='gid://shopify/Customer/41'))
    assert r.status_code == 303 and r.headers['location'] == '/reels' and signed_in_as(client) == pat   # no Claim
    assert not gdrive.status(pat)['connected'] and [c for c in google.calls if c.url.path == '/revoke']
    assert store.get_account(pat)['b2b_sender'] is None
    assert billing.get_order(order)['owner_id'] == db.user_id(pat) and referrals.code_for(pat) == code
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM referrals WHERE referrer_id=%s', (db.user_id(pat),)).fetchone()['n'] == 1
        assert c.execute('SELECT hash FROM users WHERE email=%s', (pat,)).fetchone()['hash'] == ''
    assert auth.check(old) is None and braivex_id(db, pat) == 'gid://shopify/Customer/41'
    other = client_for()                                         # gmail.com: still asked to claim
    _, state = start(other)
    r = finish(other, broker.assertion(state=state, email=guest, sub='gid://shopify/Customer/42'))
    assert r.headers['location'] == '/auth/braivex/claim'


# ---------------- 14. review round 2 (05 Oct 2026) ----------------

def test_n1_the_drive_grant_goes_with_the_link_even_when_the_google_revoke_blows_up(web, broker, db, owners, google,
                                                                                    monkeypatch):
    from fakes import connect
    from app import gdrive
    connect(owners, google)

    def boom(*_a):
        raise ValueError('synthetic failure after the link committed')
    monkeypatch.setattr(gdrive, 'disconnect_owner', boom)
    monkeypatch.setattr(gdrive, 'revoke', boom, raising=False)
    client = client_for()
    _, state = start(client)
    assert finish(client, broker.assertion(state=state, email=ALICE)).headers['location'] == '/auth/braivex/claim'
    page = client.get('/auth/braivex/claim').text
    r = client.post('/auth/braivex/claim', data={'csrf': csrf_of(page)}, follow_redirects=False)
    assert r.status_code == 303 and signed_in_as(client) == ALICE and braivex_id(db, ALICE) == SUB
    with db.connect() as c:                                        # the grant went in the link's own transaction
        rows = c.execute('SELECT status,credentials,google_sub FROM drive_connections WHERE owner_id=%s',
                         (db.user_id(ALICE),)).fetchall()
    assert all(r['credentials'] is None and r['google_sub'] is None and r['status'] != 'connected' for r in rows)
    assert not gdrive.status(ALICE)['connected']
    again = client_for()                                           # the retry is a plain, clean sign-in
    _, state = start(again)
    r = finish(again, broker.assertion(state=state, email=ALICE))
    assert r.status_code == 303 and r.headers['location'] == '/reels' and signed_in_as(again) == ALICE


def test_n2_only_the_web_process_migrates_whatever_the_environment(db, monkeypatch):
    from app import database, start as entry, worker
    monkeypatch.setattr(database, 'initialize', lambda: pytest.fail('migrated outside the web process'))
    monkeypatch.setattr(entry, 'supervise', lambda cmds: 0)
    monkeypatch.setattr(entry, 'health_server', lambda *a: None)
    for web in ('1', '0'):                                         # the supervisor never migrates, either way
        monkeypatch.setenv('WEB_ENABLED', web)
        with pytest.raises(SystemExit) as done:
            entry.main()
        assert done.value.code == 0
    with db.connect() as c:
        c.execute("DELETE FROM schema_ledger WHERE name='012_braivex_only.sql'")
    monkeypatch.setenv('WEB_ENABLED', '1')                         # a worker told it is the web still never migrates
    monkeypatch.setattr(database, 'SCHEMA_WAIT', 0)
    monkeypatch.setattr(worker, 'run_once', lambda *_a: (_ for _ in ()).throw(SystemExit('looped without a schema check')))
    with pytest.raises(SystemExit) as stop:
        worker.main()
    assert stop.value.code != 0 and '012_braivex_only.sql' in str(stop.value) and 'web service' in str(stop.value)


def test_n3_a_worker_waits_long_enough_to_span_railways_healthcheck_window():
    from app import database
    assert 270 <= database.SCHEMA_WAIT < 300


# ---------------- 15. final review (05 Oct 2026) ----------------

def test_p10_twenty_parallel_operator_guesses_from_one_network_hash_at_most_five(web, db, monkeypatch):
    """The probe from the final review: the attempt is counted before the password is hashed, under a per-network lock."""
    import concurrent.futures as cf
    import threading
    monkeypatch.setattr(server, 'LOGIN_FAIL_DELAY', 0)
    auth.create_user(OPS, 'operator-password', 'admin')
    hashed, lock, real = [], threading.Lock(), auth.verify

    def counting_verify(u, p):
        with lock:
            hashed.append(1)
        return real(u, p)
    monkeypatch.setattr(auth, 'verify', counting_verify)
    anon = client_for()
    token = csrf_of(anon.get('/login').text)
    net = {'x-forwarded-for': '198.51.100.7'}

    def guess(i):
        return anon.post('/login', data={'csrf': token, 'user': OPS, 'password': f'wrong-{i}'}, headers=net).status_code
    with cf.ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(guess, range(20)))
    assert len(hashed) <= 5 and codes.count(401) <= 5 and codes.count(429) >= 15, sorted(codes)


def test_p10_an_attempt_that_errors_gives_its_slot_back(web, db, monkeypatch):
    monkeypatch.setattr(server, 'LOGIN_FAIL_DELAY', 0)
    auth.create_user(OPS, 'operator-password', 'admin')

    def broken(*_a):
        raise RuntimeError('synthetic database failure')
    anon = TestClient(server.app, raise_server_exceptions=False)
    token = csrf_of(anon.get('/login').text)
    net = {'x-forwarded-for': '198.51.100.8'}
    real = auth.verify
    monkeypatch.setattr(auth, 'verify', broken)
    for _ in range(6):
        assert anon.post('/login', data={'csrf': token, 'user': OPS, 'password': 'x'}, headers=net).status_code == 500
    monkeypatch.setattr(auth, 'verify', real)
    r = anon.post('/login', data={'csrf': token, 'user': OPS, 'password': 'operator-password'}, headers=net,
                  follow_redirects=False)
    assert r.status_code == 303


def test_p3_the_web_start_logs_an_error_when_production_has_the_wrong_public_address(monkeypatch, capsys):
    monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'production')
    for value in (None, 'https://reelsieve.braivex.com', 'http://www.reelsieve.braivex.com'):
        if value is None:
            monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
        else:
            monkeypatch.setenv('PUBLIC_BASE_URL', value)
        server.check_public_base()
        assert 'PUBLIC_BASE_URL' in capsys.readouterr().out, value
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://www.reelsieve.braivex.com')
    server.check_public_base()
    assert capsys.readouterr().out == ''
    monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'staging')
    monkeypatch.delenv('PUBLIC_BASE_URL')
    server.check_public_base()
    assert capsys.readouterr().out == ''
