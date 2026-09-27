"""Daily backup of the invoice records to a bucket in India, and the same CSV for the accountant.

Income-tax Rules 2026 r.46(8): books "maintained in electronic mode shall remain accessible in India at all times, and
the back-up of such books ... shall be kept on a daily basis in servers physically located in India". The database is
not in India, so every worker calls backup() hourly; an advisory lock lets one replica work and a successful run per
UTC date makes the rest no-ops. Off until INVOICE_BACKUP_BUCKET, _ACCESS_KEY_ID and _SECRET_ACCESS_KEY are all set.

One PUT per day, signed with AWS Signature Version 4 using the standard library (no SDK). Keys and CSV contents are
never logged; run records hold the date, outcome, row count and an error summary written here, never a provider body.
"""
import csv
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from app import database

COLUMNS = ('ref', 'date', 'plan', 'amount', 'currency', 'provider', 'status', 'paid_at', 'billing_email')
INDIA_REGIONS = ('ap-south-1', 'ap-south-2')  # AWS Asia Pacific (Mumbai), (Hyderabad)
BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
REGION = re.compile(r'[a-z0-9-]{1,40}')
FORMULA = ('=', '+', '-', '@', '\t', '\r')  # a spreadsheet would run these as formulas


class BackupError(Exception):
    """A failed upload, with a message that is safe to store and show (no keys, no provider body)."""


def config():
    get = lambda k: (os.getenv(k) or '').strip()  # noqa: E731
    cfg = {'bucket': get('INVOICE_BACKUP_BUCKET'), 'region': get('INVOICE_BACKUP_REGION') or 'ap-south-1',
           'key_id': get('INVOICE_BACKUP_ACCESS_KEY_ID'), 'secret': get('INVOICE_BACKUP_SECRET_ACCESS_KEY'),
           'endpoint': get('INVOICE_BACKUP_ENDPOINT')}
    return cfg if cfg['bucket'] and cfg['key_id'] and cfg['secret'] else None


def lock_name():
    return 'reelsieve-invoice-backup-' + database.schema_name()


def object_key(day):
    return f'invoices/{day:%Y}/{day:%m}/{day:%Y-%m-%d}.csv'


# ---------- the records ----------

def paid_orders(conn=None):
    with database.transaction(conn) as c:
        return c.execute("SELECT ref,ts,plan,amount_usd,provider,status,paid_at,billing_email FROM orders "
                         "WHERE status='paid' ORDER BY paid_at,ref").fetchall()


def _utc(ts, fmt):
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt) if ts is not None else ''


def _cell(v):
    v = '' if v is None else str(v)
    return "'" + v if v.startswith(FORMULA) else v


def csv_text(rows):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([_cell(v) for v in (r['ref'], _utc(r['ts'], '%Y-%m-%d'), r['plan'], f"{float(r['amount_usd']):.2f}", 'USD',
                                       r['provider'], r['status'], _utc(r['paid_at'], '%Y-%m-%dT%H:%M:%SZ'), r['billing_email'])])
    return b.getvalue()


def export():
    """The CSV and its row count, for the admin download."""
    rows = paid_orders()
    return csv_text(rows), len(rows)


# ---------- AWS Signature Version 4 ----------
# docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html (fetched 27 Sep 2026).

def authorization(method, path, headers, payload_hash, key_id, secret, region, service='s3'):
    """Authorization header for a request with no query string. `headers` must hold Host and X-Amz-Date; every header
    given is signed. S3 paths are URI-encoded once, '/' kept."""
    h = {k.lower(): ' '.join(str(v).split()) for k, v in headers.items()}
    names = sorted(h)
    signed = ';'.join(names)
    canonical = '\n'.join([method, quote(path, safe='/~'), '', ''.join(f'{k}:{h[k]}\n' for k in names), signed, payload_hash])
    amz_date = h['x-amz-date']
    scope = f'{amz_date[:8]}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ('AWS4' + secret).encode()
    for part in (amz_date[:8], region, service, 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f'AWS4-HMAC-SHA256 Credential={key_id}/{scope},SignedHeaders={signed},Signature={signature}'


def _target(cfg, key):
    """(host, path) for the object: virtual-hosted on AWS, path-style on another S3-compatible endpoint."""
    if not BUCKET.fullmatch(cfg['bucket']):
        raise BackupError('INVOICE_BACKUP_BUCKET is not a valid bucket name')
    if not REGION.fullmatch(cfg['region']):
        raise BackupError('INVOICE_BACKUP_REGION is not a valid region name')
    if not cfg['endpoint']:
        if cfg['region'] not in INDIA_REGIONS:
            raise BackupError('INVOICE_BACKUP_REGION must be an AWS region in India: ap-south-1 (Mumbai) or ap-south-2 (Hyderabad)')
        return f"{cfg['bucket']}.s3.{cfg['region']}.amazonaws.com", '/' + key
    u = urlsplit(cfg['endpoint'])
    if u.scheme != 'https' or not u.netloc or u.query or u.fragment or '@' in u.netloc:
        raise BackupError('INVOICE_BACKUP_ENDPOINT must be an https:// address')
    return u.netloc, f"{u.path.rstrip('/')}/{cfg['bucket']}/{key}"


def put(cfg, key, body, now):
    host, path = _target(cfg, key)
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {'Host': host, 'Content-Type': 'text/csv; charset=utf-8', 'x-amz-content-sha256': payload_hash,
               'x-amz-date': _utc(now, '%Y%m%dT%H%M%SZ'), 'x-amz-server-side-encryption': 'AES256'}
    headers['Authorization'] = authorization('PUT', path, headers, payload_hash, cfg['key_id'], cfg['secret'], cfg['region'])
    try:
        with httpx.Client(timeout=30) as h:
            r = h.put(f'https://{host}{quote(path, safe="/~")}', content=body, headers=headers)
    except httpx.HTTPError as e:
        raise BackupError(f'Could not reach the bucket ({type(e).__name__})') from None
    if r.status_code != 200:
        # Only the error code: an S3 error body can echo the access key ID and the string to sign.
        code = re.search(r'<Code>([A-Za-z]{1,64})</Code>', r.text or '')
        raise BackupError(f'The bucket refused the upload (HTTP {r.status_code}' + (f', {code.group(1)})' if code else ')'))


# ---------- the daily job ----------

def backup(now=None):
    """Upload today's CSV (UTC date) unless it is already done. Returns None (off), 'locked', 'done', 'ok' or 'failed'."""
    cfg = config()
    if not cfg:
        return None
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).date()
    # ponytail: the lock is held in a transaction across one PUT (30 s timeout at most); fine for one small file a day.
    with database.connect() as c:
        if not c.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok', (lock_name(),)).fetchone()['ok']:
            return 'locked'
        if c.execute("SELECT 1 FROM invoice_backups WHERE day=%s AND status='ok'", (day,)).fetchone():
            return 'done'
        rows = paid_orders(c)
        try:
            put(cfg, object_key(day), csv_text(rows).encode(), now)
            status, error = 'ok', None
        except BackupError as e:
            status, error = 'failed', str(e)
        c.execute('INSERT INTO invoice_backups(day,ts,status,row_count,error) VALUES(%s,%s,%s,%s,%s)',
                  (day, now, status, len(rows), error))
    print(json.dumps({'invoice_backup': {'day': day.isoformat(), 'status': status, 'rows': len(rows)}}), flush=True)
    return status


def status():
    """What Settings shows: whether it is on, the last successful backup and the last error."""
    with database.connect() as c:
        last = lambda s: c.execute('SELECT day,ts,row_count,error FROM invoice_backups WHERE status=%s '  # noqa: E731
                                   'ORDER BY ts DESC,id DESC LIMIT 1', (s,)).fetchone()
        ok, failed = last('ok'), last('failed')
    if ok:
        ok['key'] = object_key(ok['day'])
    return {'enabled': config() is not None, 'last_ok': ok, 'last_error': failed}
