"""Refer-a-host programme against real isolated PostgreSQL: codes, rewards, limits, bonus videos, export, erasure and
retention. Google is the synthetic fake; nothing is sent to anyone. Browser-facing parts: test_referrals_web.py."""
import threading
import time

import pytest

from app import admin, auth, jobs, plans, referrals, retention, store, worker
from fakes import connect
from test_cloud_jobs import SUCCESS, FAILURE, URL, claimed, command
from test_privacy_rights import ALICE, BOB, client_for

DAY = 86400
YEAR = 365.25 * DAY


def new_user(email):
    auth.create_user(email, 'long-enough-pass')
    store.ensure_account(email)
    return email


def referral_rows(db):
    with db.connect() as c:
        return c.execute('SELECT * FROM referrals ORDER BY id').fetchall()


def bonus(db, email):
    with db.connect() as c:
        return c.execute('SELECT bonus_videos FROM accounts WHERE owner_id=%s', (db.user_id(email),)).fetchone()['bonus_videos']


def uploading_job(db, email, job_id):
    """A reel whose files are in Drive and whose worker is about to mark it delivered."""
    from psycopg.types.json import Jsonb
    now = time.time()
    with db.connect() as c:
        c.execute("INSERT INTO jobs(id,owner_id,idempotency_key,request_hash,url,params,status,lease_token,lease_until,"
                  "drive_generation,created,updated) VALUES(%s,%s,%s,'h',%s,%s,'uploading',%s,%s,1,%s,%s)",
                  (job_id, db.user_id(email), job_id, URL, Jsonb({}), 'lease-' + job_id, now + 60, now, now))
    return job_id


# ---------------- 1. code ----------------

def test_every_account_gets_a_stable_unique_code_that_survives_restarts(owners, db):
    for who in (ALICE, BOB):
        store.ensure_account(who)
    a, b = referrals.code_for(ALICE), referrals.code_for(BOB)
    assert referrals.CODE.match(a) and referrals.CODE.match(b) and a != b
    db.initialize()
    assert referrals.code_for(ALICE) == a


def test_accounts_created_before_the_programme_get_a_code_on_upgrade(owners, db):
    store.ensure_account(ALICE)
    with db.connect() as c:
        c.execute('ALTER TABLE accounts DROP COLUMN referral_code')
    db.initialize()
    assert referrals.CODE.match(referrals.code_for(ALICE))


# ---------------- 2. signup attribution ----------------

def test_the_referrer_cannot_refer_their_own_account(owners, db):
    store.ensure_account(ALICE)
    assert referrals.attribute(ALICE, referrals.code_for(ALICE)) is None
    assert referral_rows(db) == []


# ---------------- 3. reward on the first delivered reel ----------------

@pytest.fixture
def invited(owners, google, monkeypatch, tmp_path):
    """Alice (Drive connected) signed up through Bob's link."""
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    monkeypatch.setattr(worker, 'LEASE', 5)
    monkeypatch.setattr(worker, 'BEAT', 0.2)
    worker._stop.clear()
    connect(owners, google)
    store.ensure_account(BOB)
    store.ensure_account(ALICE)
    assert referrals.attribute(ALICE, referrals.code_for(BOB)) == 'pending'
    return owners


def test_first_delivered_reel_gives_both_accounts_one_bonus_video_once(invited, db):
    jobs.admit(ALICE, URL, {})
    worker.process(claimed(db), command(FAILURE))
    assert bonus(db, ALICE) == 0 and bonus(db, BOB) == 0 and referral_rows(db)[0]['reward_reason'] is None
    jobs.admit(ALICE, URL, {}, 'second')
    worker.process(claimed(db), command(SUCCESS))
    [row] = referral_rows(db)
    assert row['reward_reason'] == 'first_reel_delivered' and row['rewarded_at']
    assert bonus(db, ALICE) == 1 and bonus(db, BOB) == 1
    jobs.admit(ALICE, 'https://www.airbnb.co.uk/rooms/777', {}, 'third')
    worker.process(claimed(db), command(SUCCESS))
    assert bonus(db, ALICE) == 0 and bonus(db, BOB) == 1  # Alice's third reel used her bonus; nobody is rewarded twice


def test_concurrent_workers_delivering_two_reels_reward_once(invited, db):
    ids = [uploading_job(db, ALICE, f'cccccc00000{i}') for i in (1, 2)]
    gate = threading.Barrier(2)

    def deliver(job_id):
        gate.wait()
        return jobs.finish(job_id, 'lease-' + job_id, 'done')
    threads = [threading.Thread(target=deliver, args=(j,)) for j in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert bonus(db, ALICE) == 1 and bonus(db, BOB) == 1
    assert [r['reward_reason'] for r in referral_rows(db)] == ['first_reel_delivered']


def test_deactivated_or_erased_accounts_earn_nothing(owners, db):
    store.ensure_account(BOB)
    for who in ('carol@example.org', 'dave@example.org'):
        new_user(who)
        referrals.attribute(who, referrals.code_for(BOB))
    ids = [db.user_id(who) for who in ('carol@example.org', 'dave@example.org')]
    admin.deactivate('carol@example.org')
    admin.erase('dave@example.org')
    for owner in ids:
        with db.connect() as c:
            referrals.reward_first_delivery(c, owner)
    new_user('erin@example.org')
    referrals.attribute('erin@example.org', referrals.code_for(BOB))
    admin.deactivate(BOB)
    with db.connect() as c:
        assert referrals.reward_first_delivery(c, db.user_id('erin@example.org')) == 'account_inactive'
    assert bonus(db, BOB) == 0 and bonus(db, 'erin@example.org') == 0
    # Dave's row went with his erasure: nothing about him is written onto Bob's side.
    assert sorted(r['reward_reason'] for r in referral_rows(db)) == ['account_inactive', 'account_inactive']
    assert all(r['rewarded_at'] is None for r in referral_rows(db))


def test_referee_on_the_referrers_free_network_at_delivery_is_not_rewarded(invited, db):
    plans.reserve(BOB, 'https://www.airbnb.co.uk/rooms/1', 'bbbbbb000001', ip='203.0.113.7')
    jobs.admit(ALICE, URL, {}, ip='203.0.113.50')
    worker.process(claimed(db), command(SUCCESS))
    assert referral_rows(db)[0]['reward_reason'] == 'same_network'
    assert bonus(db, BOB) == 0


def test_paid_inviter_who_signed_in_from_the_invited_accounts_network_is_not_rewarded(invited, db):
    """Paid videos keep no network hash, so the inviter's sign-in network is what gives a self-invite away."""
    store.set_plan(BOB, 'starter', 3)
    plans.reserve(BOB, 'https://www.airbnb.co.uk/rooms/1', 'bbbbbb000001', ip='203.0.113.7')
    store.note_signin(BOB, '203.0.113.8')
    store.note_signin(ALICE, '203.0.113.9')  # her signup, same /24
    jobs.admit(ALICE, URL, {})
    worker.process(claimed(db), command(SUCCESS))
    assert referral_rows(db)[0]['reward_reason'] == 'same_network'
    assert bonus(db, BOB) == 0 and bonus(db, ALICE) == 0


def google_sub(db, email):
    with db.connect() as c:
        return c.execute('SELECT google_sub FROM drive_connections WHERE owner_id=%s', (db.user_id(email),)).fetchone()['google_sub']


def link_drive(db, email, sub):
    with db.connect() as c:
        c.execute("INSERT INTO drive_connections(owner_id,status,google_sub,updated) VALUES(%s,'connected',%s,%s) "
                  'ON CONFLICT (owner_id) DO UPDATE SET google_sub=EXCLUDED.google_sub', (db.user_id(email), sub, time.time()))


def test_invited_account_on_the_inviters_google_account_is_not_rewarded(invited, db):
    link_drive(db, BOB, google_sub(db, ALICE))
    jobs.admit(ALICE, URL, {})
    worker.process(claimed(db), command(SUCCESS))
    assert referral_rows(db)[0]['reward_reason'] == 'same_google_account' and bonus(db, BOB) == 0


def test_one_google_account_earns_one_invite_reward_even_after_disconnecting(invited, db):
    sub = google_sub(db, ALICE)
    jobs.admit(ALICE, URL, {})
    worker.process(claimed(db), command(SUCCESS))
    assert bonus(db, BOB) == 1
    link_drive(db, ALICE, None)  # Alice disconnects; her Google account then goes to a second invited account
    carol = new_user('carol@example.org')
    referrals.attribute(carol, referrals.code_for(BOB))
    link_drive(db, carol, sub)
    with db.connect() as c:
        assert referrals.reward_first_delivery(c, db.user_id(carol)) == 'google_account_used'
    assert bonus(db, BOB) == 1 and bonus(db, carol) == 0


def test_a_referral_error_never_undoes_a_delivered_reel(invited, db, monkeypatch):
    def broken(c, *a):
        c.execute('SELECT * FROM no_such_table')
    monkeypatch.setattr(referrals, '_refusal', broken)
    job = uploading_job(db, ALICE, 'aaaaaa00000e')
    assert jobs.finish(job, 'lease-' + job, 'done')
    with db.connect() as c:
        assert c.execute('SELECT status FROM jobs WHERE id=%s', (job,)).fetchone()['status'] == 'done'
    assert referral_rows(db)[0]['reward_reason'] is None and bonus(db, BOB) == 0 and bonus(db, ALICE) == 0


def test_signup_during_the_inviters_erasure_leaves_no_row_with_the_erased_id(owners, db, monkeypatch):
    """attribute() locks the inviter's users row, so erasure waits for the insert and then clears it."""
    store.ensure_account(BOB)
    code, bob_id, carol = referrals.code_for(BOB), db.user_id(BOB), new_user('carol@example.org')
    paused, go, real = threading.Event(), threading.Event(), time.time

    class Clock:  # attribute() reads the clock between its inviter lookup and its insert: pause it there
        @staticmethod
        def time():
            paused.set()
            go.wait(10)
            return real()
    monkeypatch.setattr(referrals, 'time', Clock)
    signup = threading.Thread(target=referrals.attribute, args=(carol, code))
    signup.start()
    assert paused.wait(10)
    erase = threading.Thread(target=admin.erase, args=(BOB,))
    erase.start()
    erase.join(1.0)  # without the lock, erasure finishes here, before the insert
    go.set()
    signup.join(10)
    erase.join(10)
    assert bob_id not in str(referral_rows(db))


# ---------------- 4. monthly limit, also under concurrency ----------------

def test_at_most_ten_rewards_per_referrer_per_calendar_month_even_with_concurrent_workers(owners, db):
    store.ensure_account(BOB)
    code, invitees = referrals.code_for(BOB), []
    for i in range(12):
        invitees.append(new_user(f'host{i}@example.org'))
        referrals.attribute(invitees[-1], code)
    # One reward last month does not count towards this month.
    last_month = referrals.month_start(time.time()) - DAY
    new_user('old@example.org')
    referrals.attribute('old@example.org', code)
    with db.connect() as c:
        c.execute("UPDATE referrals SET reward_reason='first_reel_delivered',rewarded_at=%s WHERE referee_id=%s",
                  (last_month, db.user_id('old@example.org')))
    ids = [uploading_job(db, who, f'dddddd0000{i:02d}') for i, who in enumerate(invitees)]
    gate = threading.Barrier(len(ids))

    def deliver(job_id):
        gate.wait()
        assert jobs.finish(job_id, 'lease-' + job_id, 'done')
    threads = [threading.Thread(target=deliver, args=(j,)) for j in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    reasons = [r['reward_reason'] for r in referral_rows(db) if r['referee_id'] != db.user_id('old@example.org')]
    assert reasons.count('first_reel_delivered') == 10 and reasons.count('monthly_limit') == 2
    assert bonus(db, BOB) == 10 and sum(bonus(db, who) for who in invitees) == 10


def test_month_start_is_the_first_of_the_calendar_month_in_utc():
    assert referrals.month_start(1790812800.0 + 3600) == 1790812800.0  # 1 Oct 2026 01:00 UTC -> 1 Oct 2026 00:00 UTC
    assert referrals.month_start(1790812800.0 - 1) == 1788220800.0      # 30 Sep 23:59:59 -> 1 Sep 2026


# ---------------- 5. bonus videos on every plan ----------------

def test_free_plan_uses_bonus_videos_first_and_refunds_them_once(owners, db):
    store.ensure_account(ALICE)
    with db.connect() as c:
        c.execute('UPDATE accounts SET bonus_videos=1 WHERE owner_id=%s', (db.user_id(ALICE),))
    view = plans.account_view(ALICE)
    assert view['remaining'] == plans.FREE_LIFETIME + 1 and view['bonus_videos'] == 1
    plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/1', 'eeeeee000001')
    assert bonus(db, ALICE) == 0 and plans.account_view(ALICE)['remaining'] == plans.FREE_LIFETIME
    with db.connect() as c:
        assert c.execute("SELECT bonus FROM usage WHERE job_id='eeeeee000001'").fetchone()['bonus'] is True
    assert plans.refund('eeeeee000001') and not plans.refund('eeeeee000001')
    assert bonus(db, ALICE) == 1
    for i in range(2, 5):  # bonus + the whole free allowance
        plans.reserve(ALICE, f'https://www.airbnb.co.uk/rooms/{i}', f'eeeeee00000{i}')
    assert plans.account_view(ALICE)['remaining'] == 0 and bonus(db, ALICE) == 0
    with pytest.raises(ValueError, match='Free plan covers'):
        plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/9', 'eeeeee000009')


def test_paid_plans_use_bonus_videos_before_credits_and_unlimited_never_spends_them(owners, db):
    store.set_plan(ALICE, 'starter', 0)
    store.set_plan(BOB, 'enterprise', 0)
    with db.connect() as c:
        c.execute('UPDATE accounts SET bonus_videos=1')
    assert plans.account_view(ALICE)['remaining'] == 1
    plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/1', 'ffffff000001')
    assert bonus(db, ALICE) == 0 and plans.account_view(ALICE)['credits'] == 0
    with pytest.raises(ValueError, match='No credits left'):
        plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/2', 'ffffff000002')
    assert plans.refund('ffffff000001') and bonus(db, ALICE) == 1 and plans.account_view(ALICE)['credits'] == 0
    plans.reserve(BOB, 'https://www.airbnb.co.uk/rooms/1', 'ffffff000003')
    assert bonus(db, BOB) == 1 and plans.account_view(BOB)['remaining'] is None


# ---------------- 6. export, erasure and retention ----------------

def test_export_has_both_sides_of_a_referral_without_the_other_party(invited, db):
    alice, bob = client_for(invited['alice']), client_for(invited['bob'])
    a, b = alice.get('/api/account/export'), bob.get('/api/account/export')
    assert a.json()['referrals'] and b.json()['referrals']
    assert [r['you_are'] for r in a.json()['referrals']] == ['referee']
    assert [r['you_are'] for r in b.json()['referrals']] == ['referrer']
    assert BOB not in a.text and db.user_id(BOB) not in a.text
    assert ALICE not in b.text and db.user_id(ALICE) not in b.text
    assert b.json()['accounts'][0]['referral_code'] == referrals.code_for(BOB) and 'bonus_videos' in b.json()['accounts'][0]
    # A neutral status only: the reason for a refusal would describe the other account (UK GDPR Art 15(4)).
    assert a.json()['signin_networks'] == [] and 'signin_networks' in b.json()
    assert [r['status'] for r in a.json()['referrals'] + b.json()['referrals']] == ['pending', 'pending']
    assert not any('reward_reason' in r for r in a.json()['referrals'] + b.json()['referrals'])


def test_erasing_an_invited_account_writes_nothing_new_about_it_on_the_inviters_side(invited, db):
    carol = new_user('carol@example.org')
    referrals.attribute(carol, referrals.code_for(BOB))
    jobs.admit(ALICE, URL, {})
    worker.process(claimed(db), command(SUCCESS))  # Alice rewarded, Carol still pending
    admin.erase(carol)
    admin.erase(ALICE)
    [row] = referral_rows(db)
    assert row['referee_id'] is None and row['google_hash'] is None and row['reward_reason'] == 'first_reel_delivered'
    [r] = client_for(invited['bob']).get('/api/account/export').json()['referrals']
    assert r['you_are'] == 'referrer' and r['status'] == 'rewarded' and r['google_account_hash'] is None
    assert set(r) == {'you_are', 'ts', 'rewarded_at', 'status', 'google_account_hash'}


def test_erasure_removes_the_erased_side_and_unrewarded_rows_and_retires_the_code(invited, db):
    code = referrals.code_for(BOB)
    jobs.admit(ALICE, URL, {})
    worker.process(claimed(db), command(SUCCESS))  # Alice's invite is rewarded
    carol = new_user('carol@example.org')
    referrals.attribute(carol, code)  # still pending when Bob is erased
    bob_id = db.user_id(BOB)
    admin.erase(BOB)
    [row] = referral_rows(db)  # Carol's unrewarded row is gone; Alice keeps the record of her own bonus
    assert row['referrer_id'] is None and row['referee_id'] == db.user_id(ALICE) and bob_id not in str(row)
    assert row['reward_reason'] == 'first_reel_delivered'
    with db.connect() as c:
        assert c.execute('SELECT referral_code FROM accounts WHERE owner_id=%s', (bob_id,)).fetchone()['referral_code'] is None
    assert referrals.attribute(new_user('dave@example.org'), code) is None  # the old link no longer attributes
    admin.erase(ALICE)
    assert referral_rows(db) == []  # nobody left on either side


def test_sign_in_networks_are_kept_90_days_and_only_the_latest_time_per_network(owners, db):
    store.note_signin(ALICE, '203.0.113.7')
    store.note_signin(ALICE, '203.0.113.99')  # same /24: one row
    store.note_signin(ALICE, 'not-an-ip')     # unknown network: nothing kept
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM signin_networks').fetchone()['n'] == 1
        c.execute('UPDATE signin_networks SET ts=ts-%s', (store.SIGNAL_DAYS * DAY + 60,))
    store.note_signin(BOB, '198.51.100.1')
    store.purge_signals()
    with db.connect() as c:
        assert [r['owner_id'] for r in c.execute('SELECT owner_id FROM signin_networks').fetchall()] == [db.user_id(BOB)]


def test_retention_deletes_referrals_two_years_after_reward_or_one_year_if_never_rewarded(owners, db):
    now = time.time()
    store.ensure_account(BOB)
    code, ages = referrals.code_for(BOB), {}
    cases = {'r-old': (2 * YEAR + DAY, True), 'r-new': (2 * YEAR - DAY, True),
             'u-old': (YEAR + DAY, False), 'u-new': (YEAR - DAY, False), 'refused-old': (YEAR + DAY, None)}
    for name, (age, rewarded) in cases.items():
        email = new_user(name + '@example.org')
        referrals.attribute(email, code)
        ages[db.user_id(email)] = name
        with db.connect() as c:
            if rewarded:
                c.execute("UPDATE referrals SET ts=%s,rewarded_at=%s,reward_reason='first_reel_delivered' WHERE referee_id=%s",
                          (now - age - DAY, now - age, db.user_id(email)))
            else:
                c.execute('UPDATE referrals SET ts=%s,reward_reason=%s WHERE referee_id=%s',
                          (now - age, None if rewarded is False else 'same_network', db.user_id(email)))
    out = retention.run(now)
    assert sorted(ages[r['referee_id']] for r in referral_rows(db)) == ['r-new', 'u-new']
    assert out['referrals_rewarded'] == 1 and out['referrals_unrewarded'] == 2
    again = retention.run(now)
    assert again['referrals_rewarded'] == again['referrals_unrewarded'] == 0
