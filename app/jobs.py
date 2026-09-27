"""Durable render jobs in PostgreSQL: owner-scoped reads, atomic admission, fenced worker leases.

Admission checks the owner's Drive connection, reserves quota and inserts the job in ONE
transaction, so a refused or failed admission never charges. Workers claim with SKIP LOCKED
and every write they make presents the lease token they were given; a cancelled, recovered or
reassigned job therefore ignores a late report. A job interrupted mid-render is failed and
refunded, never re-run automatically: its paid generation may already have happened.
"""
import hashlib
import json
import re
import secrets
import time
import uuid

from psycopg.types.json import Jsonb

from app import airbnb, database, gdrive, plans, store

ACTIVE = ('queued', 'running', 'uploading')
TERMINAL = ('done', 'failed', 'cancelled')
LOG_LINES = 80
AIRBNB = re.compile(r'^https?://(?:[a-z0-9-]+\.)?airbnb\.[a-z.]{2,12}/rooms/(?:plus/)?(\d{1,20})(?:[/?#].*)?$', re.I)
JOB_ID = re.compile(r'^[0-9a-f]{6,32}$')
REMOVED = "This listing has been removed from ReelSieve, so we can't make a reel of it."


class AdmissionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean(text, limit=200):
    """User-safe text for durable job records: no links, local paths or unbounded provider output."""
    t = re.sub(r'https?://\S+', '[link]', str(text or ''))
    t = re.sub(r'(?:/[^\s/]+){2,}/?', '[path]', t)
    return re.sub(r'\s+', ' ', t).strip()[:limit]


def listing_id(url):
    """The numeric id of an Airbnb /rooms/<id> link, else None."""
    m = AIRBNB.match((url or '').strip())
    return m.group(1) if m else None


def canonical_listing(url):
    lid = listing_id(url)
    if not lid:
        raise AdmissionError('Paste an Airbnb listing link (airbnb.…/rooms/<number>)')
    return 'https://www.airbnb.co.uk/rooms/' + lid


def _request_hash(url, params):
    return hashlib.sha256(json.dumps({'url': url, **params}, sort_keys=True).encode()).hexdigest()


def admit(user, url, requested, idempotency_key=None, ip=None):
    """Create a job for the signed-in owner. Same key + same input returns the original job."""
    url = canonical_listing(url)
    requested = {
        'ai_motion': bool(requested.get('ai_motion')),
        'style': 'v3' if requested.get('style') in ('tutorial', 'v3') else 'v2',
        'ai_resolution': '720p' if requested.get('ai_resolution') == '720p' else '1080p',
        'send_to_host': bool(requested.get('send_to_host', True)),
        'message': str(requested.get('message') or '')[:2000],
    }
    key = str(idempotency_key or '')[:120] or secrets.token_hex(16)
    digest = _request_hash(url, requested)
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    with database.connect() as c:
        owner = database.user_id(user, c)
        c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (f'job-admit:{owner}:{key}',))
        existing = c.execute('SELECT id,request_hash FROM jobs WHERE owner_id=%s AND idempotency_key=%s',
                             (owner, key)).fetchone()
        if existing:
            if existing['request_hash'] != digest:
                raise AdmissionError('That request key was already used for a different reel', 409)
            return get(user, existing['id'])
        if not airbnb.enabled():  # kill switch: refused before anything is charged
            raise AdmissionError(airbnb.DISABLED, 503)
        if store.blocked_ids([listing_id(url)], c):  # the host (or an admin) took this listing down
            raise AdmissionError(REMOVED, 403)
        generation = gdrive.usable_generation(c, owner)
        if generation is None:
            raise AdmissionError('Connect your Google Drive in Account first — finished reels are delivered there', 412)
        try:
            plans.reserve(user, url, job_id, ip, conn=c)
        except ValueError as e:
            raise AdmissionError(str(e), 402) from None
        plan = plans.PLANS.get(store.get_account(user, c)['plan'], plans.PLANS['free'])
        params = {**requested, 'plan': plan['key'], 'max_seconds': plan['max_seconds'],
                  'ai_motion': requested['ai_motion'] and bool(plan['ai_motion'])}
        c.execute('INSERT INTO jobs(id,owner_id,idempotency_key,request_hash,url,params,drive_generation,created,updated) '
                  'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (job_id, owner, key, digest, url, Jsonb(params), generation, now, now))
    return get(user, job_id)


def _owned(c, user, job_id, lock=False):
    if not JOB_ID.match(job_id or ''):
        return None
    return c.execute('SELECT j.* FROM jobs j JOIN users u ON u.id=j.owner_id WHERE j.id=%s AND u.email=%s AND u.active'
                     + (' FOR UPDATE OF j' if lock else ''), (job_id, (user or '').strip().lower())).fetchone()


def get(user, job_id):
    with database.connect() as c:
        return _owned(c, user, job_id)


def list_for(user, limit=50):
    with database.connect() as c:
        return c.execute('SELECT j.* FROM jobs j JOIN users u ON u.id=j.owner_id WHERE u.email=%s AND u.active '
                         'ORDER BY j.created DESC LIMIT %s', ((user or '').strip().lower(), limit)).fetchall()


def _refund(c, job):
    plans.refund(job['id'], conn=c)


def cancel(user, job_id):
    """Queued jobs stop at once and are refunded; rendering ones stop at the worker's next check.
    Once delivery to Drive has started the render is finished and paid for, so it is no longer cancellable."""
    with database.connect() as c:
        job = _owned(c, user, job_id, lock=True)
        if not job:
            return None
        if job['status'] == 'queued':
            c.execute("UPDATE jobs SET status='cancelled',cancel_requested=TRUE,step='Cancelled',finished_at=%s,updated=%s "
                      'WHERE id=%s', (time.time(), time.time(), job_id))
            _refund(c, job)
        elif job['status'] == 'running':
            c.execute('UPDATE jobs SET cancel_requested=TRUE,updated=%s WHERE id=%s', (time.time(), job_id))
        return _owned(c, user, job_id)


def cancel_owner(owner_id):
    """Account deactivation: stop everything the owner has in flight."""
    with database.connect() as c:
        queued = c.execute("UPDATE jobs SET status='cancelled',cancel_requested=TRUE,step='Cancelled',finished_at=%s,updated=%s "
                           "WHERE owner_id=%s AND status='queued' RETURNING id", (time.time(), time.time(), owner_id)).fetchall()
        for job in queued:
            _refund(c, job)
        c.execute("UPDATE jobs SET cancel_requested=TRUE,updated=%s WHERE owner_id=%s AND status='running'",
                  (time.time(), owner_id))


def set_meta(user, job_id, **values):
    """Owner-side annotations (host message state). Never touches lifecycle fields."""
    with database.connect() as c:
        job = _owned(c, user, job_id, lock=True)
        if not job:
            return None
        c.execute('UPDATE jobs SET meta=meta || %s,updated=%s WHERE id=%s', (Jsonb(values), time.time(), job_id))
        return _owned(c, user, job_id)


# ---------------- worker side: every write is fenced by the lease token ----------------

def claim(worker, lease_seconds):
    token, now = secrets.token_hex(16), time.time()
    with database.connect() as c:
        return c.execute(
            "UPDATE jobs SET status='running',lease_token=%s,lease_until=%s,worker=%s,step='Starting',updated=%s "
            'WHERE id=(SELECT j.id FROM jobs j JOIN users u ON u.id=j.owner_id '
            "WHERE j.status='queued' AND NOT j.cancel_requested AND u.active ORDER BY j.created "
            'FOR UPDATE OF j SKIP LOCKED LIMIT 1) '
            'RETURNING jobs.*,(SELECT email FROM users WHERE id=jobs.owner_id) AS owner_email',
            (token, now + lease_seconds, worker, now)).fetchone()


def heartbeat(job_id, token, lease_seconds):
    """False once the lease is lost or the owner asked to cancel: the worker must stop."""
    with database.connect() as c:
        row = c.execute("UPDATE jobs SET lease_until=%s WHERE id=%s AND lease_token=%s AND status IN ('running','uploading') "
                        'RETURNING cancel_requested', (time.time() + lease_seconds, job_id, token)).fetchone()
    return bool(row) and not row['cancel_requested']


def cancel_requested(job_id):
    with database.connect() as c:
        row = c.execute('SELECT cancel_requested FROM jobs WHERE id=%s', (job_id,)).fetchone()
    return bool(row and row['cancel_requested'])


def start_upload(job_id, token):
    """Rendering → delivery, atomically: refused if the owner cancelled first (cancel() locks the same row)."""
    with database.connect() as c:
        return bool(c.execute("UPDATE jobs SET status='uploading',step='Uploading to your Google Drive',"
                              'progress=GREATEST(progress,98),updated=%s WHERE id=%s AND lease_token=%s '
                              "AND status='running' AND NOT cancel_requested RETURNING id",
                              (time.time(), job_id, token)).fetchone())


def report(job_id, token, step=None, progress=None, line=None, meta=None):
    sets, args = ['updated=%s'], [time.time()]
    if step is not None:
        sets.append('step=%s')
        args.append(clean(step, 120))
    if progress is not None:
        sets.append('progress=GREATEST(progress,%s)')
        args.append(int(progress))
    if line is not None:
        sets.append("log=(CASE WHEN jsonb_array_length(log)>=%s THEN log-0 ELSE log END) || %s")
        args += [LOG_LINES, Jsonb([time.strftime('%H:%M:%S UTC ', time.gmtime()) + clean(line)])]
    if meta is not None:
        sets.append('meta=meta || %s')
        args.append(Jsonb(meta))
    with database.connect() as c:
        return bool(c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=%s AND lease_token=%s "
                              "AND status IN ('running','uploading') RETURNING id", (*args, job_id, token)).fetchone())


def finish(job_id, token, status, error=None, meta=None):
    """Terminal transition by the lease holder. Failed and cancelled jobs are refunded once."""
    assert status in TERMINAL
    now = time.time()
    with database.connect() as c:
        job = c.execute("UPDATE jobs SET status=%s,error=%s,meta=meta || %s,progress=CASE WHEN %s='done' THEN 100 ELSE progress END,"
                        "step=%s,lease_token=NULL,lease_until=NULL,finished_at=%s,updated=%s "
                        "WHERE id=%s AND lease_token=%s AND status IN ('running','uploading') RETURNING *",
                        (status, clean(error, 300) if error else None, Jsonb(meta or {}), status,
                         {'done': 'Done', 'failed': 'Failed', 'cancelled': 'Cancelled'}[status], now, now, job_id, token)).fetchone()
        if job and status != 'done':
            _refund(c, job)
    return bool(job)


def recover_stale():
    """Jobs whose worker vanished: fail and refund, never re-run (a paid generation may have happened)."""
    now = time.time()
    with database.connect() as c:
        rows = c.execute("UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,"
                         "error=CASE WHEN cancel_requested THEN NULL ELSE %s END,step='Stopped',"
                         'lease_token=NULL,lease_until=NULL,finished_at=%s,updated=%s '
                         "WHERE status IN ('running','uploading') AND lease_until<%s RETURNING *",
                         ('The server restarted while this reel was being made. It was not retried automatically and '
                          'nothing was charged — start it again.', now, now, now)).fetchall()
        for job in rows:
            _refund(c, job)
    return [r['id'] for r in rows]


def live_leases():
    with database.connect() as c:
        return {r['id'] for r in c.execute("SELECT id FROM jobs WHERE status IN ('running','uploading') AND lease_until>=%s",
                                           (time.time(),)).fetchall()}


def pending_cleanup(limit=50):
    with database.connect() as c:
        return [r['id'] for r in c.execute("SELECT id FROM jobs WHERE status IN ('done','failed','cancelled') "
                                           'AND cleanup_at IS NULL ORDER BY finished_at LIMIT %s', (limit,)).fetchall()]


def mark_cleaned(job_id, error=None):
    with database.connect() as c:
        c.execute('UPDATE jobs SET cleanup_at=%s,cleanup_error=%s WHERE id=%s',
                  (None if error else time.time(), clean(error, 200) if error else None, job_id))
