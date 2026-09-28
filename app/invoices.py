"""Daily backup of the invoice records to a bucket in India, and the same CSV for the accountant.

Income-tax Rules 2026 r.46(8): books "maintained in electronic mode shall remain accessible in India at all times, and
the back-up of such books ... shall be kept on a daily basis in servers physically located in India". The database is
not in India, so every worker calls backup() hourly; an advisory lock lets one replica work and a successful run per
UTC date makes the rest no-ops. Off until INVOICE_BACKUP_BUCKET, _ACCESS_KEY_ID and _SECRET_ACCESS_KEY are all set on
the worker service.

Each run first reads the bucket's lifecycle rules and uploads only if a rule deletes the file within EXPIRY_MAX_DAYS
(the privacy notice promises it), then sends one PUT with If-None-Match: * so a stored day is never replaced. Signed
with AWS Signature Version 4 using the standard library (no SDK). Keys and CSV contents are never logged; run records
hold the date, outcome, row count, bucket host, expiry days and an error summary written here, never a provider body.
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
import xml.etree.ElementTree as ET  # no external entities; any DTD is refused before parsing (expiry_days)

import httpx

from app import database

COLUMNS = ('ref', 'date', 'plan', 'amount', 'currency', 'provider', 'status', 'paid_at', 'billing_email')
INDIA_REGIONS = ('ap-south-1', 'ap-south-2')  # AWS Asia Pacific (Mumbai), (Hyderabad)
BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
REGION = re.compile(r'[a-z0-9-]{1,40}')
EXPIRY_MAX_DAYS = 90  # the privacy notice: backup copies are deleted 90 days after each copy is made
STALE_SECONDS = 26 * 3600  # a healthy worker tries at least once per UTC day, give or take its hourly tick
# OWASP CSV injection: = + - @ tab CR LF and their full-width forms start a formula, at the start of the cell or of
# any part a spreadsheet may split off at a separator (',' ';' tab or a line break).
FORMULA = re.compile('(^|[,;\t\r\n])(?=[=+\\-@\t\r\n＝＋－＠])')


class BackupError(Exception):
    """A failed upload, with a message that is safe to store and show (no keys, no provider body)."""


def config():
    get = lambda k: (os.getenv(k) or '').strip()  # noqa: E731
    cfg = {'bucket': get('INVOICE_BACKUP_BUCKET'), 'region': get('INVOICE_BACKUP_REGION') or 'ap-south-1',
           'key_id': get('INVOICE_BACKUP_ACCESS_KEY_ID'), 'secret': get('INVOICE_BACKUP_SECRET_ACCESS_KEY'),
           'endpoint': get('INVOICE_BACKUP_ENDPOINT'), 'endpoint_in_india': get('INVOICE_BACKUP_ENDPOINT_IN_INDIA') == '1'}
    return cfg if cfg['bucket'] and cfg['key_id'] and cfg['secret'] else None


def lock_name():
    return 'reelsieve-invoice-backup-' + database.schema_name()


def object_key(day):
    """invoices/YYYY/MM/YYYY-MM-DD.csv for the production schema; another schema (a staging service sharing the bucket)
    writes under invoices/<schema>/ so it never takes production's key for the day."""
    schema = database.schema_name()
    return f"invoices/{'' if schema == 'public' else schema + '/'}{day:%Y}/{day:%m}/{day:%Y-%m-%d}.csv"


# ---------- the records ----------

def paid_orders(conn=None):
    with database.transaction(conn) as c:
        return c.execute("SELECT ref,ts,plan,amount_usd,provider,status,paid_at,billing_email FROM orders "
                         "WHERE status='paid' ORDER BY paid_at,ref").fetchall()


def _utc(ts, fmt):
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt) if ts is not None else ''


def csv_cell(v):
    """One CSV cell no spreadsheet will run as a formula (also used by the outreach export, app/linkedin.py)."""
    return FORMULA.sub(r"\1'", '' if v is None else str(v))


def csv_text(rows):
    b = io.StringIO()
    w = csv.writer(b, quoting=csv.QUOTE_ALL)  # quoted, so a ';' in an email never splits it into two cells
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([csv_cell(v) for v in (r['ref'], _utc(r['ts'], '%Y-%m-%d'), r['plan'], f"{float(r['amount_usd']):.2f}", 'USD',
                                       r['provider'], r['status'], _utc(r['paid_at'], '%Y-%m-%dT%H:%M:%SZ'), r['billing_email'])])
    return b.getvalue()


def export():
    """The CSV and its row count, for the admin download."""
    rows = paid_orders()
    return csv_text(rows), len(rows)


# ---------- AWS Signature Version 4 ----------
# docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html (fetched 27 Sep 2026).

def authorization(method, path, headers, payload_hash, key_id, secret, region, service='s3', query=''):
    """Authorization header. `headers` must hold Host and X-Amz-Date; every header given is signed. S3 paths are
    URI-encoded once, '/' kept. `query` is the canonical query string (e.g. 'lifecycle=')."""
    h = {k.lower(): ' '.join(str(v).split()) for k, v in headers.items()}
    names = sorted(h)
    signed = ';'.join(names)
    canonical = '\n'.join([method, quote(path, safe='/~'), query, ''.join(f'{k}:{h[k]}\n' for k in names), signed, payload_hash])
    amz_date = h['x-amz-date']
    scope = f'{amz_date[:8]}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ('AWS4' + secret).encode()
    for part in (amz_date[:8], region, service, 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f'AWS4-HMAC-SHA256 Credential={key_id}/{scope},SignedHeaders={signed},Signature={signature}'


def _target(cfg):
    """(host, bucket path) with the bucket's servers in India: virtual-hosted on AWS (bucket path ''), path-style on an
    explicit endpoint. An AWS endpoint must name an India region; any other endpoint needs the admin's confirmation."""
    if not BUCKET.fullmatch(cfg['bucket']):
        raise BackupError('INVOICE_BACKUP_BUCKET is not a valid bucket name')
    if not REGION.fullmatch(cfg['region']):
        raise BackupError('INVOICE_BACKUP_REGION is not a valid region name')
    in_india = 'INVOICE_BACKUP_REGION must be an AWS region in India: ap-south-1 (Mumbai) or ap-south-2 (Hyderabad)'
    if not cfg['endpoint']:
        if cfg['region'] not in INDIA_REGIONS:
            raise BackupError(in_india)
        if '.' in cfg['bucket']:  # AWS's *.s3.<region>.amazonaws.com certificate does not cover a dotted name
            raise BackupError('INVOICE_BACKUP_BUCKET has a dot, which AWS cannot serve over HTTPS as <bucket>.s3.<region>'
                              '.amazonaws.com: use a name without dots, or set INVOICE_BACKUP_ENDPOINT to '
                              'https://s3.<region>.amazonaws.com')
        return f"{cfg['bucket']}.s3.{cfg['region']}.amazonaws.com", ''
    u = urlsplit(cfg['endpoint'])
    if u.scheme != 'https' or not u.netloc or u.query or u.fragment or '@' in u.netloc:
        raise BackupError('INVOICE_BACKUP_ENDPOINT must be an https:// address')
    host = (u.hostname or '').rstrip('.')
    if 'amazonaws' in host:  # AWS: the host itself must name the India region the request is signed for
        if not host.endswith('.amazonaws.com') or cfg['region'] not in INDIA_REGIONS or cfg['region'] not in host.split('.'):
            raise BackupError(in_india + ', and INVOICE_BACKUP_ENDPOINT must name it, e.g. https://s3.ap-south-1.amazonaws.com')
    elif not cfg['endpoint_in_india']:
        raise BackupError('The app cannot check where a non-AWS INVOICE_BACKUP_ENDPOINT keeps files: set '
                          'INVOICE_BACKUP_ENDPOINT_IN_INDIA=1 once you have confirmed its servers are in India')
    return u.netloc, f"{u.path.rstrip('/')}/{cfg['bucket']}"


def _send(cfg, method, host, path, now, subresource='', body=b'', extra=None):
    """One signed request; `subresource` is a bare query word such as 'lifecycle'."""
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {'Host': host, 'x-amz-content-sha256': payload_hash, 'x-amz-date': _utc(now, '%Y%m%dT%H%M%SZ'), **(extra or {})}
    headers['Authorization'] = authorization(method, path, headers, payload_hash, cfg['key_id'], cfg['secret'], cfg['region'],
                                             query=f'{subresource}=' if subresource else '')
    url = f'https://{host}{quote(path, safe="/~")}' + (f'?{subresource}' if subresource else '')
    try:
        with httpx.Client(timeout=30) as h:
            return h.request(method, url, content=body or None, headers=headers)
    except httpx.HTTPError as e:
        raise BackupError(f'Could not reach the bucket ({type(e).__name__})') from None


def _code(r):
    """The S3 error code only: an S3 error body can echo the access key ID and the string to sign."""
    code = re.search(r'<Code>([A-Za-z]{1,64})</Code>', r.text or '')
    return f'HTTP {r.status_code}' + (f', {code.group(1)}' if code else '')


def _local(el):
    return el.tag.rsplit('}', 1)[-1]


def _kids(el, name):
    return [k for k in el if _local(k) == name] if el is not None else []


def _text(el, name):
    k = _kids(el, name)
    return (k[0].text or '').strip() if k else None


def expiry_days(xml, key):
    """Days until the bucket's lifecycle deletes `key`: the shortest Expiration of an enabled rule whose filter is a
    key prefix only (a tag or size filter leaves some files out), or None."""
    if b'<!DOCTYPE' in xml.upper():  # S3 never sends a DTD; refusing one rules out entity tricks whatever the parser
        raise ET.ParseError('DTD')
    best = None
    for rule in _kids(ET.fromstring(xml), 'Rule'):
        f = _kids(rule, 'Filter')
        if f and any(_local(k) != 'Prefix' for k in f[0]):
            continue
        prefix = _text(f[0], 'Prefix') if f else _text(rule, 'Prefix')  # a Prefix outside Filter: the older form
        exp = _kids(rule, 'Expiration')
        days = _text(exp[0], 'Days') if exp else None
        if _text(rule, 'Status') == 'Enabled' and key.startswith(prefix or '') and days and days.isdigit():
            best = int(days) if best is None else min(best, int(days))
    return best


def check_expiry(cfg, host, bucket_path, key, now):
    """The lifecycle rule's day count, or BackupError if nothing deletes the file within EXPIRY_MAX_DAYS.
    ponytail: bucket versioning is not read (it would need s3:GetBucketVersioning); keep it off, as the setup says,
    or an expired file lives on as a noncurrent version."""
    r = _send(cfg, 'GET', host, bucket_path or '/', now, subresource='lifecycle')
    none = f'The bucket has no lifecycle rule that deletes files under invoices/ within {EXPIRY_MAX_DAYS} days'
    if r.status_code == 404:
        raise BackupError(none)
    if r.status_code != 200:
        raise BackupError(f'The bucket refused to show its lifecycle rules ({_code(r)}); '
                          'the IAM user needs s3:GetLifecycleConfiguration')
    try:
        days = expiry_days(r.content, key)
    except ET.ParseError:
        raise BackupError('The bucket sent lifecycle rules the app could not read') from None
    if days is None or days > EXPIRY_MAX_DAYS:
        raise BackupError(none + (f' (the rule keeps them {days} days)' if days else ''))
    return days


def put(cfg, host, path, body, now):
    r = _send(cfg, 'PUT', host, path, now, body=body,
              extra={'Content-Type': 'text/csv; charset=utf-8', 'x-amz-server-side-encryption': 'AES256', 'If-None-Match': '*'})
    if r.status_code == 412:  # If-None-Match: someone already stored this day; it is never overwritten
        raise BackupError('A file for this day is already in the bucket and was not replaced (HTTP 412). '
                          'If an earlier run did not write it, check the bucket.')
    if r.status_code != 200:
        raise BackupError(f'The bucket refused the upload ({_code(r)})')


# ---------- the daily job ----------

def backup(now=None):
    """Upload today's CSV (UTC date) unless it is already done. Returns None (off), 'locked', 'done', 'ok' or 'failed'."""
    cfg = config()
    if not cfg:
        return None
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).date()
    # ponytail: the lock is held in a transaction across two requests (30 s timeout each); fine for one small file a day.
    with database.connect() as c:
        if not c.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok', (lock_name(),)).fetchone()['ok']:
            return 'locked'
        if c.execute("SELECT 1 FROM invoice_backups WHERE day=%s AND status='ok'", (day,)).fetchone():
            return 'done'
        rows, key, host, days = paid_orders(c), object_key(day), None, None
        try:
            host, bucket_path = _target(cfg)
            days = check_expiry(cfg, host, bucket_path, key, now)
            put(cfg, host, f'{bucket_path}/{key}', csv_text(rows).encode(), now)
            status, error = 'ok', None
        except BackupError as e:
            status, error = 'failed', str(e)
        except Exception as e:  # a bad endpoint port, a non-ASCII key: recorded by class only (its text may hold a key)
            status, error = 'failed', f'Could not send the request to the bucket ({type(e).__name__})'
        c.execute('INSERT INTO invoice_backups(day,ts,status,row_count,error,host,expiry_days) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  (day, now, status, len(rows), error, host, days))
    print(json.dumps({'invoice_backup': {'day': day.isoformat(), 'status': status, 'rows': len(rows)}}), flush=True)
    return status


def status(now=None):
    """What Settings shows. The variables live on the worker service, so the state comes from the run records, never
    from this (web) process's environment: 'off' (never ran), 'on', 'failing' (tries, but no success for 26 hours) or
    'stopped' (no try for 26 hours)."""
    now = time.time() if now is None else now
    with database.connect() as c:
        last = lambda s: c.execute('SELECT day,ts,row_count,error,host,expiry_days FROM invoice_backups '  # noqa: E731
                                   'WHERE status=%s ORDER BY ts DESC,id DESC LIMIT 1', (s,)).fetchone()
        ok, failed = last('ok'), last('failed')
    if ok:
        ok['key'] = object_key(ok['day'])
    latest = max((r['ts'] for r in (ok, failed) if r), default=None)
    state = ('off' if latest is None else 'stopped' if now - latest > STALE_SECONDS
             else 'on' if ok and now - ok['ts'] <= STALE_SECONDS else 'failing')
    return {'state': state, 'last_ok': ok, 'last_error': failed}
