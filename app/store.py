"""PostgreSQL business records. Owner IDs are durable; email remains the API boundary.

Usage rows are kept: lifetime free quotas and no-double-charge reruns depend on them.
Network signals are pseudonymised (keyed HMACs of the /24 or /64 network, never raw addresses), kept on free
videos, where the free-tier guard counts them, and per sign-up/sign-in for the invite self-invite check
(signin_networks), and cleared after SIGNAL_DAYS (privacy page).
"""
import os
import time
import hmac
import hashlib
import json
import ipaddress
from psycopg import sql
from psycopg.types.json import Jsonb
from app import database

# Compatibility name for callers; this is a real psycopg transaction context.
conn = database.connect

def _key():
    """Key for abuse signals only (network hashes; the invite Google-account hash, prefixed "google:"): derived from
    SESSION_SECRET under its own purpose label, so it signs nothing else."""
    from app import auth;return hmac.new(auth.secret().encode(),b'reelsieve:abuse-signal-key:v1',hashlib.sha256).digest()
def net_of(ip):
    """Group by network so a phone/office NAT isn't one identity per device, and IPv6 rotation doesn't defeat it."""
    try:
        a=ipaddress.ip_address(ip)
        return str(ipaddress.ip_network(f'{ip}/24',strict=False)) if a.version==4 else str(ipaddress.ip_network(f'{ip}/64',strict=False))
    except Exception:return 'unknown'
def h(value):
    if not value:return None
    return hmac.new(_key(),str(value).encode(),hashlib.sha256).hexdigest()[:32]
def ip_hash(ip):return h('ip:'+net_of(ip))
def listing_key(url):
    import re
    u=re.sub(r'[?#].*$','',(url or '').strip().lower().rstrip('/'))
    m=re.search(r'/rooms/(\d+)',u)
    return 'airbnb:'+m.group(1) if m else u[:180]

SIGNAL_DAYS = 90


def purge_signals(days=SIGNAL_DAYS):
    """Clear network hashes (and legacy device hashes) older than the published retention; usage and accounts stay."""
    cutoff = time.time() - days * 86400
    with database.connect() as c:
        c.execute('UPDATE usage SET ip_hash=NULL,fp_hash=NULL WHERE ts<%s AND (ip_hash IS NOT NULL OR fp_hash IS NOT NULL)', (cutoff,))
        c.execute('UPDATE accounts SET ip_hash=NULL,fp_hash=NULL WHERE created<%s AND (ip_hash IS NOT NULL OR fp_hash IS NOT NULL)', (cutoff,))
        c.execute('DELETE FROM signin_networks WHERE ts<%s', (cutoff,))


def note_signin(user, ip):
    """Keep the keyed network hash of a sign-up or sign-in (latest time per network) for the invite programme's
    self-invite check (app/referrals.py). Paid videos keep no network hash, so this is how a paying inviter's own
    network is known. Nothing is kept for an unknown address."""
    if net_of(ip) == 'unknown':
        return
    with database.connect() as c:
        c.execute('INSERT INTO signin_networks(owner_id,ip_hash,ts) VALUES(%s,%s,%s) '
                  'ON CONFLICT (owner_id,ip_hash) DO UPDATE SET ts=EXCLUDED.ts', (database.user_id(user, c), ip_hash(ip), time.time()))


def get_account(user, conn=None):
    with database.transaction(conn) as c:
        return c.execute('SELECT a.*,u.email AS "user" FROM accounts a JOIN users u ON u.id=a.owner_id WHERE u.email=%s',
                         ((user or '').strip().lower(),)).fetchone()


def ensure_account(user, plan='free', conn=None):
    with database.transaction(conn) as c:
        owner = database.user_id(user, c)
        c.execute('INSERT INTO accounts(owner_id,plan,created) VALUES(%s,%s,%s) ON CONFLICT(owner_id) DO NOTHING',
                  (owner, plan, time.time()))
        return get_account(user, c)


def set_plan(user, plan, credits=None, note=None, conn=None):
    with database.transaction(conn) as c:
        ensure_account(user, conn=c)
        c.execute('UPDATE accounts SET plan=%s,credits=COALESCE(%s,credits),note=COALESCE(%s,note) WHERE owner_id=%s',
                  (plan, credits, note, database.user_id(user, c)))
        return get_account(user, c)


def add_credits(user, n, conn=None):
    with database.transaction(conn) as c:
        ensure_account(user, conn=c)
        c.execute('UPDATE accounts SET credits=credits+%s WHERE owner_id=%s', (int(n), database.user_id(user, c)))
        return get_account(user, c)


def all_accounts():
    with database.connect() as c:
        return c.execute('SELECT a.*,u.email AS "user" FROM accounts a JOIN users u ON u.id=a.owner_id ORDER BY created DESC').fetchall()


def set_blocked(user, blocked=1):
    with database.connect() as c:
        c.execute('UPDATE accounts SET blocked=%s WHERE owner_id=%s', (int(bool(blocked)), database.user_id(user, c)))


def record_usage(user, plan, listing_url, job_id, ip=None, kind='video', credits=1, conn=None, debited=False, bonus=False):
    with database.transaction(conn) as c:
        c.execute('INSERT INTO usage(ts,owner_id,plan,kind,listing_key,job_id,ip_hash,credits,debited,bonus) '
                  'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (time.time(), database.user_id(user, c), plan, kind, listing_key(listing_url), job_id,
                   ip_hash(ip) if ip else None, credits, debited, bonus))


def count_usage(user=None, ip=None, since_days=None, listing_url=None, conn=None, bonus=None):
    """Videos made and not refunded. bonus=False counts only those that used the plan's own allowance."""
    q = "SELECT count(*) AS n FROM usage WHERE kind='video' AND refunded_at IS NULL"
    args = []
    if bonus is not None:
        q += ' AND bonus=%s'
        args.append(bonus)
    with database.transaction(conn) as c:
        for column, value in [('owner_id', database.user_id(user, c) if user else None),
                              ('ip_hash', ip_hash(ip) if ip else None),
                              ('listing_key', listing_key(listing_url) if listing_url else None)]:
            if value is not None:
                q += f' AND {column}=%s'
                args.append(value)
        if since_days:
            q += ' AND ts>%s'
            args.append(time.time() - since_days * 86400)
        return c.execute(q, args).fetchone()['n']


def last_usage_ts(user, conn=None):
    with database.transaction(conn) as c:
        return c.execute("SELECT max(ts) AS t FROM usage WHERE owner_id=%s AND kind='video' AND refunded_at IS NULL",
                         (database.user_id(user, c),)).fetchone()['t'] or 0


def _owned_rows(table, user, limit):
    with database.connect() as c:
        q = sql.SQL('SELECT t.*,u.email AS "user" FROM {} t JOIN users u ON u.id=t.owner_id').format(sql.Identifier(table))
        args = []
        if user:
            q += sql.SQL(' WHERE t.owner_id=%s')
            args.append(database.user_id(user, c))
        q += sql.SQL(' ORDER BY t.ts DESC LIMIT %s')
        return c.execute(q, args + [limit]).fetchall()


def usage_rows(user=None, limit=200):
    return _owned_rows('usage', user, limit)


def add_outreach(user, channel, name, url, city, message, meta=None, status='queued'):
    with database.connect() as c:
        now = time.time()
        return c.execute('INSERT INTO outreach(ts,updated,owner_id,channel,name,url,city,message,status,meta) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                         (now, now, database.user_id(user, c), channel, name, url, city, message, status, json.dumps(meta or {}))).fetchone()['id']


def _outreach_listings(row):
    """The listings an outreach row names: its link, and the listing link kept in meta (never the row's own id)."""
    try:
        meta = json.loads(row.get('meta') or '{}')
    except (TypeError, ValueError):
        meta = {}
    return listing_ids({'url': row.get('url'), 'listing_url': meta.get('listing_url') if isinstance(meta, dict) else None})


def outreach_rows(user=None, limit=500):
    """What the Outreach page and export show: a listing taken down from ReelSieve leaves them at once (the row itself
    stays until outreach retention, and in the user's own data export)."""
    return without_blocked_listings(_owned_rows('outreach', user, limit), _outreach_listings)


def outreach_get(rid, user=None):
    with database.connect() as c:
        q = 'SELECT o.*,u.email AS "user" FROM outreach o JOIN users u ON u.id=o.owner_id WHERE o.id=%s'
        args = [rid]
        if user:
            q += ' AND o.owner_id=%s'
            args.append(database.user_id(user, c))
        return c.execute(q, args).fetchone()


def outreach_set(rid, user=None, **kw):
    if not kw:
        return
    allowed = {'status', 'note', 'sent_at', 'channel', 'name', 'url', 'city', 'message', 'meta'}
    if not set(kw) <= allowed:
        raise ValueError('Unsupported outreach field')
    with database.connect() as c:
        q = sql.SQL('UPDATE outreach SET {},updated=%s WHERE id=%s').format(
            sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in kw))
        args = [*kw.values(), time.time(), rid]
        if user:
            q += sql.SQL(' AND owner_id=%s')
            args.append(database.user_id(user, c))
        c.execute(q, args)


def outreach_delete(rid, user):
    with database.connect() as c:
        c.execute('DELETE FROM outreach WHERE id=%s AND owner_id=%s', (rid, database.user_id(user, c)))


_suppression_keys = {}  # (database, schema) -> the frozen key, read once per process
SUPPRESSION_KEY_META = 'outreach_suppression_key'


def _suppression_key():
    """The do-not-contact HMAC key. Made once, exactly as it always was (from SESSION_SECRET under its own label), then
    stored encrypted in app_meta (TOKEN_ENCRYPTION_KEY, like Drive tokens) and always read from there, so rotating
    SESSION_SECRET never voids an objection. The web process freezes it at start. Rotate TOKEN_ENCRYPTION_KEY only with
    the old key in TOKEN_ENCRYPTION_OLD_KEYS: a key that cannot be read stops the app rather than start a new list."""
    where = (os.getenv('DATABASE_URL'), database.schema_name())
    if where not in _suppression_keys:
        from cryptography.fernet import InvalidToken
        from app import auth, gdrive
        fernet = gdrive._fernet()
        made = hmac.new(auth.secret().encode(), b'reelsieve:outreach-suppression:v1', hashlib.sha256).digest()
        with database.connect() as c:
            c.execute('INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT (key) DO NOTHING',
                      (SUPPRESSION_KEY_META, Jsonb({'enc': fernet.encrypt(made).decode()})))
            stored = c.execute('SELECT value FROM app_meta WHERE key=%s', (SUPPRESSION_KEY_META,)).fetchone()['value']
        try:
            _suppression_keys[where] = fernet.decrypt(stored['enc'].encode())
        except InvalidToken:
            raise RuntimeError('The stored do-not-contact key cannot be decrypted: put the previous TOKEN_ENCRYPTION_KEY '
                               'in TOKEN_ENCRYPTION_OLD_KEYS') from None
    return _suppression_keys[where]


def suppression_keys(item):
    """How a prospect is known: a company by its Companies House number; a host by their Airbnb user id when we have
    it, and their name with a listing."""
    import re
    ids, pid = [], str(item.get('id') or '')
    company = str(item.get('company_number') or '').strip().upper()
    if re.fullmatch(r'[A-Z0-9]{8}', company):
        ids.append('company:' + company)  # app/companies.py matches this key in SQL: change both together
    m = re.search(r'/users/show/(\d{1,20})', str(item.get('airbnb_profile') or item.get('profile_url') or ''))
    uid = m.group(1) if m else (pid[1:] if re.fullmatch(r'u\d{1,20}', pid) else None)
    if uid:
        ids.append('user:' + uid)
    m = re.search(r'/(?:rooms|contact_host)/(\d{1,20})', f"{item.get('listing_url') or ''} {item.get('url') or ''}")
    listing = m.group(1) if m else (pid if re.fullmatch(r'\d{1,20}', pid) else None)
    name = ' '.join(str(item.get('name') or '').lower().split())
    if name and listing:
        ids.append(f'name:{name}|listing:{listing}')
    return [hmac.new(_suppression_key(), i.encode(), hashlib.sha256).hexdigest() for i in ids]


DAILY_SUPPRESSIONS = 30  # keys one account may add in 24 hours (a host can take two); cohost sends are capped at 5 a day


def suppress(item, user=None):
    """Do not contact this prospect again, for any user. Stores hashes only.
    user: the account marking it, recorded so misuse can be traced and undone (app/admin.py unsuppress), and limited to
    DAILY_SUPPRESSIONS a day; returns False, storing nothing, past that. None: an objection through the privacy form."""
    with database.connect() as c:
        owner = database.user_id(user, c) if user else None
        if owner and c.execute('SELECT count(*) AS n FROM outreach_suppressions WHERE owner_id=%s AND ts>%s',
                               (owner, time.time() - 86400)).fetchone()['n'] >= DAILY_SUPPRESSIONS:
            return False
        for key in suppression_keys(item):
            c.execute('INSERT INTO outreach_suppressions(key,ts,owner_id) VALUES(%s,%s,%s) ON CONFLICT (key) DO NOTHING',
                      (key, time.time(), owner))
    return True


def set_b2b_sender(user, name, business, email):
    """The name, business name and reply email the account last put on a business email (filled in next time)."""
    with database.connect() as c:
        ensure_account(user, conn=c)
        c.execute('UPDATE accounts SET b2b_sender=%s WHERE owner_id=%s',
                  (Jsonb({'name': name, 'business': business, 'email': email}), database.user_id(user, c)))


def unsuppressed(items):
    """The prospects nobody has asked us to stop contacting."""
    keyed = [(it, set(suppression_keys(it))) for it in items]
    wanted = sorted(set().union(*(k for _, k in keyed))) if keyed else []
    if not wanted:
        return list(items)
    with database.connect() as c:
        hit = {r['key'] for r in c.execute('SELECT key FROM outreach_suppressions WHERE key=ANY(%s)', (wanted,)).fetchall()}
    return [it for it, k in keyed if not k & hit]


# ---------------- listing takedowns: no reels of these listings, and not in co-host or Outreach results ----------------

def listing_ids(item):
    """Every Airbnb listing an item names: a numeric id, a listing link or a contact-host link."""
    import re
    pid = str(item.get('id') or '')
    found = set(re.findall(r'/(?:rooms(?:/plus)?|contact_host)/(\d{1,20})', f"{item.get('listing_url') or ''} {item.get('url') or ''}"))
    return found | ({pid} if re.fullmatch(r'\d{1,20}', pid) else set())


ACTIVE_BLOCK = '(expires_at IS NULL OR expires_at>%s)'  # confirmed, or waiting for an admin's check and not lapsed


def blocked_ids(ids, conn=None):
    ids = sorted({str(i) for i in ids if i})
    if not ids:
        return set()
    with database.transaction(conn) as c:
        return {r['listing_id'] for r in c.execute(f'SELECT listing_id FROM blocked_listings WHERE listing_id=ANY(%s) AND {ACTIVE_BLOCK}',
                                                   (ids, time.time())).fetchall()}


def without_blocked_listings(items, key=listing_ids):
    keyed = [(it, key(it)) for it in items]
    hit = blocked_ids(set().union(*(k for _, k in keyed))) if keyed else set()
    return [it for it, k in keyed if not k & hit]


def block_listing(listing_id, reason, conn=None, expires_at=None):
    """True when newly blocked; blocking twice keeps the first reason. expires_at: a provisional block (a public request
    nobody has checked yet) that lapses then unless confirmed. A confirmed block replaces a provisional one, and any
    block replaces a lapsed one."""
    now = time.time()
    with database.transaction(conn) as c:
        return bool(c.execute('INSERT INTO blocked_listings(listing_id,reason,ts,expires_at) VALUES(%s,%s,%s,%s) '
                              'ON CONFLICT (listing_id) DO UPDATE SET reason=EXCLUDED.reason,ts=EXCLUDED.ts,expires_at=EXCLUDED.expires_at '
                              'WHERE blocked_listings.expires_at IS NOT NULL AND (EXCLUDED.expires_at IS NULL OR blocked_listings.expires_at<=%s) '
                              'RETURNING listing_id', (listing_id, reason[:300], now, expires_at, now)).fetchone())


def confirm_listing_block(listing_id):
    """An admin checked a provisional block: it stays until removed."""
    with database.connect() as c:
        return bool(c.execute('UPDATE blocked_listings SET expires_at=NULL WHERE listing_id=%s AND expires_at IS NOT NULL '
                              'RETURNING listing_id', (listing_id,)).fetchone())


def unblock_listing(listing_id):
    with database.connect() as c:
        return bool(c.execute('DELETE FROM blocked_listings WHERE listing_id=%s RETURNING listing_id', (listing_id,)).fetchone())


def blocked_listings():
    with database.connect() as c:
        return c.execute(f'SELECT * FROM blocked_listings WHERE {ACTIVE_BLOCK} ORDER BY ts DESC', (time.time(),)).fetchall()


def recent_removals(email, since):
    """(this email's, everyone's) 'Remove my listing' requests since ts."""
    with database.connect() as c:
        row = c.execute("SELECT count(*) FILTER (WHERE email=%s) AS mine, count(*) AS total FROM privacy_requests "
                        "WHERE type='listing_removal' AND ts>%s", (email, since)).fetchone()
    return row['mine'], row['total']


def outreach_stats(user=None):
    with database.connect() as c:
        q = 'SELECT status,count(*) AS n FROM outreach'
        args = []
        if user:
            q += ' WHERE owner_id=%s'
            args.append(database.user_id(user, c))
        rows = c.execute(q + ' GROUP BY status', args).fetchall()
        counts = {r['status']: r['n'] for r in rows}
        return {k: counts.get(k, 0) for k in ('queued', 'sent', 'replied', 'won', 'skipped')}


def sent_today(user=None, channel='cohost'):
    with database.connect() as c:
        q = "SELECT count(*) AS n FROM outreach WHERE status IN ('sent','replied','won') AND channel=%s AND COALESCE(sent_at,ts)>%s"
        args = [channel, time.time() - 86400]
        if user:
            q += ' AND owner_id=%s'
            args.append(database.user_id(user, c))
        return c.execute(q, args).fetchone()['n']


def cities(user=None, limit=12):
    with database.connect() as c:
        q = "SELECT city,count(*) AS n FROM outreach WHERE city IS NOT NULL AND city<>''"
        args = []
        if user:
            q += ' AND owner_id=%s'
            args.append(database.user_id(user, c))
        return [r['city'] for r in c.execute(q + ' GROUP BY city ORDER BY n DESC LIMIT %s', args + [limit]).fetchall()]


def admin_event(action, actor=None, target=None, conn=None, **detail):
    """Accountability trail. actor/target are emails (actor None: operator console or retention); detail never holds secrets."""
    with database.transaction(conn) as c:
        c.execute('INSERT INTO admin_events(ts,actor_id,target_id,action,detail) VALUES(%s,%s,%s,%s,%s)',
                  (time.time(), database.user_id(actor, c) if actor else None, database.user_id(target, c) if target else None,
                   action, Jsonb(detail)))


TEAM_CHANGE_LABELS = {'plan': 'Plan or credits changed by our team', 'password_reset': 'Password reset by our team',
                      'order_settle': 'Order marked paid by our team', 'order_cancel': 'Order cancelled by our team',
                      'order_link': 'Payment link added to an order by our team',
                      'privacy_request_handled': 'Privacy request closed by our team',
                      'unsuppress': 'Your "Do not contact" marks removed by our team'}


def team_changes(user, limit=10):
    """What admins changed on this account, newest first, without saying which admin (shown on the Account page)."""
    with database.connect() as c:
        rows = c.execute('SELECT ts,action FROM admin_events WHERE target_id=%s ORDER BY ts DESC,id DESC LIMIT %s',
                         (database.user_id(user, c), limit)).fetchall()
    return [{'ts': r['ts'], 'label': TEAM_CHANGE_LABELS.get(r['action'], 'Account changed by our team')} for r in rows]


def admin_events(limit=50):
    with database.connect() as c:
        return c.execute('SELECT e.ts,e.action,e.detail,a.email AS actor,t.email AS target FROM admin_events e '
                         'LEFT JOIN users a ON a.id=e.actor_id LEFT JOIN users t ON t.id=e.target_id '
                         'ORDER BY e.ts DESC,e.id DESC LIMIT %s', (limit,)).fetchall()


def export(user):
    """Everything held about one owner, every table (UK/EU GDPR Art 15 and 20; Account > Download my data).
    Never password material, Drive credentials, OAuth state hashes or other people's identities."""
    with database.connect() as c:
        o = {'o': database.user_id(user, c)}
        rows = lambda q: c.execute(q, o).fetchall()  # noqa: E731
        return {
            'users': rows('SELECT id,email,role,created,changed,active,deactivated_at,erased_at FROM users WHERE id=%(o)s'),
            'accounts': rows('SELECT * FROM accounts WHERE owner_id=%(o)s'),
            'usage': rows('SELECT * FROM usage WHERE owner_id=%(o)s ORDER BY ts'),
            'jobs': rows('SELECT id,url,params,status,progress,step,log,meta,error,created,updated,finished_at '
                         'FROM jobs WHERE owner_id=%(o)s ORDER BY created'),
            'orders': rows('SELECT ref,ts,plan,amount_usd,provider,status,paid_at,note,meta,pay_link,billing_email '
                           'FROM orders WHERE owner_id=%(o)s ORDER BY ts'),
            'outreach': rows('SELECT * FROM outreach WHERE owner_id=%(o)s ORDER BY ts'),
            'drive_connections': rows('SELECT status,google_email,google_sub,scope,folder_id,connected_at,updated '
                                      'FROM drive_connections WHERE owner_id=%(o)s'),
            'drive_uploads': rows('SELECT job_id,variant,file_id,status,name,web_view_link,size,sharing,created,confirmed_at '
                                  'FROM drive_uploads WHERE owner_id=%(o)s ORDER BY created'),
            'drive_oauth_states': rows('SELECT redirect_uri,created,expires_at FROM drive_oauth_states WHERE owner_id=%(o)s'),
            'admin_events': rows('SELECT ts,action,detail,actor_id=%(o)s AS by_you,target_id=%(o)s AS about_you '
                                 'FROM admin_events WHERE actor_id=%(o)s OR target_id=%(o)s ORDER BY ts'),
            'signin_networks': rows('SELECT ip_hash,ts FROM signin_networks WHERE owner_id=%(o)s ORDER BY ts'),
            # Both sides of each invite, never who the other account is, and a neutral status only: why a reward was
            # refused would describe the other account (UK GDPR Art 15(4)). The Google hash is the invited side's own.
            'referrals': rows("SELECT CASE WHEN referrer_id=%(o)s THEN 'referrer' ELSE 'referee' END AS you_are,ts,rewarded_at,"
                              "CASE WHEN rewarded_at IS NOT NULL THEN 'rewarded' WHEN reward_reason IS NULL THEN 'pending' "
                              "ELSE 'not_rewarded' END AS status,CASE WHEN referee_id=%(o)s THEN google_hash END AS google_account_hash "
                              'FROM referrals WHERE referrer_id=%(o)s OR referee_id=%(o)s ORDER BY ts'),
            'privacy_requests': rows('SELECT ref,ts,type,name,details,airbnb_profile_id,company_number,listing_id,status,due_at,handled_at '
                                     'FROM privacy_requests WHERE lower(email)=(SELECT email FROM users WHERE id=%(o)s) ORDER BY ts'),
            # when you marked someone "Do not contact"; the keyed hash identifies them, not you, so it stays out
            'outreach_suppressions': rows('SELECT ts FROM outreach_suppressions WHERE owner_id=%(o)s ORDER BY ts'),
        }


PRIVACY_REQUEST_TYPES = {'access': 'Access', 'erasure': 'Erasure', 'rectification': 'Rectification',
                         'objection': 'Objection to outreach', 'listing_removal': 'Remove my listing from ReelSieve',
                         'complaint': 'Complaint', 'other': 'Other'}


def one_month_after(ts):
    """UK GDPR Art 12(3) 'within one month of receipt': the same day next month, or that month's last day."""
    import calendar
    import datetime as dt
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1])).timestamp()


def add_privacy_request(kind, email, name, details, airbnb_profile_id=None, company_number=None, listing_id=None):
    """Store a request and return (ref, received ts). The on-screen reference is the acknowledgement."""
    import secrets
    now = time.time()
    for _ in range(6):
        ref = 'PR-' + time.strftime('%y%m%d', time.gmtime(now)) + '-' + secrets.token_hex(3).upper()
        with database.connect() as c:
            if c.execute('INSERT INTO privacy_requests(ref,ts,type,email,name,details,airbnb_profile_id,company_number,listing_id,due_at) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ref) DO NOTHING RETURNING ref',
                         (ref, now, kind, email, name, details, airbnb_profile_id, company_number, listing_id, one_month_after(now))).fetchone():
                return ref, now
    raise RuntimeError('Could not allocate a request reference')


def open_privacy_requests():
    with database.connect() as c:
        return c.execute("SELECT * FROM privacy_requests WHERE status='open' ORDER BY due_at").fetchall()


def handle_privacy_request(ref):
    with database.connect() as c:
        return c.execute("UPDATE privacy_requests SET status='handled',handled_at=%s WHERE ref=%s AND status='open' RETURNING ref",
                         (time.time(), ref)).fetchone()
