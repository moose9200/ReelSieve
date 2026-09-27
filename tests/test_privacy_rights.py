"""Data-protection rights and minimisation (UK GDPR, EU GDPR, India DPDP) against real isolated PostgreSQL.
Google is the synthetic fake; nothing leaves the machine."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import quote

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


# ---------------- 3. export and 4. erasure ----------------

def seed(db, email, marker, drive=True):
    """A row in every table that holds an owner's data. `marker` makes this owner's values recognisable."""
    from psycopg.types.json import Jsonb
    from app import plans, store
    owner, now, job = db.user_id(email), time.time(), marker[:4] + 'abcdef0123'
    store.set_plan(email, 'starter', 3)
    plans.reserve(email, 'https://www.airbnb.co.uk/rooms/' + str(len(marker)), job + '-usage')  # job ids are random, not personal
    billing.settle(billing.create_order(email, 'starter', note=marker + ' Ltd, VAT XX1')['ref'])
    billing.create_order(email, 'commercial', note=marker + ' pending')
    billing.cancel(billing.create_order(email, 'commercial', note=marker + ' cancelled')['ref'])
    store.add_outreach(email, 'cohost', marker + '-host', 'https://www.airbnb.co.uk/users/show/9', 'Leeds', marker + ' message')
    store.suppress({'airbnb_profile': f'https://www.airbnb.co.uk/users/show/{100 + len(marker)}'}, email)  # a "Do not contact" mark
    store.set_b2b_sender(email, marker + ' Sender', marker + ' Lets', marker + '@sender.test')           # business email sender
    store.admin_event('plan', None, email, plan='starter', credits=3)
    store.note_signin(email, '198.51.100.' + str(len(marker)))
    with db.connect() as c:
        c.execute("INSERT INTO jobs(id,owner_id,idempotency_key,request_hash,url,params,status,log,meta,drive_generation,created,updated,"
                  "finished_at) VALUES(%s,%s,%s,'h','https://www.airbnb.co.uk/rooms/7',%s,'done',%s,%s,1,%s,%s,%s)",
                  (job, owner, marker, Jsonb({'message': marker + ' template'}), Jsonb([marker + ' log line']),
                   Jsonb({'listing': {'host': marker + '-hostname', 'title': 'Flat'}, 'message': marker + ' hello'}), now, now, now))
        if drive:
            c.execute("INSERT INTO drive_connections(owner_id,generation,status,credentials,google_sub,google_email,scope,folder_id,"
                      "connected_at,updated) VALUES(%s,1,'connected',%s,%s,%s,'drive.file','folder-1',%s,%s)",
                      (owner, marker + '-secret-credentials', marker + '-sub', marker + '@gmail.test', now, now))
        c.execute("INSERT INTO drive_uploads(owner_id,job_id,variant,file_id,generation,status,name,created) "
                  "VALUES(%s,%s,'primary',%s,1,'confirmed','https://www.airbnb.co.uk/rooms/7.mp4',%s)", (owner, job, marker + '-file', now))
        c.execute("INSERT INTO drive_oauth_states(state_hash,owner_id,session_hash,redirect_uri,created,expires_at) "
                  "VALUES(%s,%s,%s,'https://app.test/callback',%s,%s)", (marker + '-state', owner, marker + '-session', now, now + 600))


def owned_tables(db):
    with db.connect() as c:
        return {r['table_name'] for r in c.execute(
            "SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema=current_schema() "
            "AND column_name IN ('owner_id','target_id','actor_id')").fetchall()}


def dump(db):
    with db.connect() as c:
        tables = [r['table_name'] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()").fetchall()]
        return {t: str(c.execute(f'SELECT * FROM {t}').fetchall()) for t in tables}


def test_export_has_every_table_and_nothing_of_anyone_else(web, db):
    seed(db, ALICE, 'alicemark')
    seed(db, BOB, 'bobmark')
    r = web['alice'].get('/api/account/export')
    assert r.status_code == 200 and r.headers['content-type'].startswith('application/json')
    assert r.headers['content-disposition'].startswith('attachment') and 'no-store' in r.headers['cache-control']
    data, text = r.json(), r.text
    tables = owned_tables(db) | {'users'}
    assert tables <= set(data) and all(data[t] for t in tables), sorted(t for t in tables if not data.get(t))
    assert 'bobmark' not in text and BOB not in text and db.user_id(BOB) not in text
    assert 'alicemark@gmail.test' in text and 'alicemark-hostname' in text and 'alicemark log line' in text
    for secret in ('alicemark-secret-credentials', 'alicemark-state', 'alicemark-session'):
        assert secret not in text
    assert data['users'][0]['email'] == ALICE and not {'hash', 'salt', 'iterations'} & set(data['users'][0])
    assert 'credentials' not in data['drive_connections'][0] and data['privacy_notice'].endswith('/privacy')
    assert web['anon'].get('/api/account/export').status_code == 401


def test_erase_removes_or_anonymises_every_table(web, owners, google, db):
    from fakes import connect
    from app import admin
    connect(owners, google)  # Alice's real (encrypted, revocable) Drive grant
    seed(db, ALICE, 'alicemark', drive=False)
    seed(db, BOB, 'bobmark')
    owner, before = db.user_id(ALICE), dump(db)
    with db.connect() as c:
        paid = c.execute("SELECT ref FROM orders WHERE owner_id=%s AND status='paid'", (owner,)).fetchone()['ref']
    assert admin.erase(ALICE, ADMIN) is None
    assert [r.url.path for r in google.calls][-1] == '/revoke'
    after = dump(db)
    everything = ''.join(after.values())
    assert 'alicemark' not in everything and 'google-alice' not in everything
    assert everything.count(ALICE) == 1  # the billing email snapshot on the paid order
    with db.connect() as c:
        u = c.execute('SELECT * FROM users WHERE id=%s', (owner,)).fetchone()
        assert u['email'] == f'deleted-{owner}@erased.invalid' and not u['active'] and u['erased_at'] and u['role'] == 'member'
        assert c.execute('SELECT ip_hash,fp_hash,note,b2b_sender FROM accounts WHERE owner_id=%s', (owner,)).fetchone() == \
            {'ip_hash': None, 'fp_hash': None, 'note': None, 'b2b_sender': None}
        marks = c.execute('SELECT owner_id FROM outreach_suppressions').fetchall()  # objections stay; Alice's link goes
        assert len(marks) == 2 and [m['owner_id'] for m in marks].count(None) == 1 and owner not in str(marks)
        usage = c.execute('SELECT listing_key,fp_hash FROM usage WHERE owner_id=%s', (owner,)).fetchall()
        assert usage and all(r['listing_key'] is None and r['fp_hash'] is None for r in usage)
        for table in ('jobs', 'outreach', 'drive_uploads', 'drive_oauth_states', 'signin_networks'):
            assert c.execute(f'SELECT count(*) AS n FROM {table} WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0, table
        d = c.execute('SELECT * FROM drive_connections WHERE owner_id=%s', (owner,)).fetchone()
        assert d['status'] == 'disconnected' and not any(d[k] for k in ('credentials', 'google_sub', 'google_email', 'folder_id', 'connected_at'))
        orders = c.execute('SELECT * FROM orders WHERE owner_id=%s', (owner,)).fetchall()
        assert [(o['ref'], o['status'], o['billing_email'], o['note'], o['meta'], o['pay_link']) for o in orders] == \
            [(paid, 'paid', ALICE, None, None, None)]
        assert orders[0]['amount_usd'] == 100 and orders[0]['paid_at'] and orders[0]['plan'] == 'starter'
        ev = c.execute("SELECT actor_id,detail FROM admin_events WHERE target_id=%s AND action='erase'", (owner,)).fetchone()
        assert ev == {'actor_id': db.user_id(ADMIN), 'detail': {'via': 'admin'}}
    for table in ('jobs', 'outreach', 'orders', 'drive_uploads', 'drive_connections', 'drive_oauth_states'):
        assert before[table].count('bobmark') == after[table].count('bobmark'), table
    assert not auth.verify(ALICE, 'synthetic-password')
    auth.create_user(ALICE, 'a-fresh-password')
    assert db.user_id(ALICE) != owner and auth.verify(ALICE, 'a-fresh-password')
    with pytest.raises(ValueError, match='No such user'):
        admin.erase(f'deleted-{owner}@erased.invalid')


def test_self_service_deletion_needs_the_password_and_DELETE(web, db):
    alice = web['alice']
    for body in ({'password': 'wrong-password', 'confirm': 'DELETE'}, {'password': 'synthetic-password', 'confirm': 'delete'}):
        assert post(alice, '/api/account/delete', body).status_code == 400
    assert auth.verify(ALICE, 'synthetic-password')
    r = post(alice, '/api/account/delete', {'password': 'synthetic-password', 'confirm': 'DELETE'})
    assert r.status_code == 200 and r.json()['redirect'] == '/login?notice=deleted'
    assert alice.get('/api/account').status_code == 401
    anon = web['anon']
    page = anon.get('/login?notice=deleted').text
    assert 'Your account has been deleted' in page
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    assert anon.post('/login', data={'csrf': token, 'user': ALICE, 'password': 'synthetic-password'}).status_code == 401
    r = anon.post('/signup', data={'csrf': token, 'user': ALICE, 'password': 'a-fresh-password'}, follow_redirects=False)
    assert r.status_code == 303 and auth.verify(ALICE, 'a-fresh-password')
    with db.connect() as c:
        assert c.execute("SELECT detail FROM admin_events WHERE action='erase'").fetchone()['detail'] == {'via': 'self'}


def test_the_last_admin_cannot_erase_themselves(web, db):
    from app import admin
    r = post(web['admin'], '/api/account/delete', {'password': 'operator-password', 'confirm': 'DELETE'})
    assert r.status_code == 400 and 'admin' in r.json()['detail']
    with pytest.raises(ValueError, match='Keep at least one admin'):
        admin.erase(ADMIN)
    auth.create_user('second@example.test', 'second-password', 'admin')
    assert post(web['admin'], '/api/account/delete', {'password': 'operator-password', 'confirm': 'DELETE'}).status_code == 200


def test_admin_erase_is_separate_from_remove_and_admin_only(web, db):
    assert post(web['alice'], '/api/users/erase', {'user': BOB}).status_code == 403
    assert post(web['admin'], '/api/users/delete', {'user': ALICE}).status_code == 200
    assert post(web['admin'], '/api/users/erase', {'user': BOB}).status_code == 200
    with db.connect() as c:
        rows = {r['email']: r for r in c.execute('SELECT email,active,erased_at FROM users').fetchall()}
    assert ALICE in rows and rows[ALICE]['erased_at'] is None and not rows[ALICE]['active']  # removed, not erased
    assert BOB not in rows
    assert sum(1 for e, r in rows.items() if e.endswith('@erased.invalid') and r['erased_at']) == 1


def test_an_order_reported_paid_before_erasure_keeps_the_payer_email_when_settled_later(owners, db):
    from app import admin, retention
    reported = billing.create_order(ALICE, 'starter')['ref']
    billing.mark_reported(reported)
    stale = billing.create_order(ALICE, 'commercial')['ref']
    billing.mark_reported(stale)
    admin.erase(ALICE)
    assert billing.settle(reported)['billing_email'] == ALICE  # the tax record names the payer, not the placeholder
    with db.connect() as c:
        c.execute('UPDATE users SET erased_at=erased_at-%s', (91 * DAY,))
    retention.run()
    with db.connect() as c:
        rows = {r['ref']: r for r in c.execute('SELECT ref,status,billing_email FROM orders').fetchall()}
    assert set(rows) == {reported}  # a claimed payment that never cleared goes 90 days after erasure


def test_two_workers_erasing_the_same_account_erase_it_once(owners, db):
    from concurrent.futures import ThreadPoolExecutor
    from app import admin
    admin.deactivate(ALICE)

    def erase(_):
        try:
            return admin.erase(ALICE, via='retention') or 'ok'
        except ValueError:
            return 'lost the race'
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(erase, range(4)))
    with db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM admin_events WHERE action='erase'").fetchone()['n'] == 1
        assert c.execute('SELECT count(*) AS n FROM users WHERE erased_at IS NOT NULL').fetchone()['n'] == 1


def test_account_page_offers_download_and_delete_and_settings_offers_erase(web):
    page = web['alice'].get('/account').text
    assert 'href="/api/account/export"' in page and 'Download my data' in page
    assert all(f'id="{i}"' in page for i in ('del-password', 'del-confirm', 'del-btn')) and 'Delete my account' in page
    assert 'paid orders for 8 years' in page and 'subject=Delete' not in page  # no more "email us and we delete it within a day"
    js = web['alice'].get('/static/app.js').text
    assert '/api/account/delete' in js and '/api/users/erase' in js
    assert 'Erase deletes' in web['admin'].get('/settings').text


def test_operator_console_erases_active_and_deactivated_accounts(owners, db):
    from app import admin
    assert admin.main(['deactivate', ALICE]) == 0
    assert admin.main(['erase', ALICE]) == 0 and admin.main(['erase', BOB]) == 0
    assert admin.main(['erase', 'nobody@example.test']) == 1
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM users WHERE erased_at IS NOT NULL').fetchone()['n'] == 2


# ---------------- 5. retention enforced by code ----------------

DAY = 86400


def _job(c, job_id, owner, finished, meta, params=None, status='done'):
    from psycopg.types.json import Jsonb
    c.execute("INSERT INTO jobs(id,owner_id,idempotency_key,request_hash,url,params,status,meta,drive_generation,created,updated,"
              "finished_at) VALUES(%s,%s,%s,'h','https://www.airbnb.co.uk/rooms/7',%s,%s,%s,1,%s,%s,%s)",
              (job_id, owner, job_id, Jsonb(params or {}), status, Jsonb(meta), (finished or time.time()) - 60, time.time(), finished))


def test_retention_removes_what_is_due_and_keeps_what_is_not(owners, db):
    from app import admin, retention, store
    now, alice = time.time(), db.user_id(ALICE)
    for who in ('gone@example.test', 'kept@example.test'):
        auth.create_user(who, 'long-password-1')
        admin.deactivate(who)
    third_party = {'listing': {'host': 'Hostname', 'title': 'Flat', 'city': 'Leeds'}, 'message': 'Hi Hostname',
                   'review_used': {'stars': 5, 'text': 'Lovely'}, 'duration': 30}
    rid_old = store.add_outreach(ALICE, 'cohost', 'Old', 'https://a.test', 'Leeds', 'm')
    rid_touched = store.add_outreach(ALICE, 'cohost', 'Touched', 'https://a.test', 'Leeds', 'm')
    rid_sent = store.add_outreach(ALICE, 'cohost', 'Sent', 'https://a.test', 'Leeds', 'm')
    with db.connect() as c:
        c.execute("UPDATE users SET deactivated_at=%s WHERE email='gone@example.test'", (now - 31 * DAY,))
        c.execute("UPDATE users SET deactivated_at=%s WHERE email='kept@example.test'", (now - 29 * DAY,))
        c.execute("INSERT INTO login_failures(ip_hash,ts) VALUES('old',%s),('new',%s)", (now - 25 * 3600, now - 3600))
        c.execute("INSERT INTO drive_oauth_states(state_hash,owner_id,session_hash,redirect_uri,created,expires_at) VALUES"
                  "('expired',%s,'s','r',%s,%s),('live',%s,'s','r',%s,%s)", (alice, now - 700, now - 100, alice, now, now + 500))
        _job(c, 'aaaaaa000001', alice, now - 31 * DAY, third_party, {'message': 'Hi {host_name}'})
        _job(c, 'aaaaaa000002', alice, now - 29 * DAY, third_party, {'message': 'Hi {host_name}'})
        _job(c, 'aaaaaa000003', alice, now - 31 * DAY, {'legacy': True, 'listing': {'host': 'H'}, 'message': 'Hi H'}, {'message': 'Hi H'})
        _job(c, 'aaaaaa000004', alice, None, third_party, status='running')
        c.execute('UPDATE outreach SET ts=%s,updated=NULL', (now - 400 * DAY,))
        c.execute('UPDATE outreach SET sent_at=%s WHERE id=%s', (now - 10 * DAY, rid_sent))
        for ref, status, paid_at in (('RS-OLD', 'paid', now - (8 * 365.25 + 1) * DAY), ('RS-NEW', 'paid', now - 7 * 365 * DAY),
                                     ('RS-PENDING', 'pending', None)):
            c.execute("INSERT INTO orders(owner_id,ref,ts,plan,amount_usd,status,paid_at,billing_email) VALUES(%s,%s,%s,'starter',100,%s,%s,%s)",
                      (alice, ref, paid_at or now - 9 * 365 * DAY, status, paid_at, ALICE if paid_at else None))
        c.execute("INSERT INTO admin_events(ts,action,detail) VALUES(%s,'plan','{}'),(%s,'plan','{}')",
                  (now - (2 * 365.25 + 1) * DAY, now - 365 * DAY))
        c.execute("INSERT INTO legacy_archives(name,created,files,size,sha256,data) VALUES('legacy-volume',%s,1,1,'x','x')", (now - 91 * DAY,))
    store.outreach_set(rid_touched, ALICE, status='replied')  # a change restarts the 12 months
    out = retention.run(now)
    with db.connect() as c:
        q = lambda s, *a: [tuple(r.values()) for r in c.execute(s, a).fetchall()]  # noqa: E731
        assert q('SELECT ip_hash FROM login_failures') == [('new',)]
        assert q('SELECT state_hash FROM drive_oauth_states') == [('live',)]
        jobs = {r['id']: r for r in c.execute('SELECT id,meta,params FROM jobs').fetchall()}
        assert jobs['aaaaaa000001']['meta'] == {'listing': {'title': 'Flat', 'city': 'Leeds'}, 'duration': 30}
        assert jobs['aaaaaa000001']['params'] == {'message': 'Hi {host_name}'}  # the customer's own template stays
        assert jobs['aaaaaa000002']['meta'] == third_party and jobs['aaaaaa000004']['meta'] == third_party
        assert jobs['aaaaaa000003']['meta'] == {'legacy': True, 'listing': {}} and jobs['aaaaaa000003']['params'] == {}
        assert sorted(q('SELECT id FROM outreach')) == sorted([(rid_touched,), (rid_sent,)]) and rid_old
        assert sorted(q('SELECT ref FROM orders')) == [('RS-NEW',), ('RS-PENDING',)]
        assert len(q("SELECT id FROM admin_events WHERE action='plan'")) == 1
        assert q('SELECT name FROM legacy_archives') == []
        erased = {r['deactivated_at'] < now - 30 * DAY: r['email'] for r in c.execute(
            "SELECT email,deactivated_at FROM users WHERE NOT active AND role='member'").fetchall()}
        assert erased[True].endswith('@erased.invalid') and erased[False] == 'kept@example.test'
        assert q("SELECT detail FROM admin_events WHERE action='erase'") == [({'via': 'retention'},)]
    assert out['erased'] == 1 and out['login_failures'] == 1
    assert retention.run(now)['erased'] == 0  # idempotent


def test_legacy_archive_is_kept_until_its_keep_days_pass(db, monkeypatch):
    from app import retention
    with db.connect() as c:
        c.execute("INSERT INTO legacy_archives(name,created,files,size,sha256,data) VALUES('legacy-volume',%s,1,1,'x','x')",
                  (time.time() - 89 * DAY,))
    retention.run()
    monkeypatch.setenv('LEGACY_ARCHIVE_KEEP_DAYS', '30')
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM legacy_archives').fetchone()['n'] == 1
    retention.run()
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM legacy_archives').fetchone()['n'] == 0


def test_archive_purge_command_prints_counts_then_deletes(db, monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from cryptography.fernet import Fernet
    from app import archive_legacy
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    (tmp_path / 'auth.json').write_text('{}')
    archive_legacy.archive(tmp_path)
    r = subprocess.run([sys.executable, '-m', 'app.archive_legacy', '--purge'], capture_output=True, text=True, env=dict(os.environ),
                       cwd=Path(__file__).resolve().parents[1], timeout=60)
    assert r.returncode == 0, r.stderr
    printed = [json.loads(line) for line in r.stdout.splitlines()]
    assert printed[0]['files'] == 1 and printed[-1] == {'status': 'purged'}
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM legacy_archives').fetchone()['n'] == 0
    assert subprocess.run([sys.executable, '-m', 'app.archive_legacy', '--purge'], capture_output=True, text=True, env=dict(os.environ),
                          cwd=Path(__file__).resolve().parents[1], timeout=60).returncode != 0


def test_worker_runs_retention_hourly(db, monkeypatch, tmp_path):
    from app import retention, worker
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(retention, 'run', lambda: calls.append(1) or {})
    monkeypatch.setattr(worker, '_last_purge', [0.0])
    worker.run_once('w')
    worker.run_once('w')
    assert calls == [1]


# ---------------- 9. listing photos through our own server ----------------

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64
PHOTO = 'https://a0.muscache.com/im/pictures/hosting/Hosting-1/original/a.jpeg'


@pytest.fixture
def cdn(monkeypatch):
    """Synthetic Airbnb CDN (and one foreign host) behind MockTransport; DNS answers a public address."""
    import socket
    import httpx
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.path.endswith('redirect.jpg'):
            return httpx.Response(302, headers={'Location': 'https://evil.example.org/x.jpg'})
        if req.url.path.endswith('page.html'):
            return httpx.Response(200, content=b'<html>not an image</html>', headers={'content-type': 'image/jpeg'})
        if req.url.path.endswith('huge.jpg'):
            return httpx.Response(200, content=JPEG + b'0' * (9 * 1024 * 1024))
        return httpx.Response(200, content=JPEG, headers={'content-type': 'image/jpeg'})
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
    return calls


def test_image_proxy_serves_only_airbnb_cdn_images(web, cdn):
    r = web['alice'].get('/img', params={'u': PHOTO + '?im_w=720'})
    assert r.status_code == 200 and r.content == JPEG and r.headers['content-type'] == 'image/jpeg'
    assert r.headers['cache-control'] == 'private, max-age=86400' and r.headers['x-content-type-options'] == 'nosniff'
    assert cdn == [PHOTO + '?im_w=720']
    for bad in ('http://a0.muscache.com/im/a.jpg', 'https://evil.example.org/a.jpg', 'https://a0.muscache.com.evil.example.org/a.jpg',
                'https://a0.muscache.com@evil.example.org/a.jpg', 'https://a0.muscache.com:8443/a.jpg',
                'https://a0.muscache.com/im/redirect.jpg', 'https://a0.muscache.com/im/page.html',
                'https://a0.muscache.com/im/huge.jpg', '', 'file:///etc/passwd'):
        assert web['alice'].get('/img', params={'u': bad}).status_code == 400, bad
    assert not any('evil' in u for u in cdn)  # the redirect target was refused before any request went there
    assert web['anon'].get('/img', params={'u': PHOTO}, follow_redirects=False).status_code == 303


def test_pages_load_listing_photos_through_the_proxy(web, db):
    with db.connect() as c:
        _job(c, 'bbbbbb000001', db.user_id(ALICE), time.time(), {'listing': {'title': 'Flat', 'photo': PHOTO}})
    proxied = '/img?u=' + quote(PHOTO + '?im_w=1200', safe='')
    for path in ('/jobs/bbbbbb000001', '/reels'):
        page = web['alice'].get(path).text
        assert proxied.replace('&', '&amp;') in page or proxied in page, path
        assert 'muscache.com/im' not in page.replace(quote('muscache.com/im', safe=''), ''), path
    assert web['alice'].get('/api/jobs/bbbbbb000001').json()['poster'] == proxied
    js = web['alice'].get('/static/app.js').text
    assert 'imgSrc(it.photo' in js and 'imgSrc(it.avatar' in js and 'src="\' + esc(it.photo)' not in js


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


# ---------------- 8. admin accountability ----------------

def test_admin_actions_leave_an_accountability_trail(web, db):
    from app import store
    ref = post(web['alice'], '/api/billing/request', {'plan': 'starter'}).json()['order']['ref']
    ref2 = post(web['alice'], '/api/billing/request', {'plan': 'commercial'}).json()['order']['ref']
    admin = web['admin']
    for path, body in (('/api/users/plan', {'user': ALICE, 'plan': 'starter', 'credits': 5}),
                       ('/api/users/password', {'user': ALICE, 'password': 'admin-set-password'}),
                       ('/api/billing/link', {'ref': ref, 'url': 'https://pay.provider.test/one'}),
                       ('/api/billing/settle', {'ref': ref}), ('/api/billing/cancel', {'ref': ref2}),
                       ('/api/users/delete', {'user': BOB})):
        assert post(admin, path, body).status_code == 200, path
    with db.connect() as c:
        rows = c.execute('SELECT e.action,a.email AS actor,t.email AS target,e.detail FROM admin_events e '
                         'JOIN users a ON a.id=e.actor_id JOIN users t ON t.id=e.target_id ORDER BY e.id').fetchall()
        notes = str(c.execute('SELECT note FROM accounts UNION ALL SELECT note FROM orders').fetchall())
    assert [(r['action'], r['actor'], r['target']) for r in rows] == [
        ('plan', ADMIN, ALICE), ('password_reset', ADMIN, ALICE), ('order_link', ADMIN, ALICE),
        ('order_settle', ADMIN, ALICE), ('order_cancel', ADMIN, ALICE), ('deactivate', ADMIN, BOB)]
    assert rows[0]['detail'] == {'plan': 'starter', 'credits': 5} and rows[3]['detail'] == {'ref': ref}
    assert 'admin-set-password' not in str(rows) and 'pay.provider.test' not in str(rows)
    assert ADMIN not in notes  # who did it lives in the trail, not in the customer's records
    page = admin.get('/settings').text
    assert 'Admin activity' in page and 'Password reset' in page and page.count('data-event') == 6
    assert web['alice'].get('/settings', follow_redirects=False).status_code == 303
    for _ in range(55):
        store.admin_event('plan', ADMIN, ALICE, plan='free', credits=0)
    assert admin.get('/settings').text.count('data-event') == 50


# ---------------- 12. public privacy request and complaint form ----------------

def _request(client, **fields):
    page = client.get('/privacy/request').text
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    data = {'csrf': token, 'name': 'Pat Host', 'email': 'pat@example.org', 'type': 'objection', 'details': 'Please stop messaging me.',
            'airbnb_profile': '', **fields}
    return client.post('/privacy/request', data=data)


def test_privacy_request_form_is_public_acknowledged_and_handled_by_admins(web, db):
    from app import store
    page = web['anon'].get('/privacy/request').text
    for label in ('Access', 'Erasure', 'Rectification', 'Objection to outreach', 'Complaint', 'Other', 'Airbnb profile'):
        assert label in page, label
    assert 'href="/privacy/request"' in web['anon'].get('/privacy').text  # linked from the footer
    r = _request(web['anon'], airbnb_profile=' https://www.airbnb.co.uk/users/show/777 ')
    assert r.status_code == 200
    ref = re.search(r'PR-\d{6}-[0-9A-F]{6}', r.text).group(0)
    assert 'Received' in r.text and time.strftime('%d %b %Y', time.gmtime()) in r.text
    with db.connect() as c:
        row = c.execute('SELECT * FROM privacy_requests').fetchone()
    assert (row['ref'], row['type'], row['email'], row['name'], row['airbnb_profile_id'], row['status']) == \
        (ref, 'objection', 'pat@example.org', 'Pat Host', '777', 'open')
    assert 28 * DAY <= row['due_at'] - row['ts'] <= 31 * DAY and row['handled_at'] is None
    assert store.unsuppressed([{'name': 'Pat', 'airbnb_profile': 'https://www.airbnb.co.uk/users/show/777'}]) == []  # objection honoured
    settings = web['admin'].get('/settings').text
    assert ref in settings and 'Due' in settings and 'pat@example.org' in settings
    assert post(web['alice'], '/api/privacy-requests/handled', {'ref': ref}).status_code == 403
    assert post(web['admin'], '/api/privacy-requests/handled', {'ref': ref}).status_code == 200
    with db.connect() as c:
        row = c.execute('SELECT status,handled_at FROM privacy_requests').fetchone()
    settings = web['admin'].get('/settings').text
    assert row['status'] == 'handled' and row['handled_at']
    card = settings.split('id="requests-card"', 1)[1].split('</section>', 1)[0]
    assert 'id="req-list"' not in card and 'Privacy request handled' in settings  # no longer open
    assert ref in card.split('id="req-marks"', 1)[1]  # its do-not-contact entry stays visible and undoable
    for bad in ({'type': 'lawsuit'}, {'email': 'not-an-email'}, {'airbnb_profile': 'https://evil.example.org/users/show/1'}, {'details': ''}):
        assert _request(client_for(), **bad).status_code == 400, bad


def test_due_date_is_one_calendar_month_after_receipt():
    import calendar
    from app import store
    jan31 = calendar.timegm((2026, 1, 31, 10, 0, 0))
    assert time.gmtime(store.one_month_after(jan31))[:3] == (2026, 2, 28)
    assert time.gmtime(store.one_month_after(calendar.timegm((2026, 12, 15, 9, 0, 0))))[:3] == (2027, 1, 15)


def test_privacy_requests_are_rate_limited_per_network(web):
    for _ in range(5):
        assert _request(web['anon'], type='access', airbnb_profile='').status_code == 200
    assert _request(web['anon'], type='access').status_code == 429


def test_own_privacy_requests_are_in_the_export_and_leave_two_years_after_handling(web, db):
    from app import retention
    _request(client_for(), email=ALICE, type='access', details='Copy of my data please')
    _request(client_for(), email=BOB, type='access', details='Bob asks too')
    data = web['alice'].get('/api/account/export').json()
    assert [r['details'] for r in data['privacy_requests']] == ['Copy of my data please'] and BOB not in json.dumps(data)
    with db.connect() as c:
        c.execute("UPDATE privacy_requests SET status='handled',handled_at=%s WHERE email=%s", (time.time() - (2 * 365.25 + 1) * DAY, ALICE))
        c.execute("UPDATE privacy_requests SET status='handled',handled_at=%s WHERE email=%s", (time.time() - 300 * DAY, BOB))
    retention.run()
    with db.connect() as c:
        assert [r['email'] for r in c.execute('SELECT email FROM privacy_requests').fetchall()] == [BOB]


# ---------------- 13. outreach objection and suppression ----------------

PROFILE = 'https://www.airbnb.co.uk/users/show/4242'
PROSPECTS = [{'id': str(n), 'name': name, 'url': f'https://www.airbnb.co.uk/contact_host/{n}/send_message',
              'listing_url': f'https://www.airbnb.co.uk/rooms/{n}', 'profile_url': profile, 'city': 'Leeds', 'listing_title': 'Flat'}
             for n, name, profile in ((111, 'Jo', PROFILE), (222, 'Sam', None), (333, 'Kim', None))]


def test_do_not_contact_suppresses_the_prospect_for_every_user(web, db, monkeypatch):
    from app import cohost, store
    monkeypatch.setattr(cohost, 'discover', lambda city, limit=12: {'city': city, 'items': [dict(p) for p in PROSPECTS],
                                                                    'source': 'operators', 'note': ''})
    queued = [{'id': p['id'], 'name': p['name'], 'url': p['url'], 'city': 'Leeds', 'message': 'Hi', 'airbnb_profile': p['profile_url'] or '',
               'listing_url': p['listing_url']} for p in PROSPECTS[:2]]
    ids = post(web['alice'], '/api/outreach/queue', {'channel': 'cohost', 'items': queued}).json()['ids']
    assert post(web['bob'], '/api/outreach/suppress', {'id': ids[0]}).status_code == 404  # only the row's owner
    for rid in ids:
        r = post(web['alice'], '/api/outreach/suppress', {'id': rid})
        assert r.status_code == 200 and 'stats' in r.json()
    assert store.outreach_rows(ALICE) == []
    assert [p['name'] for p in web['bob'].get('/api/outreach/cohosts?city=Leeds').json()['items']] == ['Kim']
    assert [p['name'] for p in web['bob'].get('/api/outreach/linkedin?city=Leeds').json()['items']] == ['Kim']
    search = 'https://www.linkedin.com/search/results/people/?keywords=Jo%20Leeds'
    post(web['bob'], '/api/outreach/queue', {'channel': 'linkedin', 'items': [
        {'name': 'Jo', 'url': search, 'city': 'Leeds', 'airbnb_profile': PROFILE},
        {'name': 'Sam', 'url': search, 'city': 'Leeds', 'listing_url': 'https://www.airbnb.co.uk/rooms/222'},
        {'name': 'Kim', 'url': search, 'city': 'Leeds', 'listing_url': 'https://www.airbnb.co.uk/rooms/333'}]})
    assert [r['name'] for r in store.outreach_rows(BOB)] == ['Kim']
    with db.connect() as c:
        rows = c.execute('SELECT * FROM outreach_suppressions').fetchall()
    # nothing about the prospect but a keyed hash; owner_id is the account that marked it (exported, erased, 90 days)
    assert rows and all(set(r) == {'key', 'ts', 'owner_id', 'owner_hash', 'request_ref'} for r in rows)
    assert {r['owner_id'] for r in rows} == {db.user_id(ALICE)} and 'Jo' not in str(rows) and 'Sam' not in str(rows)
    assert not any(x in str(rows) for x in ('4242', 'Sam', 'Jo', '222'))
    page = web['alice'].get('/outreach').text
    assert 'Do not contact' in web['alice'].get('/static/app.js').text or 'Do not contact' in page


# ---------------- 11, 14, 15. transparency at the point of collection and use ----------------

def test_signup_links_terms_and_privacy_at_collection(web):
    page = web['anon'].get('/signup').text
    assert ('By creating an account you agree to the <a href="/terms">Terms</a> and confirm you have read the '
            '<a href="/privacy">Privacy notice</a>.') in page


def test_ai_motion_checkbox_says_photos_go_to_higgsfield(web):
    page = web['alice'].get('/app').text
    block = page.split('id="ai_motion"', 1)[1].split('</div>', 1)[0]
    assert 'Higgsfield' in block and 'listing photos' in block
    assert 'name="ai_motion" checked' not in page  # still opt-in


def test_outreach_copy_is_honest_about_where_hosts_come_from_and_who_writes(web, monkeypatch):
    monkeypatch.delenv('COHOST_MESSAGE', raising=False)
    page = web['alice'].get('/outreach').text
    assert 'list themselves' not in page and 'best-reviewed listings' in page
    assert 'Hemant' not in page  # the default message no longer speaks as the founder for every customer


def test_outreach_page_carries_a_plain_pecr_notice(web):
    page = web['alice'].get('/outreach').text
    notice = page.split('id="outreach-rules"', 1)[1].split('</section>', 1)[0]
    for phrase in ('PECR', 'electronic mail', 'consent', 'sole traders', 'opt-out', "Airbnb's Terms", 'responsible'):
        assert phrase in notice.replace('&#39;', "'"), phrase


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
