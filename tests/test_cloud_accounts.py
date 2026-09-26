from concurrent.futures import ThreadPoolExecutor
import importlib
import time

import pytest


ALICE = 'alice@example.test'
BOB = 'bob@example.test'


def account(email=ALICE):
    from app import auth
    auth.create_user(email, 'long-initial-password')


def test_database_requires_postgresql(monkeypatch):
    from app import database
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(RuntimeError, match='DATABASE_URL'):
        with database.connect():
            pass
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///not-allowed.db')
    with pytest.raises(RuntimeError, match='PostgreSQL'):
        with database.connect():
            pass


def test_imports_never_connect_or_write(monkeypatch):
    from app import database, auth, store, plans, billing
    def forbidden(*args, **kwargs):
        raise AssertionError('Import attempted a database connection')
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', forbidden)
    monkeypatch.setattr(psycopg.Connection, 'connect', classmethod(forbidden))  # what the pool uses
    from app import gdrive, jobs, worker, migrate_cloud, start
    for module in (database, auth, store, plans, billing, gdrive, jobs, worker, migrate_cloud, start):
        importlib.reload(module)


@pytest.mark.parametrize('schema', ['public; DROP TABLE users', 'pg_catalog', 'information_schema', 'MixedCase'])
def test_schema_identifier_rejects_unsafe_names(monkeypatch, schema):
    from app import database
    monkeypatch.setenv('DATABASE_SCHEMA', schema)
    with pytest.raises(RuntimeError, match='DATABASE_SCHEMA'):
        database.schema_name()


def test_first_signup_is_member_and_secret_required(db, monkeypatch):
    from app import auth
    account()
    assert auth.role(ALICE) == 'member'
    monkeypatch.delenv('SESSION_SECRET')
    with pytest.raises(RuntimeError, match='SESSION_SECRET'):
        auth.secret()


def test_schema_initialize_is_idempotent_and_text_is_utf8(db):
    from app import store
    account()
    owner = db.user_id(ALICE)
    rid = store.add_outreach(ALICE, 'cohost', 'José', 'https://example.test', 'Zürich', 'draft')
    db.initialize()
    assert db.user_id(ALICE) == owner
    assert store.outreach_get(rid, user=ALICE)['name'] == 'José'


def test_password_change_revokes_session(db):
    from app import auth
    account()
    cookie, _ = auth.issue(ALICE)
    assert auth.check(cookie) == ALICE
    auth.set_password(ALICE, 'long-new-password')
    assert auth.check(cookie) is None
    assert not auth.verify(ALICE, 'long-initial-password')
    assert auth.verify(ALICE, 'long-new-password')


def test_legacy_password_hash_and_stable_identity(db, monkeypatch):
    from app import auth
    salt = '01' * 16
    with db.connect() as c:
        c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  ('legacy-owner', ALICE, salt, auth._hash('legacy-password', salt, 200000), 200000, 'member', time.time()))
    assert auth.verify(ALICE.upper(), 'legacy-password')
    assert db.user_id(ALICE) == 'legacy-owner'
    auth.set_password(ALICE, 'replacement-password')
    assert db.user_id(ALICE) == 'legacy-owner'
    monkeypatch.setenv('APP_USER', BOB)
    monkeypatch.setenv('APP_PASSWORD', 'emergency-password')
    assert not auth.verify(BOB, 'emergency-password')


def test_session_tampering_deletion_and_csrf(db):
    from app import auth
    account()
    account(BOB)
    auth.create_user('operator@example.test', 'operator-password', 'admin')
    cookie, _ = auth.issue(ALICE)
    bob_cookie, ttl = auth.issue(BOB, long=False)
    assert ttl is None
    assert auth.check(cookie[:-5] + 'bad') is None
    assert auth.check('not-base64') is None
    assert auth.csrf_ok(cookie, auth.csrf_token(cookie))
    assert not auth.csrf_ok(bob_cookie, auth.csrf_token(cookie))
    with pytest.raises(ValueError, match='Admin only'):
        auth.delete_user(ALICE, BOB)
    auth.delete_user(ALICE, 'operator@example.test')
    assert auth.check(cookie) is None


def test_removal_deactivates_identity_and_preserves_business_records(db):
    from app import auth, store, plans, billing
    account()
    auth.create_user('operator@example.test', 'operator-password', 'admin')
    store.set_plan(ALICE, 'starter', 3)
    plans.reserve(ALICE, 'https://example.test/one', 'preserved-job')
    order = billing.create_order(ALICE, 'commercial')
    rid = store.add_outreach(ALICE, 'cohost', 'A', 'https://example.test', 'London', 'draft')
    owner = db.user_id(ALICE)
    cookie, _ = auth.issue(ALICE)
    auth.delete_user(ALICE, 'operator@example.test')
    assert db.user_id(ALICE) == owner
    assert store.get_account(ALICE)['credits'] == 2
    assert store.count_usage(user=ALICE) == 1
    assert billing.get_order(order['ref'])['owner_id'] == owner
    assert store.outreach_get(rid, user=ALICE)['message'] == 'draft'
    assert auth.check(cookie) is None
    assert not auth.verify(ALICE, 'long-initial-password')
    assert ALICE not in [u['user'] for u in auth.users()]
    with pytest.raises(ValueError, match='No such user'):
        auth.issue(ALICE)
    with pytest.raises(ValueError, match='No such user'):
        auth.set_password(ALICE, 'replacement-password')
    with pytest.raises(ValueError, match='already has an account'):
        account()
    with pytest.raises(ValueError, match='no longer active'):
        plans.reserve(ALICE, 'https://example.test/two', 'inactive-job')
    assert not plans.can_generate(ALICE, 'https://example.test/two')[0]
    with db.connect() as c:
        assert not c.execute('SELECT active FROM users WHERE id=%s', (owner,)).fetchone()['active']
    with pytest.raises(Exception, match='foreign key'):
        with db.connect() as c:
            c.execute('DELETE FROM users WHERE id=%s', (owner,))


def test_consume_preserves_none_return(db):
    from app import store, plans
    account()
    store.set_plan(ALICE, 'starter', 1)
    assert plans.consume(ALICE, 'https://example.test/one', 'consumed-job') is None
    assert store.get_account(ALICE)['credits'] == 0


def test_schema_upgrades_initial_cascades_without_losing_records(db):
    from app import store
    account()
    store.set_plan(ALICE, 'starter', 3)
    with db.connect() as c:
        c.execute('ALTER TABLE accounts DROP CONSTRAINT accounts_owner_id_fkey')
        c.execute('ALTER TABLE accounts ADD CONSTRAINT accounts_owner_id_fkey FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE')
        c.execute('ALTER TABLE users DROP COLUMN active, DROP COLUMN deactivated_at')
    db.initialize()
    assert store.get_account(ALICE)['credits'] == 3
    with db.connect() as c:
        assert c.execute('SELECT active FROM users WHERE email=%s', (ALICE,)).fetchone()['active']
        assert c.execute("SELECT confdeltype FROM pg_constraint WHERE conrelid='accounts'::regclass AND conname='accounts_owner_id_fkey'").fetchone()['confdeltype'] == 'a'


def test_shared_login_failures_survive_module_reload(db):
    from app import auth
    for _ in range(5):
        auth.record_fail('192.0.2.1')
    importlib.reload(auth)
    assert auth.too_many('192.0.2.1')
    assert not auth.too_many('192.0.2.2')
    auth.clear_fails('192.0.2.1')
    assert not auth.too_many('192.0.2.1')


def test_two_owners_business_data_is_separate(db):
    from app import store, billing
    account()
    account(BOB)
    store.set_plan(ALICE, 'starter', 3)
    store.ensure_account(BOB)
    order = billing.create_order(ALICE, 'starter')
    rid = store.add_outreach(ALICE, 'cohost', 'A', 'https://example.test', 'London', 'draft')
    assert billing.get_order_for(order['ref'], BOB) is None
    assert billing.orders(BOB) == []
    assert store.outreach_rows(BOB) == []
    assert store.outreach_get(rid, user=BOB) is None
    assert store.get_account(BOB)['credits'] == 0
    with db.connect() as c:
        assert c.execute('SELECT owner_id FROM accounts WHERE credits=3').fetchone()['owner_id'] == db.user_id(ALICE, c)


def test_credit_reservation_is_atomic_and_refund_once(db):
    from app import store, plans
    account()
    store.set_plan(ALICE, 'starter', 1)
    def reserve(i):
        try:
            plans.reserve(ALICE, f'https://example.test/{i}', f'job-{i}')
            return i
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        admitted = [i for i in pool.map(reserve, range(6)) if i is not None]
    assert len(admitted) == 1
    assert store.get_account(ALICE)['credits'] == 0
    plans.refund(f'job-{admitted[0]}')
    plans.refund(f'job-{admitted[0]}')
    assert store.get_account(ALICE)['credits'] == 1
    assert store.count_usage(user=ALICE) == 0


def test_reservation_joins_caller_transaction(db):
    from app import store, plans
    account()
    store.set_plan(ALICE, 'starter', 1)
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            plans.reserve(ALICE, 'https://example.test/one', 'rolled-back', conn=c)
            raise RuntimeError('job insert failed')
    assert store.get_account(ALICE)['credits'] == 1
    assert store.count_usage(user=ALICE) == 0


def test_reservation_idempotency_owner_and_listing_match(db):
    from app import store, plans
    account()
    account(BOB)
    store.set_plan(ALICE, 'starter', 2)
    plans.reserve(ALICE, 'https://example.test/one', 'same-job')
    plans.reserve(ALICE, 'https://example.test/one', 'same-job')
    assert store.get_account(ALICE)['credits'] == 1
    for user, listing in [(BOB, 'one'), (ALICE, 'two')]:
        with pytest.raises(ValueError, match='does not match'):
            plans.reserve(user, 'https://example.test/' + listing, 'same-job')
    plans.reserve(ALICE, 'https://example.test/one', 'rerun-job')
    assert store.get_account(ALICE)['credits'] == 1
    assert not plans.refund('missing-job')
    assert plans.refund('rerun-job')
    assert store.get_account(ALICE)['credits'] == 1
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            plans.refund('same-job', conn=c)
            raise RuntimeError('caller rollback')
    assert store.get_account(ALICE)['credits'] == 1
    assert plans.refund('same-job')
    with pytest.raises(ValueError, match='released'):
        plans.reserve(ALICE, 'https://example.test/one', 'same-job')


def test_free_network_budget_is_atomic_across_owners(db, monkeypatch):
    from app import store, plans
    monkeypatch.setattr(plans, 'FREE_PER_NET', 1)
    account()
    account(BOB)
    def reserve(user):
        try:
            plans.reserve(user, 'https://example.test/' + user, 'job-' + user, ip='203.0.113.9')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, [ALICE, BOB])) == 1
    assert store.count_usage(ip='203.0.113.9') == 1


def test_outreach_mutations_are_owner_scoped(db):
    from app import store
    account()
    account(BOB)
    rid = store.add_outreach(ALICE, 'cohost', 'A', 'https://example.test', 'London', 'draft')
    store.outreach_set(rid, user=BOB, status='sent')
    assert store.outreach_get(rid, user=ALICE)['status'] == 'queued'
    store.outreach_set(rid, user=ALICE, status='sent', sent_at=time.time())
    assert store.outreach_stats(ALICE)['sent'] == 1
    assert store.outreach_stats(BOB)['sent'] == 0
    assert store.sent_today(ALICE) == 1
    assert store.sent_today(BOB) == 0
    assert store.cities(ALICE) == ['London']
    assert store.cities(BOB) == []
    with pytest.raises(ValueError, match='Unsupported'):
        store.outreach_set(rid, user=ALICE, owner_id=db.user_id(BOB))


def test_settle_concurrently_grants_once_and_rollback_is_atomic(db):
    from app import store, billing
    account()
    store.set_plan(ALICE, 'free', 0)
    order = billing.create_order(ALICE, 'starter')
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: billing.settle(order['ref']), range(6)))
    assert all(o['status'] == 'paid' for o in results)
    assert store.get_account(ALICE)['credits'] == 3
    assert store.get_account(ALICE)['plan'] == 'starter'
    another = billing.create_order(ALICE, 'commercial')
    with db.connect() as c:
        c.execute("CREATE FUNCTION reject_paid() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='paid' THEN RAISE EXCEPTION 'synthetic failure'; END IF; RETURN NEW; END $$")
        c.execute('CREATE TRIGGER reject_paid BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION reject_paid()')
    with pytest.raises(Exception, match='synthetic failure'):
        billing.settle(another['ref'])
    assert store.get_account(ALICE)['credits'] == 3
    assert billing.get_order(another['ref'])['status'] == 'pending'


def test_network_and_device_signals_expire_after_retention(db):
    from app import auth, store
    auth.create_user('old@example.test', 'long-initial-password')
    store.ensure_account('old@example.test', 'free')
    store.record_usage('old@example.test', 'free', 'https://www.airbnb.co.uk/rooms/1', 'job-old', '203.0.113.9')
    with db.connect() as c:
        c.execute("UPDATE accounts SET ip_hash='legacy-net',fp_hash='legacy-device'")  # imported legacy values
        c.execute("UPDATE usage SET fp_hash='legacy-device'")
        c.execute('UPDATE usage SET ts=ts-%s', (91 * 86400,))
        c.execute('UPDATE accounts SET created=created-%s', (91 * 86400,))
    store.purge_signals()
    with db.connect() as c:
        assert c.execute('SELECT ip_hash,fp_hash FROM usage').fetchone() == {'ip_hash': None, 'fp_hash': None}
        assert c.execute('SELECT ip_hash,fp_hash FROM accounts').fetchone() == {'ip_hash': None, 'fp_hash': None}
    assert store.count_usage(user='old@example.test') == 1


def test_pool_reuses_connections_and_survives_a_killed_connection(db):
    pids = set()
    for _ in range(20):
        with db.connect() as c:
            pids.add(c.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
    assert len(pids) <= 10  # bounded by DB_POOL_MAX: reused, never a new connection per transaction
    first = pids.pop()
    import os, psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as admin:
        for pid in [first, *pids]:
            admin.execute('SELECT pg_terminate_backend(%s)', (pid,))
    with db.connect() as c:
        assert c.execute('SELECT 1 AS one').fetchone()['one'] == 1


def test_operator_console_sets_a_plan_and_refuses_bad_input(owners, db):
    from app import admin, plans, store
    assert admin.main(['set-plan', 'Alice@Example.test', 'starter', '3']) == 0
    assert plans.account_view('alice@example.test')['credits'] == 3
    assert store.get_account('alice@example.test')['note'] == 'set by operator console'
    assert admin.main(['set-plan', 'alice@example.test', 'gold', '3']) == 2
    assert admin.main(['set-plan', 'alice@example.test', 'starter', '-1']) == 2
    assert admin.main(['set-plan', 'nobody@example.test', 'starter', '1']) == 1
    assert plans.account_view('alice@example.test')['plan'] == 'starter'


def test_operator_console_deactivates_without_an_admin_session(owners, db):
    from app import admin, auth, jobs
    assert admin.main(['deactivate', 'alice@example.test']) == 0
    assert 'alice@example.test' not in {u['user'] for u in auth.users()}
    assert not auth.verify('alice@example.test', 'long-initial-password')
    assert admin.main(['deactivate', 'nobody@example.test']) == 1
