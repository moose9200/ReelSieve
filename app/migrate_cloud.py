"""One-time import of the legacy file-based volume into PostgreSQL.

    python -m app.migrate_cloud /data --dry-run     # validate and count; writes nothing
    python -m app.migrate_cloud /data --apply       # import once, atomically, with a completion marker

Source: auth.json, reelsieve.db (SQLite, opened read-only), jobs/*/job.json and per-user Drive token
files. Every record must map to a known account: an unknown or ambiguous owner halts the whole
import, and nothing is ever assigned to "the first admin". Password hashes, roles, balances, order
references and job owners are preserved; Drive credentials are encrypted and bound to their owner.
Interrupted legacy jobs become failed, never re-queued. The source is never modified or deleted.
Output is counts only: no secrets, tokens or customer content are printed.
"""
import calendar
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from psycopg.types.json import Jsonb

from app import database, gdrive, jobs

NAME = 'legacy-volume'
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
TOKEN_FILE = re.compile(r'^google-([0-9a-f]{32})\.json$')
LISTING_KEYS = ('id', 'url', 'title', 'city', 'rating', 'count', 'guests', 'host')
INTERRUPTED = ('This reel was interrupted when ReelSieve moved to its new cloud system. It was not retried '
               'automatically — start it again.')


class MigrationError(Exception):
    pass


def owner_id(email):
    """Deterministic across reruns and environments."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'reelsieve:legacy-user:' + email))


def _norm(email):
    return (email or '').strip().lower()


def _rows(db, table):
    if not db.exists():
        return []
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        return [dict(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]


def load(root):
    root = Path(root)
    auth_file = root / 'auth.json'
    if not auth_file.is_file():
        raise MigrationError('No auth.json in the legacy source')
    auth = json.loads(auth_file.read_text())
    if auth.get('hash') and 'users' not in auth:  # the single-account format the old app upgraded on read
        auth['users'] = {auth.get('user') or 'admin': {'salt': auth['salt'], 'hash': auth['hash'], 'role': 'admin',
                                                        'created': auth.get('created')}}
    db = root / 'reelsieve.db'
    tokens, stray = [], []
    for d in (root, root / 'gdrive'):
        for p in sorted(d.glob('google-*.json')) if d.is_dir() else []:
            (tokens if TOKEN_FILE.match(p.name) else stray).append(p)
    return {
        'users': {_norm(k): v for k, v in (auth.get('users') or {}).items()},
        'accounts': _rows(db, 'accounts'), 'usage': _rows(db, 'usage'),
        'outreach': _rows(db, 'outreach'), 'orders': _rows(db, 'orders'),
        'jobs': [json.loads(p.read_text()) for p in sorted(root.glob('jobs/*/job.json'))],
        'tokens': [(TOKEN_FILE.match(p.name).group(1), json.loads(p.read_text())) for p in tokens],
        'stray_tokens': [p.name for p in stray],
    }


def validate(src):
    problems = []
    users = src['users']
    if not users:
        problems.append('no accounts in auth.json')
    for email, u in users.items():
        if not EMAIL.match(email) and email != 'admin':
            problems.append('an account has an invalid email identifier')
        if not re.fullmatch(r'[0-9a-f]+', str(u.get('salt') or '')) or not re.fullmatch(r'[0-9a-f]{64}', str(u.get('hash') or '')):
            problems.append('an account has an unreadable password hash')
        if u.get('role', 'member') not in ('member', 'admin'):
            problems.append('an account has an unknown role')
    if users and not any(u.get('role') == 'admin' for u in users.values()):
        problems.append('no admin account')
    for table in ('accounts', 'usage', 'outreach', 'orders'):
        unknown = sum(1 for r in src[table] if _norm(r.get('user')) not in users)
        if unknown:
            problems.append(f'{unknown} {table} row(s) belong to no known account')
    job_ids = [j.get('id') for j in src['jobs']]
    if any(not j.get('user') or _norm(j['user']) not in users for j in src['jobs']):
        problems.append('a job has no owner or an unknown owner')
    if any(not jobs.JOB_ID.match(str(i or '')) for i in job_ids) or len(set(job_ids)) != len(job_ids):
        problems.append('job ids are missing, malformed or duplicated')
    usage_jobs = [r['job_id'] for r in src['usage'] if r.get('job_id')]
    if len(set(usage_jobs)) != len(usage_jobs):
        problems.append('usage has duplicate job references')
    refs = [o.get('ref') for o in src['orders']]
    if any(not r for r in refs) or len(set(refs)) != len(refs):
        problems.append('order references are missing or duplicated')
    for key, tok in src['tokens']:
        who = _norm(tok.get('user'))
        if who not in users or hashlib.sha256(who.encode()).hexdigest()[:32] != key:
            problems.append('a Drive token cannot be matched to exactly one account')
    if src['stray_tokens']:
        problems.append('an unowned shared Drive token exists; it cannot be assigned to an account')
    return problems


def _digest(src):
    return hashlib.sha256(json.dumps(src, sort_keys=True, default=str).encode()).hexdigest()


def counts(src):
    status = {}
    for j in src['jobs']:
        s = j.get('status') if j.get('status') in ('done', 'failed') else 'interrupted'
        status[s] = status.get(s, 0) + 1
    return {'users': len(src['users']), 'admins': sum(1 for u in src['users'].values() if u.get('role') == 'admin'),
            'accounts': len(src['accounts']), 'usage': len(src['usage']), 'outreach': len(src['outreach']),
            'orders': len(src['orders']), 'jobs': len(src['jobs']), 'jobs_by_status': status,
            'drive_receipts': sum(1 for j in src['jobs'] if j.get('status') == 'done' and j.get('drive_id')),
            'drive_connections': len(src['tokens'])}


def _created(job):
    try:
        return float(calendar.timegm(time.strptime(job.get('created') or '', '%Y-%m-%d %H:%M')))
    except ValueError:
        return time.time()


def _job_meta(j):
    listing = {k: j['listing'][k] for k in LISTING_KEYS if isinstance(j.get('listing'), dict) and j['listing'].get(k) is not None}
    plan = j.get('ai_plan')
    if isinstance(plan, dict):
        for shot in plan.get('shots') or []:
            if shot.get('error'):
                shot['error'] = jobs.clean(shot['error'], 120)
    return {'listing': listing, 'duration': j.get('duration'), 'audit': j.get('audit'), 'ai_plan': plan,
            'selection': j.get('selection'), 'photo_scores': j.get('photo_scores'), 'host_status': j.get('host_status'),
            'host_error': jobs.clean(j.get('host_error'), 200) if j.get('host_error') else None,
            'message': str(j.get('message') or '')[:2000], 'legacy': True}


def _insert(c, src, now):
    ids = {email: owner_id(email) for email in src['users']}
    for email, u in src['users'].items():
        c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created,changed) VALUES(%s,%s,%s,%s,200000,%s,%s,%s)',
                  (ids[email], email, u['salt'], u['hash'], u.get('role', 'member'), u.get('created') or now, u.get('changed')))
    for a in src['accounts']:
        c.execute('INSERT INTO accounts(owner_id,plan,credits,created,ip_hash,fp_hash,note,blocked) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                  (ids[_norm(a['user'])], a.get('plan') or 'free', max(0, int(a.get('credits') or 0)), a.get('created') or now,
                   a.get('ip_hash'), a.get('fp_hash'), a.get('note'), int(a.get('blocked') or 0)))
    for r in src['usage']:
        c.execute('INSERT INTO usage(owner_id,ts,plan,kind,listing_key,job_id,ip_hash,fp_hash,credits,debited) '
                  'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (ids[_norm(r['user'])], r.get('ts') or now, r.get('plan') or 'free', r.get('kind') or 'video', r.get('listing_key'),
                   r.get('job_id'), r.get('ip_hash'), r.get('fp_hash'), int(r.get('credits') if r.get('credits') is not None else 1),
                   r.get('kind') == 'video' and r.get('plan') in ('starter', 'commercial')))
    for o in src['outreach']:
        c.execute('INSERT INTO outreach(id,owner_id,ts,channel,name,url,city,message,status,note,sent_at,meta) '
                  'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (o['id'], ids[_norm(o['user'])], o.get('ts') or now, o.get('channel'), o.get('name'), o.get('url'), o.get('city'),
                   o.get('message'), o.get('status') or 'queued', o.get('note'), o.get('sent_at'), o.get('meta')))
    for o in src['orders']:
        c.execute('INSERT INTO orders(id,owner_id,ref,ts,plan,amount_usd,provider,status,paid_at,note,meta,pay_link) '
                  'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (o['id'], ids[_norm(o['user'])], o['ref'], o.get('ts') or now, o.get('plan'), float(o.get('amount_usd') or 0),
                   o.get('provider'), o.get('status') or 'pending', o.get('paid_at'), o.get('note'), o.get('meta'), o.get('pay_link')))
    for table in ('outreach', 'orders'):
        c.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), GREATEST((SELECT max(id) FROM {table}), 1))")
    for j in src['jobs']:
        owner, created = ids[_norm(j['user'])], _created(j)
        done = j.get('status') == 'done'
        status = j.get('status') if j.get('status') in ('done', 'failed') else 'failed'
        error = INTERRUPTED if j.get('status') not in ('done', 'failed') else (jobs.clean(j.get('error'), 300) if j.get('error') else None)
        params = {'ai_motion': bool(j.get('ai_motion')), 'style': j.get('style') or 'v2', 'plan': j.get('plan'),
                  'max_seconds': j.get('max_seconds'), 'send_to_host': bool(j.get('send_to_host')), 'message': str(j.get('message') or '')[:2000]}
        log = [jobs.clean(line) for line in (j.get('log') or [])][-jobs.LOG_LINES:]
        c.execute('INSERT INTO jobs(id,owner_id,idempotency_key,request_hash,url,params,status,progress,step,log,meta,error,'
                  'drive_generation,created,updated,finished_at,cleanup_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s)',
                  (j['id'], owner, 'legacy:' + j['id'], 'legacy', j.get('url') or '', Jsonb(params), status,
                   100 if done else int(j.get('progress') or 0), 'Done' if done else 'Failed', Jsonb(log), Jsonb(_job_meta(j)),
                   error, created, now, created, now))
        if done and j.get('drive_id'):
            # Legacy uploads were shared anyone-with-link and only the primary file was delivered.
            c.execute("INSERT INTO drive_uploads(owner_id,job_id,variant,file_id,generation,status,name,web_view_link,sharing,created,confirmed_at) "
                      "VALUES(%s,%s,'primary',%s,0,'confirmed',%s,%s,'public',%s,%s)",
                      (owner, j['id'], j['drive_id'], j.get('drive_name'), j.get('drive_link'), created, created))
    for _key, tok in src['tokens']:
        owner = ids[_norm(tok['user'])]
        creds = {'access_token': tok.get('access_token'), 'refresh_token': tok.get('refresh_token'),
                 'scope': tok.get('scope') or gdrive.SCOPES, 'expires_at': float(tok.get('expires_at') or 0)}
        usable = bool(creds['refresh_token']) and gdrive.DRIVE_SCOPE in creds['scope'].split()
        c.execute('INSERT INTO drive_connections(owner_id,generation,status,credentials,google_email,scope,connected_at,updated) '
                  'VALUES(%s,1,%s,%s,%s,%s,%s,%s)',
                  (owner, 'connected' if usable else 'reconnect_required', gdrive._encrypt(owner, 1, creds) if usable else None,
                   tok.get('email'), creds['scope'], now, now))


def _target_counts(c):
    q = lambda sql: c.execute(sql).fetchone()['n']  # noqa: E731
    status = {r['s']: r['n'] for r in c.execute("SELECT CASE WHEN status='failed' AND error=%s THEN 'interrupted' ELSE status END AS s,"
                                                'count(*) AS n FROM jobs GROUP BY 1', (INTERRUPTED,)).fetchall()}
    return {'users': q('SELECT count(*) AS n FROM users'), 'admins': q("SELECT count(*) AS n FROM users WHERE role='admin'"),
            'accounts': q('SELECT count(*) AS n FROM accounts'), 'usage': q('SELECT count(*) AS n FROM usage'),
            'outreach': q('SELECT count(*) AS n FROM outreach'), 'orders': q('SELECT count(*) AS n FROM orders'),
            'jobs': q('SELECT count(*) AS n FROM jobs'), 'jobs_by_status': status,
            'drive_receipts': q('SELECT count(*) AS n FROM drive_uploads'), 'drive_connections': q('SELECT count(*) AS n FROM drive_connections')}


def run(root, apply=False):
    src = load(root)
    problems = validate(src)
    if problems:
        raise MigrationError('Legacy migration halted: ' + '; '.join(sorted(set(problems))))
    digest, expected = _digest(src), counts(src)
    if not apply:
        return {'status': 'dry-run', 'source': digest[:16], 'counts': expected}
    with database.connect() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('reelsieve-legacy-migration',))
        marker = c.execute('SELECT source_digest,counts FROM migrations WHERE name=%s', (NAME,)).fetchone()
        if marker:
            if marker['source_digest'] != digest:
                raise MigrationError('The legacy source changed after it was migrated; refusing to import it again')
            return {'status': 'already-applied', 'source': digest[:16], 'counts': marker['counts']}
        if c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise MigrationError('The target database already has accounts; refusing to merge a legacy import into it')
        now = time.time()
        _insert(c, src, now)
        got = _target_counts(c)
        if got != expected:
            raise MigrationError('Imported counts do not match the source; nothing was committed')
        c.execute('INSERT INTO migrations(name,applied_at,source_digest,counts) VALUES(%s,%s,%s,%s)', (NAME, now, digest, Jsonb(expected)))
    return {'status': 'applied', 'source': digest[:16], 'counts': expected}


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[2] not in ('--dry-run', '--apply'):
        sys.exit('usage: python -m app.migrate_cloud <legacy-dir> --dry-run|--apply')
    if sys.argv[2] == '--apply':
        database.wait_for_schema(0)  # never migrates here: only the web process does
    try:
        print(json.dumps(run(sys.argv[1], sys.argv[2] == '--apply'), sort_keys=True))
    except MigrationError as e:
        sys.exit(str(e))
