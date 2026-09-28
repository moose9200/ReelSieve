"""Self-service password reset (28 Sep 2026): /forgot, the emailed link and /reset.

Resend is an HTTPX MockTransport; no network, no real key, no real email. Real isolated PostgreSQL.
"""
import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, server, store

ALICE, BOB = 'alice@example.test', 'bob@example.test'
SITE = 'https://www.reelsieve.braivex.com'
NEW = 'a-brand-new-password'


class Resend:
    """Just enough of POST https://api.resend.com/emails to record what we would have sent."""

    def __init__(self):
        self.sent, self.status = [], 200

    def handle(self, req):
        assert req.url.host == 'api.resend.com' and req.url.path == '/emails' and req.method == 'POST'
        assert req.headers['authorization'] == 'Bearer synthetic-resend-key'
        self.sent.append(json.loads(req.content))
        return httpx.Response(self.status, json={'id': 'synthetic-message-id'})

    def link(self, i=-1):
        return re.search(r'https?://\S+/reset\?token=\S+', self.sent[i]['text']).group(0)


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


@pytest.fixture
def mailer(db, monkeypatch):
    fake = Resend()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setenv('RESEND_API_KEY', 'synthetic-resend-key')
    monkeypatch.setenv('RESEND_FROM', 'ReelSieve <no-reply@example.test>')
    monkeypatch.setenv('PUBLIC_BASE_URL', SITE)
    return fake


@pytest.fixture
def web(owners, mailer):
    clients = {name: client_for(tok) for name, tok in owners.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def csrf_of(page):
    """The token every page carries, in its form or in the meta tag scripts read."""
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', page).group(1)


def ask(client, email, **extra):
    page = client.get('/forgot').text
    return client.post('/forgot', data={'csrf': csrf_of(page), 'user': email}, **extra)


def use(client, link, password=NEW, repeat=None):
    """Open the emailed link, then send the form back, as a person would."""
    page = client.get(link.replace(SITE, '')).text
    token = re.search(r'token=([^&\s]+)', link).group(1)
    return client.post('/reset', data={'csrf': csrf_of(page), 'token': token, 'password': password,
                                       'password2': password if repeat is None else repeat}, follow_redirects=False)


def reset_rows(db, email=ALICE):
    with db.connect() as c:
        return c.execute('SELECT * FROM password_resets WHERE owner_id=%s', (db.user_id(email),)).fetchall()


# ---------------- the page tells nobody who has an account ----------------

def test_a_known_and_an_unknown_address_get_exactly_the_same_page(web, mailer):
    known = ask(web['anon'], ALICE)
    unknown = ask(client_for(), 'nobody@example.test')
    assert known.status_code == unknown.status_code == 200
    plain = lambda r, email: re.sub(r'content="[0-9a-f]{32}"', '', r.text).replace(email, 'X')  # minus each browser's own CSRF token
    assert plain(known, ALICE) == plain(unknown, 'nobody@example.test')
    assert 'a reset link is on its way' in known.text
    assert [m['to'] for m in mailer.sent] == [[ALICE]]  # only the real account was written to


def test_the_email_carries_a_single_use_link_built_from_the_configured_site(web, mailer):
    ask(web['anon'], ALICE)
    message = mailer.sent[0]
    assert message['from'] == 'ReelSieve <no-reply@example.test>' and message['subject'] == 'Reset your ReelSieve password'
    assert mailer.link().startswith(SITE + '/reset?token=')
    assert NEW not in message['text'] and 'password' in message['text']


def test_the_link_never_comes_from_the_request_host_header(web, mailer):
    ask(web['anon'], ALICE, headers={'Host': 'evil.example.test', 'X-Forwarded-Host': 'evil.example.test'})
    assert mailer.link().startswith(SITE + '/reset?token=')


# ---------------- the link itself ----------------

def test_the_link_sets_the_password_once_and_signs_every_session_out(web, mailer, owners, db):
    alice = web['alice']
    assert alice.get('/api/account').status_code == 200
    ask(web['anon'], ALICE)
    r = use(client_for(), mailer.link())
    assert r.status_code == 303 and r.headers['location'] == '/login?notice=pw'
    assert auth.verify(ALICE, NEW) and not auth.verify(ALICE, 'synthetic-password')
    assert alice.get('/api/account').status_code == 401  # the session she had is void
    assert auth.check(owners['alice']) is None
    assert reset_rows(db) == []                          # the link is spent, and no other link survives
    again = use(client_for(), mailer.link(), password='another-new-password')
    assert again.status_code == 400 and 'expired or has already been used' in again.text
    assert auth.verify(ALICE, NEW)


def test_asking_again_kills_the_earlier_link(web, mailer, db):
    ask(web['anon'], ALICE)
    first = mailer.link()
    ask(client_for(), ALICE)
    second = mailer.link()
    assert first != second and len(reset_rows(db)) == 1
    assert use(client_for(), first).status_code == 400
    assert use(client_for(), second).status_code == 303 and auth.verify(ALICE, NEW)


def test_an_expired_link_is_refused_and_retention_deletes_it(web, mailer, db):
    from app import retention
    ask(web['anon'], ALICE)
    link = mailer.link()
    with db.connect() as c:
        row = c.execute('SELECT created,expires_at FROM password_resets').fetchone()
        assert 59 * 60 <= row['expires_at'] - row['created'] <= 61 * 60
        c.execute('UPDATE password_resets SET expires_at=%s', (time.time() - 1,))
    page = client_for().get(link).text
    assert 'has expired or has already been used' in page and 'name="token"' not in page
    assert use(client_for(), link).status_code == 400 and not auth.verify(ALICE, NEW)
    assert retention.run()['password_resets'] == 1 and reset_rows(db) == []


def test_a_short_password_is_refused_and_the_link_still_works(web, mailer, db):
    ask(web['anon'], ALICE)
    link = mailer.link()
    short = use(client_for(), link, password='short')
    assert short.status_code == 400 and 'at least 8 characters' in short.text
    assert len(reset_rows(db)) == 1  # a refused attempt does not spend the link
    mismatch = use(client_for(), link, repeat='something-else-entirely')
    assert mismatch.status_code == 400 and 'do not match' in mismatch.text
    assert use(client_for(), link).status_code == 303 and auth.verify(ALICE, NEW)


def test_the_reset_page_asks_browsers_not_to_send_the_token_anywhere(web, mailer):
    ask(web['anon'], ALICE)
    r = client_for().get(mailer.link())
    assert r.headers['referrer-policy'] == 'no-referrer'
    assert client_for().get('/login').headers['referrer-policy'] == 'strict-origin-when-cross-origin'


# ---------------- a form from another site is refused ----------------

def test_both_forms_need_this_browsers_own_token(web, mailer, db):
    anon = client_for()
    anon.get('/forgot')  # a cross-site form has the browser's cookies but not the token in the page
    assert anon.post('/forgot', data={'user': ALICE}).status_code == 403
    assert anon.post('/forgot', data={'csrf': 'f' * 32, 'user': ALICE}).status_code == 403
    assert mailer.sent == [] and reset_rows(db) == []
    ask(web['anon'], ALICE)
    link = mailer.link()
    attacker = client_for()
    attacker.get(link)
    token = re.search(r'token=(\S+)', link).group(1)
    assert attacker.post('/reset', data={'token': token, 'password': NEW, 'password2': NEW}).status_code == 403
    assert attacker.post('/reset', data={'csrf': 'f' * 32, 'token': token, 'password': NEW, 'password2': NEW}).status_code == 403
    assert not auth.verify(ALICE, NEW) and auth.verify(ALICE, 'synthetic-password')


# ---------------- limits ----------------

def test_requests_are_limited_per_network_and_per_address(web, mailer):
    for i in range(5):
        assert ask(client_for(), ALICE, headers={'X-Forwarded-For': f'2001:db8:5:5::{i + 1}'}).status_code == 200
    # same /64, another address: the network is spent, exactly as sign-in counts it
    assert ask(client_for(), BOB, headers={'X-Forwarded-For': '2001:db8:5:5::99'}).status_code == 429
    assert ask(client_for(), BOB, headers={'X-Forwarded-For': '203.0.113.9'}).status_code == 200
    for i in range(4):
        ask(client_for(), ALICE, headers={'X-Forwarded-For': f'203.0.113.{20 + i}'})
    # a fresh network, but this address has had its five: one mailbox cannot be flooded from many places
    flooded = ask(client_for(), ALICE, headers={'X-Forwarded-For': '198.51.100.4'})
    assert flooded.status_code == 429 and 'Too many' in flooded.text
    assert len([m for m in mailer.sent if m['to'] == [ALICE]]) == 5


# ---------------- nothing at all happens until Resend is configured ----------------

def test_without_a_resend_key_the_page_is_honest_and_sends_nothing(web, mailer, db, monkeypatch):
    monkeypatch.delenv('RESEND_API_KEY')
    page = client_for().get('/forgot').text
    assert 'not switched on yet' in page and 'hello@braivex.com' in page and 'Settings → Users' in page
    assert 'name="user"' not in page
    assert ask(client_for(), ALICE).status_code == 200
    assert mailer.sent == [] and reset_rows(db) == []
    assert 'password-reset emails are switched off at the moment' in client_for().get('/privacy').text


def test_the_notice_names_resend_while_it_is_switched_on(web):
    notice = client_for().get('/privacy').text
    assert 'Resend' in notice and 'the email service that delivers the password-reset email' in notice
    assert 'switched off at the moment' not in notice


# ---------------- what the account keeps about a reset ----------------

def test_the_export_shows_the_times_but_never_the_token_hash(web, mailer, db):
    ask(web['anon'], ALICE)
    with db.connect() as c:
        stored = c.execute('SELECT token_hash FROM password_resets').fetchone()['token_hash']
    token = re.search(r'token=(\S+)', mailer.link()).group(1)
    data = web['alice'].get('/api/account/export')
    assert data.status_code == 200 and len(data.json()['password_resets']) == 1
    assert set(data.json()['password_resets'][0]) == {'created', 'expires_at'}
    assert stored not in data.text and token not in data.text
    assert store.export(ALICE)['password_resets'][0]['expires_at'] > time.time()


def test_erasing_the_account_deletes_its_reset_links(web, mailer, db):
    from app import admin
    ask(web['anon'], ALICE)
    ask(client_for(), BOB)
    admin.erase(ALICE, ALICE)
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM password_resets').fetchone()['n'] == 1  # Bob's stays
    assert use(client_for(), mailer.link(0)).status_code == 400
