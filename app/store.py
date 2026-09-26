"""PostgreSQL business records. Owner IDs are durable; email remains the API boundary.

Usage is not age-purged: lifetime free quotas and no-double-charge reruns depend on it.
Network/device signals are HMACs, never raw addresses.
"""
import time
import hmac
import hashlib
import json
import ipaddress
from psycopg import sql
from app import database

# Compatibility name for callers; this is a real psycopg transaction context.
conn = database.connect

def _key():
    from app import auth;return auth.secret().encode()
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
def fp_hash(fp):return h('fp:'+str(fp)[:400]) if fp else None
def listing_key(url):
    import re
    u=re.sub(r'[?#].*$','',(url or '').strip().lower().rstrip('/'))
    m=re.search(r'/rooms/(\d+)',u)
    return 'airbnb:'+m.group(1) if m else u[:180]

def get_account(user, conn=None):
    with database.transaction(conn) as c:
        return c.execute('SELECT a.*,u.email AS "user" FROM accounts a JOIN users u ON u.id=a.owner_id WHERE u.email=%s',
                         ((user or '').strip().lower(),)).fetchone()


def ensure_account(user, plan='free', ip=None, fp=None, conn=None):
    with database.transaction(conn) as c:
        owner = database.user_id(user, c)
        c.execute('INSERT INTO accounts(owner_id,plan,created,ip_hash,fp_hash) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(owner_id) DO NOTHING',
                  (owner, plan, time.time(), ip_hash(ip) if ip else None, fp_hash(fp)))
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


def record_usage(user, plan, listing_url, job_id, ip=None, fp=None, kind='video', credits=1, conn=None, debited=False):
    with database.transaction(conn) as c:
        c.execute('INSERT INTO usage(ts,owner_id,plan,kind,listing_key,job_id,ip_hash,fp_hash,credits,debited) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (time.time(), database.user_id(user, c), plan, kind, listing_key(listing_url), job_id,
                   ip_hash(ip) if ip else None, fp_hash(fp), credits, debited))


def count_usage(user=None, ip=None, fp=None, since_days=None, listing_url=None, conn=None):
    q = "SELECT count(*) AS n FROM usage WHERE kind='video' AND refunded_at IS NULL"
    args = []
    with database.transaction(conn) as c:
        for column, value in [('owner_id', database.user_id(user, c) if user else None),
                              ('ip_hash', ip_hash(ip) if ip else None), ('fp_hash', fp_hash(fp) if fp else None),
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
        return c.execute('INSERT INTO outreach(ts,owner_id,channel,name,url,city,message,status,meta) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                         (time.time(), database.user_id(user, c), channel, name, url, city, message, status, json.dumps(meta or {}))).fetchone()['id']


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
        q = sql.SQL('UPDATE outreach SET {} WHERE id=%s').format(
            sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in kw))
        args = [*kw.values(), rid]
        if user:
            q += sql.SQL(' AND owner_id=%s')
            args.append(database.user_id(user, c))
        c.execute(q, args)


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
