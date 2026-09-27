"""Daily India backup of paid orders (Income-tax Rules 2026 r.46(8)) against isolated PostgreSQL.
S3 is a synthetic fake behind HTTPX MockTransport; nothing leaves the machine and no real keys exist."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import os
import time

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app import auth, billing, invoices, retention, server

KEY_ID, SECRET = 'AKIASYNTHETIC0000KEY', 'synthetic/secret+never-logged-0000000000'
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400
HEADER = ['ref', 'date', 'plan', 'amount', 'currency', 'provider', 'status', 'paid_at', 'billing_email']


XMLNS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def rule(filter_xml='<Filter><Prefix>invoices/</Prefix></Filter>', days='90', status='Enabled', expiration=None):
    return (f'<Rule><ID>expire</ID>{filter_xml}<Status>{status}</Status>'
            f'{expiration or f"<Expiration><Days>{days}</Days></Expiration>"}</Rule>')


def lifecycle(*rules):
    return f'<?xml version="1.0" encoding="UTF-8"?><LifecycleConfiguration {XMLNS}>{"".join(rules)}</LifecycleConfiguration>'


class S3:
    """PUT Object answers `status`/`body`; GET ?lifecycle answers `lifecycle_status`/`lifecycle` (a 90-day rule)."""
    def __init__(self):
        self.calls, self.gets, self.status, self.body, self.error = [], [], 200, '', None
        self.lifecycle_status, self.lifecycle = 200, lifecycle(rule())

    def handle(self, req):
        (self.gets if req.method == 'GET' else self.calls).append(req)
        if self.error:
            raise self.error
        if req.method == 'GET':
            return httpx.Response(self.lifecycle_status, text=self.lifecycle)
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
    monkeypatch.delenv('INVOICE_BACKUP_ENDPOINT_IN_INDIA', raising=False)


def schema():
    return os.environ['DATABASE_SCHEMA']


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
    assert str(req.url) == f'https://braivex-books-test.s3.ap-south-1.amazonaws.com/invoices/{schema()}/2026/09/2026-09-27.csv'
    assert req.headers['x-amz-server-side-encryption'] == 'AES256'
    assert req.headers['if-none-match'] == '*'  # never replaces a stored day
    assert req.headers['x-amz-content-sha256'] == hashlib.sha256(req.content).hexdigest()
    assert req.headers['x-amz-date'] == '20260927T120000Z'
    signed = {k: v for k, v in req.headers.items() if k in ('host', 'content-type', 'if-none-match') or k.startswith('x-amz-')}
    assert req.headers['authorization'] == invoices.authorization(
        'PUT', req.url.path, signed, req.headers['x-amz-content-sha256'], KEY_ID, SECRET, 'ap-south-1')
    assert 'if-none-match;' in req.headers['authorization']
    [get] = s3.gets  # the lifecycle rule is checked before anything is uploaded
    assert str(get.url) == 'https://braivex-books-test.s3.ap-south-1.amazonaws.com/?lifecycle'
    got = {k: v for k, v in get.headers.items() if k == 'host' or k.startswith('x-amz-')}
    assert get.headers['authorization'] == invoices.authorization(
        'GET', '/', got, hashlib.sha256(b'').hexdigest(), KEY_ID, SECRET, 'ap-south-1', query='lifecycle=')
    assert req.headers['authorization'].startswith(f'AWS4-HMAC-SHA256 Credential={KEY_ID}/20260927/ap-south-1/s3/aws4_request,')
    rows = list(csv.reader(io.StringIO(req.content.decode())))
    assert rows[0] == HEADER and rows[1][0] == o['ref'] and rows[1][-1] == 'payer@example.test' and len(rows) == 2
    assert [dict(r) for r in runs(db)] == [{'day': datetime(2026, 9, 27).date(), 'status': 'ok', 'row_count': 1, 'error': None}]
    with db.connect() as c:
        assert dict(c.execute('SELECT host,expiry_days FROM invoice_backups').fetchone()) == {
            'host': 'braivex-books-test.s3.ap-south-1.amazonaws.com', 'expiry_days': 90}
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


def test_custom_endpoint_is_path_style_https_only_and_confirmed_in_india(db, env, s3, monkeypatch):
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', 'http://s3.india-store.test')
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT_IN_INDIA', '1')
    assert invoices.backup(NOW) == 'failed'
    assert 'https' in runs(db)[0]['error'] and s3.calls == []
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', 'https://s3.india-store.test/')
    monkeypatch.setenv('INVOICE_BACKUP_REGION', 'in-mum-1')
    monkeypatch.delenv('INVOICE_BACKUP_ENDPOINT_IN_INDIA')
    assert invoices.backup(NOW + 60) == 'failed'  # the app cannot see where a non-AWS store keeps files
    assert 'INVOICE_BACKUP_ENDPOINT_IN_INDIA' in runs(db)[1]['error'] and s3.calls == [] and s3.gets == []
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT_IN_INDIA', '1')
    assert invoices.backup(NOW + 3600) == 'ok'
    assert str(s3.gets[0].url) == 'https://s3.india-store.test/braivex-books-test?lifecycle'
    assert str(s3.calls[0].url) == f'https://s3.india-store.test/braivex-books-test/invoices/{schema()}/2026/09/2026-09-27.csv'
    assert '/in-mum-1/s3/aws4_request' in s3.calls[0].headers['authorization']


@pytest.mark.parametrize('endpoint,region', [
    ('https://s3.us-east-1.amazonaws.com', 'us-east-1'),
    ('https://s3.eu-west-2.amazonaws.com', 'eu-west-2'),
    ('https://s3.amazonaws.com', 'us-east-1'),
    ('https://s3.amazonaws.com', 'ap-south-1'),  # the global endpoint names no region
    ('https://s3.us-east-1.amazonaws.com', 'ap-south-1'),  # signed for India, sent to the US
    ('https://S3.US-EAST-1.AMAZONAWS.COM.', 'ap-south-1'),
    ('https://s3.cn-north-1.amazonaws.com.cn', 'cn-north-1'),
    ('https://s3.ap-south-1.amazonaws.com.example.test', 'ap-south-1'),  # names India, but is not AWS
])
def test_aws_endpoint_outside_india_is_refused(db, env, s3, monkeypatch, endpoint, region):
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', endpoint)
    monkeypatch.setenv('INVOICE_BACKUP_REGION', region)
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT_IN_INDIA', '1')  # a confirmation never overrides the AWS check
    assert invoices.backup(NOW) == 'failed'
    assert s3.calls == [] and s3.gets == [] and 'India' in runs(db)[0]['error']


@pytest.mark.parametrize('endpoint,region', [('https://s3.ap-south-1.amazonaws.com', 'ap-south-1'),
                                             ('https://s3.dualstack.ap-south-2.amazonaws.com/', 'ap-south-2')])
def test_aws_endpoint_in_india_is_accepted_path_style(db, env, s3, monkeypatch, endpoint, region):
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', endpoint)
    monkeypatch.setenv('INVOICE_BACKUP_REGION', region)
    assert invoices.backup(NOW) == 'ok'
    assert str(s3.calls[0].url) == f"{endpoint.rstrip('/')}/braivex-books-test/invoices/{schema()}/2026/09/2026-09-27.csv"


def test_dotted_bucket_is_refused_on_aws_virtual_hosts(db, env, s3, monkeypatch):
    """AWS's wildcard certificate does not cover <a.b>.s3.<region>.amazonaws.com, so every run would fail TLS."""
    monkeypatch.setenv('INVOICE_BACKUP_BUCKET', 'braivex.books')
    assert invoices.backup(NOW) == 'failed'
    assert s3.gets == [] and 'dot' in runs(db)[0]['error']
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', 'https://s3.ap-south-1.amazonaws.com')  # path-style has no such limit
    assert invoices.backup(NOW + 60) == 'ok'
    assert str(s3.calls[0].url).startswith('https://s3.ap-south-1.amazonaws.com/braivex.books/invoices/')


def test_object_key_names_the_schema_unless_public(monkeypatch):
    """A staging service sharing the bucket must never replace production's file for the day."""
    day = datetime(2026, 9, 27).date()
    monkeypatch.setenv('DATABASE_SCHEMA', 'public')
    assert invoices.object_key(day) == 'invoices/2026/09/2026-09-27.csv'
    monkeypatch.setenv('DATABASE_SCHEMA', 'reelsieve_staging')
    assert invoices.object_key(day) == 'invoices/reelsieve_staging/2026/09/2026-09-27.csv'


def test_put_never_replaces_a_stored_day(db, env, s3):
    paid('payer@example.test')
    s3.status, s3.body = 412, '<Error><Code>PreconditionFailed</Code></Error>'
    assert invoices.backup(NOW) == 'failed'
    [req] = s3.calls
    assert req.headers['if-none-match'] == '*'
    assert runs(db)[0]['error'] == ('A file for this day is already in the bucket and was not replaced (HTTP 412). '
                                    'If an earlier run did not write it, check the bucket.')


@pytest.mark.parametrize('config,days', [
    (lifecycle(rule()), 90),
    (lifecycle(rule('<Filter></Filter>', '30')), 30),  # the whole bucket
    (lifecycle(rule('<Filter><Prefix></Prefix></Filter>', '7')), 7),
    (lifecycle(rule('<Prefix>invoices/</Prefix>', '60')), 60),  # the older, filter-less form
    (lifecycle(rule(days='365'), rule(days='45')), 45),  # the shortest rule deletes first
])
def test_lifecycle_rule_deleting_within_90_days_is_accepted(db, env, s3, config, days):
    s3.lifecycle = config
    assert invoices.backup(NOW) == 'ok'
    assert invoices.status(NOW)['last_ok']['expiry_days'] == days


@pytest.mark.parametrize('config', [
    lifecycle(),
    lifecycle(rule(days='365')),
    lifecycle(rule(status='Disabled')),
    lifecycle(rule('<Filter><Prefix>other/</Prefix></Filter>')),
    lifecycle(rule('<Filter><Tag><Key>k</Key><Value>v</Value></Tag></Filter>')),  # untagged files never expire
    lifecycle(rule('<Filter><And><Prefix>invoices/</Prefix><ObjectSizeGreaterThan>9</ObjectSizeGreaterThan></And></Filter>')),
    lifecycle(rule(expiration='<Expiration><Date>2030-01-01T00:00:00Z</Date></Expiration>')),
    'not xml',
    '<?xml version="1.0"?><!DOCTYPE L [<!ENTITY p "invoices/">]>' + lifecycle(rule('<Filter><Prefix>&p;</Prefix></Filter>'))[38:],
])
def test_backup_refuses_without_a_90_day_lifecycle_rule(db, env, s3, config):
    s3.lifecycle = config
    assert invoices.backup(NOW) == 'failed'
    assert s3.calls == [] and 'lifecycle' in runs(db)[0]['error']


@pytest.mark.parametrize('status,code,expect', [
    (404, 'NoSuchLifecycleConfiguration', 'The bucket has no lifecycle rule that deletes files under invoices/ within 90 days'),
    (403, 'AccessDenied', 'The bucket refused to show its lifecycle rules (HTTP 403, AccessDenied); '
                          'the IAM user needs s3:GetLifecycleConfiguration'),
])
def test_unreadable_lifecycle_is_a_recorded_failure(db, env, s3, status, code, expect):
    s3.lifecycle_status, s3.lifecycle = status, f'<Error><Code>{code}</Code><AWSAccessKeyId>{KEY_ID}</AWSAccessKeyId></Error>'
    assert invoices.backup(NOW) == 'failed'
    assert s3.calls == [] and runs(db)[0]['error'] == expect


@pytest.mark.parametrize('var,value,name', [('INVOICE_BACKUP_ENDPOINT', 'https://s3.india-store.test:abc', 'InvalidURL'),
                                            ('INVOICE_BACKUP_ACCESS_KEY_ID', 'AKIA\u00e9X', 'UnicodeEncodeError')])
def test_unexpected_upload_error_is_recorded_not_raised(db, env, s3, monkeypatch, var, value, name):
    monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT_IN_INDIA', '1')
    monkeypatch.setenv(var, value)
    assert invoices.backup(NOW) == 'failed'
    [r] = runs(db)
    assert r['error'] == f'Could not send the request to the bucket ({name})' and value not in r['error']


def test_csv_content_escaping_and_formula_guard():
    rows = [{'ref': 'RS-260927-ABC123', 'ts': NOW - 60, 'plan': 'starter', 'amount_usd': 100.0, 'provider': 'invoice',
             'status': 'paid', 'paid_at': NOW, 'billing_email': 'a,"quoted"@example.test'},
            {'ref': 'RS-260927-DEF456', 'ts': NOW, 'plan': 'commercial', 'amount_usd': 500, 'provider': None,
             'status': 'paid', 'paid_at': NOW + 1, 'billing_email': '=HYPERLINK("http://x.test")@example.test'},
            {'ref': 'RS-260927-GHI789', 'ts': NOW, 'plan': 'starter', 'amount_usd': 100, 'provider': 'stripe',
             'status': 'paid', 'paid_at': NOW + 2, 'billing_email': None}]
    rows += [dict(rows[2], billing_email=e) for e in ('x@a.b;=1+1', '\uff1dcmd@a.b', '\n=1@a.b', 'y@a.b,@SUM(1)', 'z@a.b\t-2')]
    text = invoices.csv_text(rows)
    assert text.splitlines()[1] == ('"RS-260927-ABC123","2026-09-27","starter","100.00","USD","invoice","paid","2026-09-27T12:00:00Z",'
                                    '"a,""quoted""@example.test"')
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == HEADER
    assert parsed[1][-1] == 'a,"quoted"@example.test'
    assert parsed[2][3] == '500.00' and parsed[2][5] == '' and parsed[2][-1] == '\'=HYPERLINK("http://x.test")@example.test'
    assert parsed[3][-1] == ''
    # A spreadsheet that splits on ';' (or ',', tab, a line break) must still find no cell starting a formula.
    assert [r[-1] for r in parsed[4:]] == ["x@a.b;'=1+1", "'\uff1dcmd@a.b", "'\n'=1@a.b", "y@a.b,'@SUM(1)", "z@a.b\t'-2"]
    assert all('"' + r[-1].replace('"', '""') + '"' in text for r in parsed[4:])  # quoted, so the email stays one cell


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
    assert f'invoices/{schema()}/2026/09/2026-09-27.csv' in page and 'The bucket refused the upload (HTTP 403)' in page
    assert 'braivex-books-test.s3.ap-south-1.amazonaws.com' in page and 'deleted by the bucket after 90 days' in page
    assert 'worker service' in page and 'Refunds' in page
    api = web['admin'].get('/api/settings').json()
    assert api['invoice_backup_secret_access_key'] == {'configured': True}


def card(page):
    return page.split('id="backup-card"')[1].split('</section>')[0]


def add_run(db, status, ago):
    with db.connect() as c:
        c.execute('INSERT INTO invoice_backups(day,ts,status,row_count,error,host,expiry_days) VALUES(%s,%s,%s,3,%s,%s,%s)',
                  ('2026-09-27', time.time() - ago, status, None if status == 'ok' else 'The bucket refused the upload (HTTP 403)',
                   'braivex-books.s3.ap-south-1.amazonaws.com', 90 if status == 'ok' else None))


def test_settings_takes_on_off_from_the_worker_runs_not_the_web_env(web, db, monkeypatch):
    """The INVOICE_BACKUP_* variables live on the worker service; the web process that renders Settings has none."""
    for k in ('INVOICE_BACKUP_BUCKET', 'INVOICE_BACKUP_ACCESS_KEY_ID', 'INVOICE_BACKUP_SECRET_ACCESS_KEY'):
        monkeypatch.delenv(k, raising=False)
    page = card(web['admin'].get('/settings').text)
    assert '>Off</span>' in page and 'on the worker service' in page and 'bucket. It starts when' in page
    add_run(db, 'ok', 3600)
    page = card(web['admin'].get('/settings').text)
    assert '>On</span>' in page and '>Off</span>' not in page and 'It starts when' not in page and 'bucket. Each file' in page


@pytest.mark.parametrize('history,state', [
    ([], 'off'),
    ([('ok', 3600)], 'on'),
    ([('ok', 25 * 3600), ('failed', 60)], 'on'),  # still inside the day it covers
    ([('ok', 30 * 3600), ('failed', 60)], 'failing'),
    ([('failed', 60)], 'failing'),
    ([('ok', 30 * 3600)], 'stopped'),  # the worker stopped trying: removed variables or a dead worker
])
def test_backup_state_follows_the_run_records(db, monkeypatch, history, state):
    monkeypatch.delenv('INVOICE_BACKUP_BUCKET', raising=False)
    for status, ago in history:
        add_run(db, status, ago)
    assert invoices.status()['state'] == state


def test_settings_warns_when_backups_stop(web, db):
    add_run(db, 'ok', 30 * 3600)
    page = card(web['admin'].get('/settings').text)
    assert '>Stopped</span>' in page and 'No backup has been tried for more than 26 hours' in page


def test_privacy_notice_names_the_india_backup_its_recipients_and_its_retention(db):
    text = TestClient(server.app).get('/privacy').text
    for required in ['Amazon Web Services', 'Mumbai', 'Hyderabad', 'Our accountant',
                     'backup copies in India are deleted within 90 days of being made',
                     'For the India backup, AWS']:
        assert required in text, required
