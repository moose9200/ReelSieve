"""Security review findings (28 Sep 2026). Each test was written red against the code before its fix.

F1 non-Stripe webhook settled any signed payload carrying an RS- reference.
F2 the data export matched privacy requests by email string, so claiming an address showed that person's request.
F6 the outreach CSV had no formula-injection guard (invoices.py already had one).
F7 the reel stream forwarded Drive's content type instead of pinning video/mp4.
"""
import hashlib
import hmac
import io
import json
import re
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, billing, jobs, server, store, worker
from fakes import connect
from test_saas_routes import SUCCESS, URL, client_for, csrf

ALICE, BOB = 'alice@example.test', 'bob@example.test'
SECRET = 'synthetic-webhook-secret-only'


@pytest.fixture
def web(owners, google, monkeypatch, tmp_path):
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    monkeypatch.setenv('BILLING_WEBHOOK_SECRET', SECRET)
    for k in ('PUBLIC_BASE_URL', 'RAILWAY_PUBLIC_DOMAIN', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'):
        monkeypatch.delenv(k, raising=False)
    clients = {name: client_for(tok) for name, tok in owners.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


# ---------------- F1: a webhook settles an order only when the payload says it was paid, in full ----------------

def hook(web, payload):
    raw = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return web['anon'].post('/api/billing/webhook/skydo', content=raw,
                            headers={'X-Signature': sig, 'Content-Type': 'application/json'})


def status_of(ref):
    return billing.get_order(ref)['status']


def test_a_signed_payload_that_never_says_paid_settles_nothing(web):
    ref = billing.create_order(ALICE, 'starter', 'skydo')['ref']
    for payload in ({'reference': ref},
                    {'reference': ref, 'status': 'failed', 'amount': 100, 'currency': 'USD'},
                    {'reference': ref, 'status': 'pending', 'amount': 100, 'currency': 'USD'},
                    {'data': {'payment': {'status': 'processing', 'amount': 100, 'currency': 'USD', 'note': ref}}}):
        assert hook(web, payload).status_code == 400, payload
        assert status_of(ref) == 'pending'


def test_a_paid_payload_for_another_amount_or_currency_settles_nothing(web):
    ref = billing.create_order(ALICE, 'starter', 'skydo')['ref']  # $100
    for payload in ({'reference': ref, 'status': 'paid', 'amount': 1, 'currency': 'USD'},
                    {'reference': ref, 'status': 'paid', 'amount': 100, 'currency': 'INR'},
                    {'reference': ref, 'status': 'paid', 'amount': 100},
                    {'reference': ref, 'status': 'paid', 'currency': 'USD'},
                    {'reference': ref, 'status': 'paid', 'amount': 500, 'currency': 'USD'}):
        assert hook(web, payload).status_code == 400, payload
        assert status_of(ref) == 'pending'
    from app import plans
    assert plans.account_view(ALICE)['remaining'] == 2  # free plan untouched


def test_a_paid_payload_that_matches_the_order_settles_it_once(web):
    ref = billing.create_order(ALICE, 'starter', 'skydo')['ref']
    r = hook(web, {'event': 'payment.captured', 'payment': {'status': 'captured', 'amount': 10000, 'currency': 'usd',
                                                            'notes': {'reference': ref}}})
    assert r.status_code == 200 and r.json()['status'] == 'paid'
    assert status_of(ref) == 'paid'
    assert hook(web, {'reference': ref, 'status': 'paid', 'amount': '100.00', 'currency': 'USD'}).status_code == 200
    assert billing.get_order(ref)['note'].count('settled by') == 1  # a replay grants nothing again


# ---------------- F2: the export shows privacy requests made from this account, not this email ----------------

def form_request(client, **fields):
    page = client.get('/privacy/request').text
    data = {'csrf': re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1), 'type': 'access',
            'email': ALICE, 'name': 'Pat', 'details': 'Send me everything you hold.', **fields}
    r = client.post('/privacy/request', data=data)
    assert r.status_code == 200, r.text
    return re.search(r'PR-\d{6}-[0-9A-F]{6}', r.text).group(0)


def test_the_export_holds_only_privacy_requests_this_account_sent(web, db):
    mine = form_request(web['alice'], details='Mine: a copy of my data please')
    stranger = form_request(web['anon'], email=ALICE, details='Not hers: sent by whoever claimed the address')
    data = web['alice'].get('/api/account/export').json()
    assert [r['ref'] for r in data['privacy_requests']] == [mine]
    assert stranger not in json.dumps(data) and 'Not hers' not in json.dumps(data)
    with db.connect() as c:  # the stranger's request is still on file for the admins to answer
        assert c.execute('SELECT count(*) AS n FROM privacy_requests').fetchone()['n'] == 2
        assert c.execute('SELECT owner_id FROM privacy_requests WHERE ref=%s', (stranger,)).fetchone()['owner_id'] is None
        assert c.execute('SELECT owner_id FROM privacy_requests WHERE ref=%s', (mine,)).fetchone()['owner_id'] == db.user_id(ALICE)


def test_another_signed_in_account_cannot_put_its_request_in_your_export(web):
    form_request(web['bob'], email=ALICE, details='Bob typed her address')
    assert web['alice'].get('/api/account/export').json()['privacy_requests'] == []


# ---------------- F6: the outreach CSV cannot carry a spreadsheet formula ----------------

FORMULAS = ['=1+1', '+1', '-1+1', '@SUM(A1)', '=cmd|\'/c calc\'!A1', 'safe;=1+2', 'safe,=HYPERLINK("http://x")']


@pytest.mark.parametrize('bad', FORMULAS)
def test_the_outreach_csv_neutralises_every_formula_a_prospect_can_plant(bad):
    import csv as _csv
    from app import linkedin
    rows = [{'ts': 0, 'channel': 'cohost', 'name': bad, 'city': bad, 'status': 'queued', 'url': None,
             'meta': None, 'message': bad, 'note': bad}]
    out = list(_csv.reader(io.StringIO(linkedin.csv_rows(rows))))[1]
    for cell in out:
        for part in re.split(r'[,;\t\r\n]', cell):
            assert not part.startswith(('=', '+', '-', '@')), (bad, cell)
    assert out[2].replace("'", '', 1) == bad  # the value is kept, with one quote in front of the formula


def test_the_downloaded_outreach_csv_is_guarded_too(web, db):
    store.add_outreach(ALICE, 'cohost', "=2+5+cmd|' /C calc'!A0", 'https://www.airbnb.co.uk/contact_host/1/send_message',
                       'Leeds', 'Hi there')
    text = web['alice'].get('/api/outreach/export.csv').text
    assert '"=2+5+cmd' not in text and "\"'=2+5+cmd" in text  # every cell is quoted; the formula one starts with '


# ---------------- F7: a reel is streamed as video/mp4, whatever Drive's header says ----------------

def test_the_reel_stream_pins_video_mp4_whatever_drive_returns(web, owners, google, db, monkeypatch):
    connect(owners, google)
    job = jobs.admit(ALICE, URL, {'attested': True})
    worker.process(jobs.claim('w', 30), lambda j, d: [sys.executable, '-c', SUCCESS, str(d)])
    assert jobs.get(ALICE, job['id'])['status'] == 'done'
    inner = google.handle

    def html_content_type(req):
        r = inner(req)
        if req.url.params.get('alt') == 'media':
            return httpx.Response(r.status_code, content=b'video',
                                  headers={**{k: v for k, v in r.headers.items() if k.lower() != 'content-type'},
                                           'Content-Type': 'text/html'})
        return r
    monkeypatch.setattr(google, 'handle', html_content_type)
    r = web['alice'].get(f"/api/jobs/{job['id']}/video")
    assert r.status_code == 200 and r.headers['content-type'] == 'video/mp4'
