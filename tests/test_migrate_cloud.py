"""Legacy volume → PostgreSQL, using synthetic files in the exact legacy formats."""
import hashlib
import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from app import auth, gdrive, jobs, migrate_cloud, plans, store

LEGACY_SQL = """
CREATE TABLE usage(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, plan TEXT, kind TEXT,
  listing_key TEXT, job_id TEXT, ip_hash TEXT, fp_hash TEXT, credits INTEGER DEFAULT 1);
CREATE TABLE accounts(user TEXT PRIMARY KEY, plan TEXT DEFAULT 'free', credits INTEGER DEFAULT 0,
  created REAL, ip_hash TEXT, fp_hash TEXT, note TEXT, blocked INTEGER DEFAULT 0);
CREATE TABLE outreach(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, channel TEXT, name TEXT, url TEXT,
  city TEXT, message TEXT, status TEXT DEFAULT 'queued', note TEXT, sent_at REAL, meta TEXT);
CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, ts REAL, user TEXT, plan TEXT,
  amount_usd REAL, provider TEXT, status TEXT DEFAULT 'pending', paid_at REAL, note TEXT, meta TEXT, pay_link TEXT);
"""


def legacy_hash(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()


@pytest.fixture
def legacy(tmp_path, db, monkeypatch):
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    root = tmp_path / 'data'
    root.mkdir()
    users = {'ops@example.test': ('aa' * 16, 'admin'), 'pat@example.test': ('bb' * 16, 'member')}
    (root / 'auth.json').write_text(json.dumps({'secret': 'legacy-secret', 'users': {
        u: {'salt': s, 'hash': legacy_hash('legacy-password-' + u[:3], s), 'role': r, 'created': 1_700_000_000.0}
        for u, (s, r) in users.items()}}))
    with sqlite3.connect(root / 'reelsieve.db') as c:
        c.executescript(LEGACY_SQL)
        c.execute("INSERT INTO accounts VALUES('pat@example.test','starter',2,1700000000,'iphash','fphash',NULL,0)")
        c.execute("INSERT INTO accounts VALUES('ops@example.test','enterprise',0,1700000000,NULL,NULL,NULL,0)")
        c.execute("INSERT INTO usage(ts,user,plan,kind,listing_key,job_id,credits) VALUES(1700000100,'pat@example.test','starter','video','airbnb:1','aaaaaaaaaa',1)")
        c.execute("INSERT INTO outreach(ts,user,channel,name,url,city,message,status) VALUES(1700000200,'pat@example.test','cohost','Host','https://x.test','Leeds','Hi','sent')")
        c.execute("INSERT INTO orders(ref,ts,user,plan,amount_usd,provider,status,paid_at) VALUES('RS-260101-ABCDEF',1700000300,'pat@example.test','starter',100,'invoice','paid',1700000400)")
    for jid, status, extra in [('aaaaaaaaaa', 'done', {'drive_id': 'drive-file-1', 'drive_link': 'https://drive.google.com/file/d/drive-file-1',
                                                       'drive_name': 'listing.mp4', 'video_url': '/media/aaaaaaaaaa/x.mp4'}),
                               ('bbbbbbbbbb', 'running', {}),
                               ('cccccccccc', 'failed', {'error': 'RuntimeError: render failed: /Users/me/secret/path boom',
                                                         'log': ['12:00 Traceback in /srv/app/pipeline.py line 3']})]:
        d = root / 'jobs' / jid
        d.mkdir(parents=True)
        (d / 'job.json').write_text(json.dumps({'id': jid, 'url': 'https://www.airbnb.co.uk/rooms/1', 'user': 'pat@example.test',
                                                'status': status, 'created': '2026-09-20 10:00', 'style': 'v2', 'plan': 'starter',
                                                'listing': {'url': 'https://www.airbnb.co.uk/rooms/1', 'title': 'Flat', 'city': 'Leeds'},
                                                'share_token': 'secret-share', **extra}))
    key = hashlib.sha256(b'pat@example.test').hexdigest()[:32]
    (root / f'google-{key}.json').write_text(json.dumps({
        'user': 'pat@example.test', 'email': 'pat@gmail.test', 'access_token': 'legacy-access-secret',
        'refresh_token': 'legacy-refresh-secret', 'expires_at': 9_999_999_999, 'scope': gdrive.SCOPES}))
    return root


def table_count(db, table):
    with db.connect() as c:
        return c.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']


def test_dry_run_counts_and_writes_nothing(legacy, db):
    out = migrate_cloud.run(legacy)
    assert out['status'] == 'dry-run'
    assert out['counts'] == {'users': 2, 'admins': 1, 'accounts': 2, 'usage': 1, 'outreach': 1, 'orders': 1, 'jobs': 3,
                             'jobs_by_status': {'done': 1, 'interrupted': 1, 'failed': 1}, 'drive_receipts': 1, 'drive_connections': 1}
    assert table_count(db, 'users') == 0
    assert 'secret' not in json.dumps(out)


def test_apply_preserves_identity_balances_and_ownership(legacy, db):
    out = migrate_cloud.run(legacy, apply=True)
    assert out['status'] == 'applied'
    # hashes are carried over as they were; since 04 Oct 2026 only the operator's still signs in with one
    assert auth.verify('ops@example.test', 'legacy-password-ops') and not auth.verify('ops@example.test', 'wrong-password')
    assert not auth.verify('pat@example.test', 'legacy-password-pat')
    assert auth.role('ops@example.test') == 'admin' and auth.role('pat@example.test') == 'member'
    assert db.user_id('pat@example.test') == migrate_cloud.owner_id('pat@example.test')
    view = plans.account_view('pat@example.test')
    assert view['plan'] == 'starter' and view['credits'] == 2 and view['used'] == 1
    assert store.outreach_rows('pat@example.test')[0]['status'] == 'sent'
    assert store.outreach_rows('ops@example.test') == []
    with db.connect() as c:
        order = c.execute('SELECT o.*,u.email FROM orders o JOIN users u ON u.id=o.owner_id').fetchone()
        usage = c.execute('SELECT debited FROM usage').fetchone()
    assert order['ref'] == 'RS-260101-ABCDEF' and order['email'] == 'pat@example.test' and order['status'] == 'paid'
    assert usage['debited'] is True
    # new rows continue after the preserved ids
    assert store.add_outreach('pat@example.test', 'cohost', 'N', 'u', 'c', 'm') == 2


def test_jobs_are_owned_sanitised_and_interrupted_never_requeued(legacy, db):
    migrate_cloud.run(legacy, apply=True)
    done = jobs.get('pat@example.test', 'aaaaaaaaaa')
    interrupted = jobs.get('pat@example.test', 'bbbbbbbbbb')
    failed = jobs.get('pat@example.test', 'cccccccccc')
    assert done['status'] == 'done' and interrupted['status'] == 'failed' and 'not retried' in interrupted['error']
    assert jobs.get('ops@example.test', 'aaaaaaaaaa') is None
    assert '/Users/' not in failed['error'] and '/srv/' not in json.dumps(failed['log'])
    assert 'secret-share' not in json.dumps(done['meta']) and 'video_url' not in done['meta']
    rec = gdrive.receipt('pat@example.test', 'aaaaaaaaaa')
    assert rec['id'] == 'drive-file-1' and rec['sharing'] == 'public'
    assert gdrive.receipt('pat@example.test', 'aaaaaaaaaa', '720p') is None
    assert jobs.claim('w', 5) is None


def test_drive_token_is_encrypted_and_bound_to_owner(legacy, db):
    migrate_cloud.run(legacy, apply=True)
    with db.connect() as c:
        raw = str(c.execute('SELECT * FROM drive_connections').fetchall())
    assert 'legacy-refresh-secret' not in raw and 'legacy-access-secret' not in raw
    assert gdrive.status('pat@example.test')['connected'] and gdrive.status('pat@example.test')['email'] == 'pat@gmail.test'
    assert gdrive.access_token('pat@example.test') == 'legacy-access-secret'
    assert not gdrive.connected('ops@example.test')


def test_rerun_is_a_no_op_and_changed_source_is_refused(legacy, db):
    migrate_cloud.run(legacy, apply=True)
    again = migrate_cloud.run(legacy, apply=True)
    assert again['status'] == 'already-applied' and table_count(db, 'users') == 2 and table_count(db, 'jobs') == 3
    (legacy / 'jobs' / 'aaaaaaaaaa' / 'job.json').write_text(json.dumps({'id': 'aaaaaaaaaa', 'user': 'pat@example.test', 'status': 'done'}))
    with pytest.raises(migrate_cloud.MigrationError, match='changed'):
        migrate_cloud.run(legacy, apply=True)


@pytest.mark.parametrize('break_it', ['unknown_job_owner', 'ownerless_job', 'unknown_usage_owner', 'token_hash_mismatch', 'shared_token'])
def test_ambiguous_ownership_halts_before_any_write(legacy, db, break_it):
    if break_it in ('unknown_job_owner', 'ownerless_job'):
        f = legacy / 'jobs' / 'bbbbbbbbbb' / 'job.json'
        j = json.loads(f.read_text())
        j['user'] = 'stranger@example.test' if break_it == 'unknown_job_owner' else None
        f.write_text(json.dumps(j))
    elif break_it == 'unknown_usage_owner':
        with sqlite3.connect(legacy / 'reelsieve.db') as c:
            c.execute("INSERT INTO usage(ts,user,plan,kind,job_id) VALUES(1,'gone@example.test','free','video','dddddddddd')")
    elif break_it == 'token_hash_mismatch':
        f = next(legacy.glob('google-*.json'))
        f.rename(legacy / ('google-' + '0' * 32 + '.json'))
    else:
        (legacy / 'google-token.json').write_text(json.dumps({'refresh_token': 'shared'}))
    with pytest.raises(migrate_cloud.MigrationError):
        migrate_cloud.run(legacy, apply=True)
    with db.connect() as c:                                 # this import's marker; schema releases keep their own rows
        mine = c.execute('SELECT count(*) AS n FROM migrations WHERE name=%s', (migrate_cloud.NAME,)).fetchone()['n']
    assert table_count(db, 'users') == 0 and mine == 0


def test_refuses_to_merge_into_a_populated_database(legacy, db):
    auth.create_user('someone@example.test')
    with pytest.raises(migrate_cloud.MigrationError, match='already has accounts'):
        migrate_cloud.run(legacy, apply=True)
