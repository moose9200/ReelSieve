"""SQLite store for ReelSieve: usage/quota events, signup signals and outreach tracking.
Privacy: IP addresses and device fingerprints are never stored raw — only an HMAC of the network prefix
(/24 for IPv4, /64 for IPv6), keyed by SESSION_SECRET. Rows older than RETENTION_DAYS are purged on open.
DB path: DB_PATH env, else <ROOT>/.listing-reel/reelsieve.db (on Railway point DB_PATH at /data)."""
import os,sqlite3,time,hmac,hashlib,json,ipaddress,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
DB_PATH=Path(os.getenv('DB_PATH') or (ROOT/'.listing-reel'/'reelsieve.db'))
RETENTION_DAYS=int(os.getenv('RETENTION_DAYS','90'))
_lock=threading.Lock()
SCHEMA="""
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, plan TEXT, kind TEXT,
  listing_key TEXT, job_id TEXT, ip_hash TEXT, fp_hash TEXT, credits INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_usage_user ON usage(user);
CREATE INDEX IF NOT EXISTS ix_usage_ip ON usage(ip_hash);
CREATE INDEX IF NOT EXISTS ix_usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS accounts(
  user TEXT PRIMARY KEY, plan TEXT DEFAULT 'free', credits INTEGER DEFAULT 0,
  created REAL, ip_hash TEXT, fp_hash TEXT, note TEXT, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS outreach(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, channel TEXT, name TEXT, url TEXT,
  city TEXT, message TEXT, status TEXT DEFAULT 'queued', note TEXT, sent_at REAL, meta TEXT);
CREATE INDEX IF NOT EXISTS ix_out_user ON outreach(user);
CREATE INDEX IF NOT EXISTS ix_out_status ON outreach(status);
"""
def _conn():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB_PATH,timeout=15);c.row_factory=sqlite3.Row;c.executescript(SCHEMA);return c
_purged=[0.0]
def conn():
    c=_conn()
    if time.time()-_purged[0]>3600:
        cut=time.time()-RETENTION_DAYS*86400
        try:c.execute('DELETE FROM usage WHERE ts<?',(cut,));c.commit()
        except Exception:pass
        _purged[0]=time.time()
    return c
# ---------- hashing (no raw PII at rest) ----------
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
# ---------- accounts ----------
def get_account(user):
    with _lock,conn() as c:
        r=c.execute('SELECT * FROM accounts WHERE user=?',(user,)).fetchone()
        return dict(r) if r else None
def ensure_account(user,plan='free',ip=None,fp=None):
    with _lock,conn() as c:
        if not c.execute('SELECT 1 FROM accounts WHERE user=?',(user,)).fetchone():
            c.execute('INSERT INTO accounts(user,plan,credits,created,ip_hash,fp_hash) VALUES(?,?,?,?,?,?)',
                      (user,plan,0,time.time(),ip_hash(ip) if ip else None,fp_hash(fp)));c.commit()
    return get_account(user)
def set_plan(user,plan,credits=None,note=None):
    with _lock,conn() as c:
        cur=c.execute('SELECT credits FROM accounts WHERE user=?',(user,)).fetchone()
        cr=credits if credits is not None else (cur['credits'] if cur else 0)
        c.execute('INSERT INTO accounts(user,plan,credits,created) VALUES(?,?,?,?) ON CONFLICT(user) DO UPDATE SET plan=excluded.plan,credits=excluded.credits'+(',note=?' if note else ''),
                  ((user,plan,cr,time.time())+((note,) if note else ())));c.commit()
    return get_account(user)
def add_credits(user,n):
    with _lock,conn() as c:
        c.execute('UPDATE accounts SET credits=COALESCE(credits,0)+? WHERE user=?',(int(n),user));c.commit()
    return get_account(user)
def all_accounts():
    with _lock,conn() as c:return [dict(r) for r in c.execute('SELECT * FROM accounts ORDER BY created DESC').fetchall()]
def set_blocked(user,blocked=1):
    with _lock,conn() as c:c.execute('UPDATE accounts SET blocked=? WHERE user=?',(int(blocked),user));c.commit()
# ---------- usage ----------
def record_usage(user,plan,listing_url,job_id,ip=None,fp=None,kind='video',credits=1):
    with _lock,conn() as c:
        c.execute('INSERT INTO usage(ts,user,plan,kind,listing_key,job_id,ip_hash,fp_hash,credits) VALUES(?,?,?,?,?,?,?,?,?)',
                  (time.time(),user,plan,kind,listing_key(listing_url),job_id,ip_hash(ip) if ip else None,fp_hash(fp),credits));c.commit()
def count_usage(user=None,ip=None,fp=None,since_days=None,listing_url=None):
    q='SELECT COUNT(*) n FROM usage WHERE kind="video"';a=[]
    if user:q+=' AND user=?';a.append(user)
    if ip:q+=' AND ip_hash=?';a.append(ip_hash(ip))
    if fp:q+=' AND fp_hash=?';a.append(fp_hash(fp))
    if listing_url:q+=' AND listing_key=?';a.append(listing_key(listing_url))
    if since_days:q+=' AND ts>?';a.append(time.time()-since_days*86400)
    with _lock,conn() as c:return c.execute(q,a).fetchone()['n']
def last_usage_ts(user):
    with _lock,conn() as c:
        r=c.execute('SELECT MAX(ts) t FROM usage WHERE user=? AND kind="video"',(user,)).fetchone();return r['t'] or 0
def usage_rows(user=None,limit=200):
    q='SELECT * FROM usage';a=[]
    if user:q+=' WHERE user=?';a.append(user)
    q+=' ORDER BY ts DESC LIMIT ?';a.append(limit)
    with _lock,conn() as c:return [dict(r) for r in c.execute(q,a).fetchall()]
# ---------- outreach ----------
def add_outreach(user,channel,name,url,city,message,meta=None,status='queued'):
    with _lock,conn() as c:
        cur=c.execute('INSERT INTO outreach(ts,user,channel,name,url,city,message,status,meta) VALUES(?,?,?,?,?,?,?,?,?)',
                      (time.time(),user,channel,name,url,city,message,status,json.dumps(meta or {})));c.commit();return cur.lastrowid
def outreach_rows(user=None,limit=500):
    q='SELECT * FROM outreach';a=[]
    if user:q+=' WHERE user=?';a.append(user)
    q+=' ORDER BY ts DESC LIMIT ?';a.append(limit)
    with _lock,conn() as c:return [dict(r) for r in c.execute(q,a).fetchall()]
def outreach_get(rid):
    with _lock,conn() as c:
        r=c.execute('SELECT * FROM outreach WHERE id=?',(rid,)).fetchone();return dict(r) if r else None
def outreach_set(rid,**kw):
    if not kw:return
    cols=','.join(f'{k}=?' for k in kw)
    with _lock,conn() as c:c.execute(f'UPDATE outreach SET {cols} WHERE id=?',(*kw.values(),rid));c.commit()
def outreach_stats(user=None):
    q='SELECT status,COUNT(*) n FROM outreach';a=[]
    if user:q+=' WHERE user=?';a.append(user)
    q+=' GROUP BY status'
    with _lock,conn() as c:
        d={r['status']:r['n'] for r in c.execute(q,a).fetchall()}
    return {k:d.get(k,0) for k in ('queued','sent','replied','won','skipped')}
def sent_today(user=None,channel='cohost'):
    cut=time.time()-86400;q='SELECT COUNT(*) n FROM outreach WHERE status IN ("sent","replied","won") AND channel=? AND COALESCE(sent_at,ts)>?';a=[channel,cut]
    if user:q+=' AND user=?';a.append(user)
    with _lock,conn() as c:return c.execute(q,a).fetchone()['n']
def cities(user=None,limit=12):
    q='SELECT city,COUNT(*) n FROM outreach WHERE city IS NOT NULL AND city!=""';a=[]
    if user:q+=' AND user=?';a.append(user)
    q+=' GROUP BY city ORDER BY n DESC LIMIT ?';a.append(limit)
    with _lock,conn() as c:return [r['city'] for r in c.execute(q,a).fetchall()]
