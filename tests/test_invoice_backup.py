"""Daily India backup of paid orders (Income-tax Rules 2026 r.46(8)) against isolated PostgreSQL.
S3 is a synthetic fake behind HTTPX MockTransport; nothing leaves the machine and no real keys exist."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import os

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app import auth, billing, invoices, retention, server

KEY_ID, SECRET = 'AKIASYNTHETIC0000KEY', 'synthetic/secret+never-logged-0000000000'
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400
HEADER = ['ref', 'date', 'plan', 'amount', 'currency', 'provider', 'status', 'paid_at', 'billing_email']


class S3:
    def __init__(self):
        self.calls, self.status, self.body, self.error = [], 200, '', None

    def handle(self, req):
        self.calls.append(req)
        if self.error:
            raise self.error
        return httpx.Response(self.status, text=self.body)


@pytest.fixture
def s3(monkeypatch):
    fake = S3()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    return fake


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('INVOICE_BACKUP_BUCKET', 'braivex-books-test')
    monkeypatch.setenv('INVOICE_BACKUP_ACCESS_KEY_ID', KEY_ID)
    monkeypatch.setenv('INVOICE_BACKUP_SECRET_ACCESS_KEY', SECRET)
    monkeypatch.delenv('INVOICE_BACKUP_REGION', raising=False)
    monkeypatch.delenv('INVOICE_BACKUP_ENDPOINT', raising=False)


def paid(email, plan='starter'):
    if email not in {u['user'] for u in auth.users()}:
        auth.create_user(email, 'synthetic-password')
    return billing.settle(billing.create_order(email, plan)['ref'])


def runs(db):
    with db.connect() as c:
        return c.execute('SELECT day,status,row_count,error FROM invoice_backups ORDER BY id').fetchall()


# ---------------- AWS Signature Version 4: known answers published by AWS ----------------

def test_sigv4_matches_aws_s3_put_object_example():
    """AWS S3 API Reference, "Signature Calculations for the Authorization Header: Transferring Payload in a Single
    Chunk", Example: PUT Object (docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html)."""
    payload = hashlib.sha256(b'Welcome to Amazon S3.').hexdigest()
    assert payload == '44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072'
    headers = {'Host': 'examplebucket.s3.amazonaws.com', 'Date': 'Fri, 24 May 2013 00:00:00 GMT',
               'x-amz-date': '20130524T000000Z', 'x-amz-storage-class': 'REDUCED_REDUNDANCY', 'x-amz-content-sha256': payload}
    assert invoices.authorization('PUT', '/test$file.text', headers, payload, 'AKIAIOSFODNN7EXAMPLE',
                                  'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'us-east-1') == (
        'AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,'
        'SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class,'
        'Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd')


def test_sigv4_matches_aws_signing_test_suite_signed_body():
    """AWS SigV4 test suite, case post-x-www-form-urlencoded (awslabs/aws-c-auth tests/aws-signing-test-suite/v4)."""
    payload = hashlib.sha256(b'Param1=value1').hexdigest()
    headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Host': 'example.amazonaws.com', 'Content-Length': '13',
               'X-Amz-Date': '20150830T123600Z', 'x-amz-content-sha256': payload}
    auth_header = invoices.authorization('POST', '/', headers, payload, 'AKIDEXAMPLE',
                                         'wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY', 'us-east-1', service='service')
    assert auth_header.endswith('SignedHeaders=content-length;content-type;host;x-amz-content-sha256;x-amz-date,'
                                'Signature=d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b')


# ---------------- the daily job ----------------

def test_backup_uploads_encrypted_csv_to_indian_bucket(db, env, s3, capsys):
    o = paid('payer@example.test')
    assert invoices.backup(NOW) == 'ok'
    [req] = s3.calls
    assert req.method == 'PUT'
    assert str(req.url) == 'https://braivex-books-test.s3.ap-south-1.amazonaws.com/invoices/2026/09/2026-09-27.csv'
    assert req.headers['x-amz-server-side-encryption'] == 'AES256'
    assert req.headers['x-amz-content-sha256'] == hashlib.sha256(req.content).hexdigest()
    assert req.headers['x-amz-date'] == '20260927T120000Z'
    signed = {k: v for k, v in req.headers.items() if k in ('host', 'content-type') or k.startswith('x-amz-')}
    assert req.headers['authorization'] == invoices.authorization(
        'PUT', req.url.path, signed, req.headers['x-amz-content-sha256'], KEY_ID, SECRET, 'ap-south-1')
    assert req.headers['authorization'].startswith(f'AWS4-HMAC-SHA256 Credential={KEY_ID}/20260927/ap-south-1/s3/aws4_request,')
    rows = list(csv.reader(io.StringIO(req.content.decode())))
    assert rows[0] == HEADER and rows[1][0] == o['ref'] and rows[1][-1] == 'payer@example.test' and len(rows) == 2
    assert [dict(r) for r in runs(db)] == [{'day': datetime(2026, 9, 27).date(), 'status': 'ok', 'row_count': 1, 'error': None}]
    out = capsys.readouterr().out
    assert '"status": "ok"' in out and SECRET not in out and KEY_ID not in out and 'payer@example.test' not in out
    assert SECRET not in str(req.headers)


def test_backup_runs_once_per_utc_day(db, env, s3):
    paid('payer@example.test')
    assert invoices.backup(NOW) == 'ok'
    assert invoices.backup(NOW + 3600) == 'done'  # the next hourly tick on either replica
    assert invoices.backup(NOW + 11 * 3600) == 'done'  # 23:00 UTC, same day
    assert invoices.backup(NOW + 12 * 3600) == 'ok'  # 00:00 UTC next day
    assert [str(r.url).rsplit('/', 1)[1] for r in s3.calls] == ['2026-09-27.csv', '2026-09-28.csv']
    with db.connect() as c:
        with pytest.raises(psycopg.errors.UniqueViolation):  # the database itself allows one success per day
            c.execute("INSERT INTO invoice_backups(day,ts,status,row_count) VALUES('2026-09-27',1,'ok',0)")


def test_refused_upload_is_recorded_without_secrets_and_retried_next_hour(db, env, s3):
    paid('payer@example.test')
    s3.status = 403
    s3.body = (f'<?xml version="1.0"?><Error><Code>SignatureDoesNotMatch</Code><Message>no</Message>'
               f'<AWSAccessKeyId>{KEY_ID}</AWSAccessKeyId><StringToSign>AWS4-HMAC-SHA256 secret-ish</StringToSign></Error>')
    assert invoices.backup(NOW) == 'failed'
    [r] = runs(db)
    assert r['status'] == 'failed' and r['row_count'] == 1
    assert r['error'] == 'The bucket refused the upload (HTTP 403, SignatureDoesNotMatch)'
    s3.status, s3.body = 200, ''
    assert invoices.backup(NOW + 3600) == 'ok'
    assert [x['status'] for x in runs(db)] == ['failed', 'ok'] and len(s3.calls) == 2


def test_network_error_is_recorded(db, env, s3):
    s3.error = httpx.ConnectError(f'cannot connect with {SECRET}')
    assert invoices.backup(NOW) == 'failed'
    [r] = runs(db)
    assert r['error'] == 'Could not reach the bucket (ConnectError)' and r['row_count'] == 0


def test_backup_skips_while_another_worker_holds_the_lock(db, env, s3):
    with psycopg.connect(os.environ['DATABASE_URL']) as other:
        other.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (invoices.lock_name(),))
        assert invoices.backup(NOW) == 'locked'
    assert s3.calls == [] and runs(db) == []
    assert invoices.backup(NOW) == 'ok'  # the lock is gone with the other transaction


@pytest.mark.parametrize('missing', ['INVOICE_BACKUP_BUCKET', 'INVOICE_BACKUP_ACCESS_KEY_ID', 'INVOICE_BACKUP_SECRET_ACCESS_KEY'])
def test_backup_is_off_until_every_variable_is_set(db, env, s3, monkeypatch, missing):
    monkeypatch.setenv(missing, '  ')
    assert invoices.config() is None
    assert invoices.backup(NOW) is None
    assert s3.calls == [] and runs(db) == []


def test_aws_region_outside_india_is_refused(db, env, s3, monkeypatch):
    monkeypatch.setenv('INVOICE_BACKUP_REGION', 'us-east-1')
    assert invoices.backup(NOW) == 'failed'
    assert s3.calls == []
    assert 'India' in runs(db)[0]['error']
    monkeypatch.setenv('INVOICE_BACKUP_REGION', 'ap-south-2')  # Hyderabad
    assert invoices.backup(NOW + 3600) == 'ok'
    assert s3.calls[0].url.host == 'braivex-books-test.s3.ap-south-2.amazonaws.com'


def test_custom_endpoint_is_path_style_and_https_only(db, env, s3, monkeypatch):
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', 'http://s3.india-store.test')
    assert invoices.backup(NOW) == 'failed'
    assert 'https' in runs(db)[0]['error'] and s3.calls == []
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', 'https://s3.india-store.test/')
    monkeypatch.setenv('INVOICE_BACKUP_REGION', 'in-mum-1')
    assert invoices.backup(NOW + 3600) == 'ok'
    assert str(s3.calls[0].url) == 'https://s3.india-store.test/braivex-books-test/invoices/2026/09/2026-09-27.csv'
    assert '/in-mum-1/s3/aws4_request' in s3.calls[0].headers['authorization']


def test_csv_content_escaping_and_formula_guard():
    rows = [{'ref': 'RS-260927-ABC123', 'ts': NOW - 60, 'plan': 'starter', 'amount_usd': 100.0, 'provider': 'invoice',
             'status': 'paid', 'paid_at': NOW, 'billing_email': 'a,"quoted"@example.test'},
            {'ref': 'RS-260927-DEF456', 'ts': NOW, 'plan': 'commercial', 'amount_usd': 500, 'provider': None,
             'status': 'paid', 'paid_at': NOW + 1, 'billing_email': '=HYPERLINK("http://x.test")@example.test'},
            {'ref': 'RS-260927-GHI789', 'ts': NOW, 'plan': 'starter', 'amount_usd': 100, 'provider': 'stripe',
             'status': 'paid', 'paid_at': NOW + 2, 'billing_email': None}]
    text = invoices.csv_text(rows)
    assert text.splitlines()[1] == ('RS-260927-ABC123,2026-09-27,starter,100.00,USD,invoice,paid,2026-09-27T12:00:00Z,'
                                    '"a,""quoted""@example.test"')
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == HEADER
    assert parsed[1][-1] == 'a,"quoted"@example.test'
    assert parsed[2][3] == '500.00' and parsed[2][5] == '' and parsed[2][-1] == '\'=HYPERLINK("http://x.test")@example.test'
    assert parsed[3][-1] == ''


def test_only_paid_orders_are_exported(db):
    o = paid('payer@example.test')
    auth.create_user('other@example.test', 'synthetic-password')
    billing.create_order('other@example.test', 'starter')  # pending
    billing.cancel(billing.create_order('other@example.test', 'starter')['ref'])
    text, n = invoices.export()
    assert n == 1 and o['ref'] in text and 'other@example.test' not in text


def test_retention_deletes_backup_records_after_two_years(db):
    with db.connect() as c:
        c.execute("INSERT INTO invoice_backups(day,ts,status,row_count) VALUES('2024-01-01',%s,'ok',1),('2025-01-01',%s,'failed',0)",
                  (NOW - (2 * 365.25 + 1) * DAY, NOW - 365 * DAY))
    assert retention.run(NOW)['invoice_backups'] == 1
    assert [r['day'].isoformat() for r in runs(db)] == ['2025-01-01']


def test_worker_runs_backup_hourly_even_if_retention_fails(db, monkeypatch, tmp_path):
    from app import worker
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path))
    calls = []

    def broken(*a):
        raise RuntimeError('retention broke')
    monkeypatch.setattr(retention, 'run', broken)
    monkeypatch.setattr(invoices, 'backup', lambda: calls.append(1))
    monkeypatch.setattr(worker, '_last_purge', [0.0])
    worker.run_once('w')
    worker.run_once('w')
    assert calls == [1]


# ---------------- admin: status and on-demand download ----------------

ADMIN = 'operator@example.test'


@pytest.fixture
def web(owners):
    auth.create_user(ADMIN, 'operator-password', 'admin')
    clients = {}
    for name, tok in {**owners, 'admin': auth.issue(ADMIN)[0]}.items():
        c = TestClient(server.app)
        c.__enter__()
        c.cookies.set(auth.COOKIE, tok)
        clients[name] = c
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def test_admin_downloads_the_same_csv(web, db):
    o = paid('alice@example.test')
    r = web['admin'].get('/api/invoices/export.csv')
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/csv')
    assert r.headers['content-disposition'].startswith('attachment; filename="reelsieve-invoices-')
    assert r.headers['cache-control'] == 'no-store'
    assert r.text == invoices.export()[0] and o['ref'] in r.text
    with db.connect() as c:
        assert c.execute("SELECT detail FROM admin_events WHERE action='invoice_export'").fetchone()['detail'] == {'rows': 1}
    assert web['alice'].get('/api/invoices/export.csv').status_code == 403


def test_settings_shows_backup_status_and_keeps_secrets_out(web, db, env, s3):
    page = web['admin'].get('/settings').text
    assert 'Invoice backup (India)' in page and 'No backup has run yet' in page
    for k in ('INVOICE_BACKUP_BUCKET', 'INVOICE_BACKUP_REGION', 'INVOICE_BACKUP_ACCESS_KEY_ID',
              'INVOICE_BACKUP_SECRET_ACCESS_KEY', 'INVOICE_BACKUP_ENDPOINT'):
        assert k in page
    assert SECRET not in page and KEY_ID not in page and 'braivex-books-test' in page
    s3.status = 403
    invoices.backup(NOW - DAY)
    s3.status = 200
    invoices.backup(NOW)
    page = web['admin'].get('/settings').text
    assert 'invoices/2026/09/2026-09-27.csv' in page and 'The bucket refused the upload (HTTP 403)' in page
    api = web['admin'].get('/api/settings').json()
    assert api['invoice_backup_secret_access_key'] == {'configured': True}
