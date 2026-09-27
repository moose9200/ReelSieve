"""PostgreSQL business records. Owner IDs are durable; email remains the API boundary.

Usage rows are kept: lifetime free quotas and no-double-charge reruns depend on them.
Network signals are pseudonymised (keyed HMACs of the /24 or /64 network, never raw addresses), kept only on free
videos, where the free-tier guard counts them, and cleared after SIGNAL_DAYS (privacy page).
"""
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
    """Key for network signals only: derived from SESSION_SECRET under its own purpose label, so it signs nothing else."""
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


def record_usage(user, plan, listing_url, job_id, ip=None, kind='video', credits=1, conn=None, debited=False):
    with database.transaction(conn) as c:
        c.execute('INSERT INTO usage(ts,owner_id,plan,kind,listing_key,job_id,ip_hash,credits,debited) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (time.time(), database.user_id(user, c), plan, kind, listing_key(listing_url), job_id,
                   ip_hash(ip) if ip else None, credits, debited))


def count_usage(user=None, ip=None, since_days=None, listing_url=None, conn=None):
    q = "SELECT count(*) AS n FROM usage WHERE kind='video' AND refunded_at IS NULL"
    args = []
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


def outreach_rows(user=None, limit=500):
    return _owned_rows('outreach', user, limit)


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


def _suppression_key():
    # ponytail: derived from SESSION_SECRET like the signal key, so rotating that secret stops old objections
    # matching. Re-key outreach_suppressions (or pin this key) before any rotation.
    from app import auth;return hmac.new(auth.secret().encode(),b'reelsieve:outreach-suppression:v1',hashlib.sha256).digest()


def suppression_keys(item):
    """How a prospect is known: their Airbnb user id when we have it, and their name with a listing."""
    import re
    ids, pid = [], str(item.get('id') or '')
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


def suppress(item):
    """Do not contact this prospect again, for any user. Stores hashes only."""
    with database.connect() as c:
        for key in suppression_keys(item):
            c.execute('INSERT INTO outreach_suppressions(key,ts) VALUES(%s,%s) ON CONFLICT (key) DO NOTHING', (key, time.time()))


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


def blocked_ids(ids, conn=None):
    ids = sorted({str(i) for i in ids if i})
    if not ids:
        return set()
    with database.transaction(conn) as c:
        return {r['listing_id'] for r in c.execute('SELECT listing_id FROM blocked_listings WHERE listing_id=ANY(%s)', (ids,)).fetchall()}


def without_blocked_listings(items):
    keyed = [(it, listing_ids(it)) for it in items]
    hit = blocked_ids(set().union(*(k for _, k in keyed))) if keyed else set()
    return [it for it, k in keyed if not k & hit]


def block_listing(listing_id, reason, conn=None):
    """True when newly blocked; blocking twice keeps the first reason."""
    with database.transaction(conn) as c:
        return bool(c.execute('INSERT INTO blocked_listings(listing_id,reason,ts) VALUES(%s,%s,%s) ON CONFLICT (listing_id) '
                              'DO NOTHING RETURNING listing_id', (listing_id, reason[:300], time.time())).fetchone())


def unblock_listing(listing_id):
    with database.connect() as c:
        return bool(c.execute('DELETE FROM blocked_listings WHERE listing_id=%s RETURNING listing_id', (listing_id,)).fetchone())


def blocked_listings():
    with database.connect() as c:
        return c.execute('SELECT * FROM blocked_listings ORDER BY ts DESC').fetchall()


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
                      'privacy_request_handled': 'Privacy request closed by our team'}


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
            'privacy_requests': rows('SELECT ref,ts,type,name,details,airbnb_profile_id,listing_id,status,due_at,handled_at FROM privacy_requests '
                                     'WHERE lower(email)=(SELECT email FROM users WHERE id=%(o)s) ORDER BY ts'),
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


def add_privacy_request(kind, email, name, details, airbnb_profile_id=None, listing_id=None):
    """Store a request and return (ref, received ts). The on-screen reference is the acknowledgement."""
    import secrets
    now = time.time()
    for _ in range(6):
        ref = 'PR-' + time.strftime('%y%m%d', time.gmtime(now)) + '-' + secrets.token_hex(3).upper()
        with database.connect() as c:
            if c.execute('INSERT INTO privacy_requests(ref,ts,type,email,name,details,airbnb_profile_id,listing_id,due_at) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ref) DO NOTHING RETURNING ref',
                         (ref, now, kind, email, name, details, airbnb_profile_id, listing_id, one_month_after(now))).fetchone():
                return ref, now
    raise RuntimeError('Could not allocate a request reference')


def open_privacy_requests():
    with database.connect() as c:
        return c.execute("SELECT * FROM privacy_requests WHERE status='open' ORDER BY due_at").fetchall()


def handle_privacy_request(ref):
    with database.connect() as c:
        return c.execute("UPDATE privacy_requests SET status='handled',handled_at=%s WHERE ref=%s AND status='open' RETURNING ref",
                         (time.time(), ref)).fetchone()
