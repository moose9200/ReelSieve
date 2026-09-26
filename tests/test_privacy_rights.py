"""Data-protection rights and minimisation (UK GDPR, EU GDPR, India DPDP) against real isolated PostgreSQL.
Google is the synthetic fake; nothing leaves the machine."""
import hashlib
import hmac
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, billing, server

ALICE, BOB, ADMIN = 'alice@example.test', 'bob@example.test', 'operator@example.test'
ORDER_VIEW = {'ref', 'ts', 'plan', 'amount_usd', 'provider', 'status', 'paid_at', 'pay_link', 'note', 'user'}


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


def post(client, url, body=None, **headers):
    headers['X-CSRF-Token'] = auth.csrf_token(client.cookies.get(auth.COOKIE))
    return client.post(url, json=body or {}, headers=headers)


@pytest.fixture
def web(owners, monkeypatch):
    for k in ('PUBLIC_BASE_URL', 'RAILWAY_PUBLIC_DOMAIN', 'CHECKOUT_STARTER', 'CHECKOUT_COMMERCIAL', 'STRIPE_SECRET_KEY',
              'STRIPE_WEBHOOK_SECRET'):
        monkeypatch.delenv(k, raising=False)
    auth.create_user(ADMIN, 'operator-password', 'admin')
    clients = {name: client_for(tok) for name, tok in {**owners, 'admin': auth.issue(ADMIN)[0]}.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


# ---------------- 1. orders keep no raw IP ----------------

def test_new_orders_store_no_payer_ip(web, db, monkeypatch):
    monkeypatch.setenv('CHECKOUT_STARTER', 'https://checkout.provider.test/starter')
    for path in ('/api/billing/request', '/api/billing/start'):
        r = post(web['alice'], path, {'plan': 'starter'}, **{'X-Forwarded-For': '203.0.113.77'})
        assert r.status_code == 200, r.text
    with db.connect() as c:
        metas = [r['meta'] for r in c.execute('SELECT meta FROM orders').fetchall()]
    assert len(metas) == 2 and not any('203.0.113.77' in (m or '') or 'ip' in json.loads(m or '{}') for m in metas)


def test_schema_strips_payer_ip_from_existing_orders_and_is_idempotent(db):
    auth.create_user(ALICE, 'long-initial-password')
    owner = db.user_id(ALICE)
    metas = {'RS-1': '{"ip": "203.0.113.5", "stripe_session": "cs_test_9"}', 'RS-2': '{"ip": "198.51.100.7"}',
             'RS-3': 'legacy "ip": 203.0.113.9 not json', 'RS-4': '"ip"', 'RS-5': '{"stripe_session": "cs_test_2"}', 'RS-6': None}
    with db.connect() as c:
        for ref, meta in metas.items():
            c.execute("INSERT INTO orders(owner_id,ref,ts,plan,amount_usd,meta) VALUES(%s,%s,%s,'starter',100,%s)",
                      (owner, ref, time.time(), meta))
    db.initialize()
    db.initialize()
    with db.connect() as c:
        got = {r['ref']: r['meta'] for r in c.execute('SELECT ref,meta FROM orders').fetchall()}
    assert json.loads(got['RS-1']) == {'stripe_session': 'cs_test_9'}
    assert json.loads(got['RS-2']) == {} and json.loads(got['RS-3']) == {} and json.loads(got['RS-4']) == {}
    assert got['RS-5'] == metas['RS-5'] and got['RS-6'] is None
    assert not any(x in str(got) for x in ('203.0.113', '198.51.100'))


def test_order_views_carry_only_what_the_pages_show(web):
    ref = post(web['alice'], '/api/billing/request', {'plan': 'starter', 'note': 'Synthetic Ltd, VAT XX123'}).json()['order']['ref']
    assert set(billing.orders()[0]) == ORDER_VIEW
    for rows in (web['admin'].get('/api/billing/orders?all=1').json()['orders'], web['alice'].get('/api/billing/orders').json()['orders']):
        assert [set(o) for o in rows] == [ORDER_VIEW]
    for path, body in (('/api/billing/link', {'ref': ref, 'url': 'https://pay.provider.test/one'}), ('/api/billing/settle', {'ref': ref})):
        order = post(web['admin'], path, body).json()['order']
        assert set(order) == ORDER_VIEW, path


# ---------------- 7. abuse signals: minimal, pseudonymised, described honestly ----------------

def test_signup_stores_no_fingerprint_and_no_network_hash_on_the_account(web, db):
    page = web['anon'].get('/signup').text
    assert 'name="fp"' not in page
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    r = web['anon'].post('/signup', data={'csrf': token, 'user': 'carol@example.org', 'password': 'long-enough-pass',
                                          'fp': 'client-supplied-print'}, headers={'X-Forwarded-For': '203.0.113.9'},
                         follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as c:
        assert c.execute('SELECT ip_hash,fp_hash FROM accounts').fetchall() == [{'ip_hash': None, 'fp_hash': None}]


def test_network_hash_is_stored_only_for_free_videos(owners, db):
    from app import plans, store
    store.set_plan(BOB, 'starter', 3)
    plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/1', 'free-job', '203.0.113.9')
    plans.reserve(BOB, 'https://www.airbnb.co.uk/rooms/2', 'paid-job', '203.0.113.9')
    plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/1', 'rerun-job', '203.0.113.9')  # same listing: not counted, not kept
    with db.connect() as c:
        rows = {r['job_id']: r for r in c.execute('SELECT job_id,ip_hash,fp_hash FROM usage').fetchall()}
    assert rows['free-job']['ip_hash'] == store.ip_hash('203.0.113.9')
    assert rows['paid-job']['ip_hash'] is None and rows['rerun-job']['ip_hash'] is None
    assert all(r['fp_hash'] is None for r in rows.values())
    assert store.count_usage(ip='203.0.113.200', since_days=30) == 1  # the same /24 network


def test_network_hash_uses_a_key_dedicated_to_that_purpose(db):
    from app import store
    session_keyed = hmac.new(auth.secret().encode(), b'ip:203.0.113.0/24', hashlib.sha256).hexdigest()[:32]
    assert store.ip_hash('203.0.113.9') == store.ip_hash('203.0.113.77') != session_keyed


def test_schema_clears_fingerprint_hashes_already_stored(db):
    auth.create_user(ALICE, 'long-initial-password')
    owner = db.user_id(ALICE)
    with db.connect() as c:
        c.execute("INSERT INTO accounts(owner_id,created,ip_hash,fp_hash) VALUES(%s,%s,'net','dev')", (owner, time.time()))
        c.execute("INSERT INTO usage(owner_id,ts,plan,kind,ip_hash,fp_hash) VALUES(%s,%s,'free','video','net','dev')", (owner, time.time()))
    db.initialize()
    with db.connect() as c:
        assert c.execute('SELECT ip_hash,fp_hash FROM accounts').fetchone() == {'ip_hash': None, 'fp_hash': None}
        assert c.execute('SELECT ip_hash,fp_hash FROM usage').fetchone() == {'ip_hash': 'net', 'fp_hash': None}


def test_account_page_describes_the_network_code_as_pseudonymised(web):
    page = web['alice'].get('/account').text
    card = page.split('Your data', 1)[1]
    assert 'pseudonymised' in card and 'href="/privacy"' in card
    assert not any(w in card.lower() for w in ('one-way', 'irreversible', 'cannot be reversed', 'device'))


# ---------------- 10. password hashing (Art 32) ----------------

def test_new_password_hashes_use_the_owasp_pbkdf2_sha256_work_factor(db):
    # OWASP Password Storage Cheat Sheet, fetched 26 Sep 2026: "PBKDF2-HMAC-SHA256: 600,000 iterations (recommended)"
    auth.create_user(ALICE, 'long-initial-password')
    with db.connect() as c:
        assert c.execute('SELECT iterations FROM users').fetchone()['iterations'] == 600_000
    auth.set_password(ALICE, 'another-long-password')
    with db.connect() as c:
        assert c.execute('SELECT iterations FROM users').fetchone()['iterations'] == 600_000


def test_old_hash_still_signs_in_and_is_upgraded_without_signing_anyone_out(db):
    salt = '02' * 16
    with db.connect() as c:
        c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  ('old-owner', ALICE, salt, auth._hash('old-password-1', salt, 200_000), 200_000, 'member', time.time()))
    cookie, _ = auth.issue(ALICE)
    assert not auth.verify(ALICE, 'wrong-password')
    with db.connect() as c:
        assert c.execute('SELECT iterations FROM users').fetchone()['iterations'] == 200_000  # a failed attempt changes nothing
    assert auth.verify(ALICE, 'old-password-1')
    with db.connect() as c:
        row = c.execute('SELECT salt,iterations FROM users').fetchone()
    assert row['iterations'] == 600_000 and row['salt'] != salt
    assert auth.verify(ALICE, 'old-password-1') and auth.check(cookie) == ALICE
