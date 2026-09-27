"""Refer-a-host programme: a shareable link, a bonus video for both sides when the invited account's first reel
is delivered. Schema: schema/007_referrals.sql, schema/008_referral_guards.sql.

What keeps it lawful (sources fetched 27 Sep 2026, see PLAN.md):
- PECR reg 6 / Sch A1: the code travels in the link (/r/<code> -> /signup?ref=<code>) and a hidden signup field.
  No referral cookie and no browser storage, so there is nothing to ask consent for.
- ICO, electronic mail marketing ("refer a friend"): encouraging people to send our marketing by email or DM is
  instigating it. So the product never emails or messages anyone, offers no send buttons or pre-written messages,
  and the Account card and Terms say: public posts only, labelled as an ad (ASA, CAP Code 2.1), never in Airbnb
  listings, guidebooks or guest messages, and by email/text/DM only to someone who asked for the link (solicited).
- Nothing is decided at signup and the export shows a neutral status, so no account can learn anything about the
  other one (for example where the inviter has been) from the programme.
"""
import datetime as dt
import re
import time

from app import database, plans, store

CODE = re.compile(r'^[0-9a-f]{12}$')
MONTHLY_LIMIT = 10  # rewards per inviter per calendar month (UTC)
REWARDED = 'first_reel_delivered'
REASONS = {REWARDED: 'Rewarded', 'same_network': 'Same network as the inviter', 'monthly_limit': 'Inviter at the monthly limit',
           'same_google_account': 'Same Google account as the inviter', 'google_account_used': 'Google account already rewarded',
           'account_inactive': 'An account was removed or on hold'}


def month_start(ts):
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def code_for(user):
    return store.ensure_account(user, plans._default_plan(user))['referral_code']


def attribute(referee, code):
    """A new account signed up through an invite link. Returns 'pending', or None (ignored). Nothing is decided here:
    every check runs at the first delivery, so signing up through a link tells nobody anything."""
    code = (code or '').strip().lower()
    if not CODE.match(code):
        return None
    with database.connect() as c:
        referee_id = database.user_id(referee, c)
        # FOR SHARE: the inviter's erasure or deactivation waits for this insert and then clears it, or goes first
        # and the re-checked u.active refuses the code (the same guard as plans.reserve).
        row = c.execute('SELECT a.owner_id FROM accounts a JOIN users u ON u.id=a.owner_id '
                        'WHERE a.referral_code=%s AND u.active AND u.erased_at IS NULL FOR SHARE OF u', (code,)).fetchone()
        if not row or row['owner_id'] == referee_id:
            return None
        added = c.execute('INSERT INTO referrals(referrer_id,referee_id,ts) VALUES(%s,%s,%s) '
                          'ON CONFLICT (referee_id) DO NOTHING RETURNING id', (row['owner_id'], referee_id, time.time()))
        return 'pending' if added.fetchone() else None


def _google(c, owner):
    row = c.execute('SELECT google_sub FROM drive_connections WHERE owner_id=%s', (owner,)).fetchone()
    return row['google_sub'] if row else None


def _google_hash(sub):
    return store.h('google:' + sub) if sub else None


# Every network either account used: free videos, sign-ups and sign-ins (all kept 90 days, keyed hashes only).
_NETS = 'SELECT ip_hash FROM usage WHERE owner_id=%s UNION SELECT ip_hash FROM signin_networks WHERE owner_id=%s'


def _refusal(c, referrer, referee, now):
    active = c.execute('SELECT count(*) AS n FROM users u JOIN accounts a ON a.owner_id=u.id WHERE u.id=ANY(%s) '
                       'AND u.active AND u.erased_at IS NULL AND a.blocked=0', ([referrer, referee],)).fetchone()['n']
    if active < 2:
        return 'account_inactive'
    if c.execute(f'SELECT 1 FROM ({_NETS}) a JOIN ({_NETS}) b USING (ip_hash) WHERE ip_hash<>%s LIMIT 1',
                 (referrer, referrer, referee, referee, store.ip_hash('unknown'))).fetchone():
        return 'same_network'
    sub = _google(c, referee)
    if sub and sub == _google(c, referrer):
        return 'same_google_account'
    # ponytail: serialised per inviter only; two inviters' referees on one Google account at the same instant can both win.
    if sub and c.execute('SELECT 1 FROM referrals WHERE google_hash=%s AND rewarded_at IS NOT NULL LIMIT 1',
                         (_google_hash(sub),)).fetchone():
        return 'google_account_used'
    if c.execute('SELECT count(*) AS n FROM referrals WHERE referrer_id=%s AND rewarded_at>=%s',
                 (referrer, month_start(now))).fetchone()['n'] >= MONTHLY_LIMIT:
        return 'monthly_limit'
    return None


def reward_first_delivery(c, referee_id, now=None):
    """In the transaction that marks the referee's reel delivered: decide their waiting referral, once.
    Returns the decision, or None when there was nothing to decide."""
    now = now or time.time()
    # Row lock: a second worker delivering another reel of the same account waits, then finds it decided.
    ref = c.execute('SELECT id,referrer_id FROM referrals WHERE referee_id=%s AND reward_reason IS NULL FOR UPDATE',
                    (referee_id,)).fetchone()
    if not ref:
        return None
    # The monthly count must not race between two of the same inviter's referees.
    c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('referrer:' + ref['referrer_id'],))
    reason = _refusal(c, ref['referrer_id'], referee_id, now) or REWARDED
    rewarded = reason == REWARDED
    if rewarded:
        for owner in sorted((ref['referrer_id'], referee_id)):  # fixed order: no deadlock with another reward
            c.execute('UPDATE accounts SET bonus_videos=bonus_videos+1 WHERE owner_id=%s', (owner,))
    c.execute('UPDATE referrals SET reward_reason=%s,rewarded_at=%s,google_hash=%s WHERE id=%s',
              (reason, now if rewarded else None, _google_hash(_google(c, referee_id)) if rewarded else None, ref['id']))
    return reason


def rewarded_count(user):
    with database.connect() as c:
        return c.execute('SELECT count(*) AS n FROM referrals WHERE referrer_id=%s AND rewarded_at IS NOT NULL',
                         (database.user_id(user, c),)).fetchone()['n']


def totals():
    """Programme totals for admins (Settings): counts only."""
    with database.connect() as c:
        out = c.execute('SELECT count(*) AS signups,count(rewarded_at) AS rewarded,'
                        'count(*) FILTER (WHERE reward_reason IS NULL) AS pending FROM referrals').fetchone()
        out['not_rewarded'] = {REASONS.get(r['reward_reason'], r['reward_reason']): r['n'] for r in c.execute(
            'SELECT reward_reason,count(*) AS n FROM referrals WHERE rewarded_at IS NULL AND reward_reason IS NOT NULL '
            'GROUP BY reward_reason ORDER BY reward_reason').fetchall()}
        out['bonus_unused'] = c.execute('SELECT COALESCE(sum(bonus_videos),0) AS n FROM accounts').fetchone()['n']
        out['bonus_used'] = c.execute('SELECT count(*) AS n FROM usage WHERE bonus AND refunded_at IS NULL').fetchone()['n']
        return out
