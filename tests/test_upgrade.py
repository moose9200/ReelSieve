"""In-app upgrade: plans page, server-priced Stripe Checkout, signed webhook, fallback without Stripe.
Stripe is an HTTPX MockTransport; no network, no real keys, no real payments."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, billing, database, plans, server, store

ALICE, BOB = 'alice@example.test', 'bob@example.test'
KEY, WHSEC = 'sk_test_synthetic_not_a_key', 'whsec_synthetic_not_a_secret'


class Stripe:
    """Just enough of POST/GET /v1/checkout/sessions to exercise our side of the contract."""

    def __init__(self):
        self.calls, self.sessions = [], {}

    def handle(self, req):
        self.calls.append(req)
        assert req.url.host == 'api.stripe.com' and req.url.scheme == 'https'
        if req.method == 'POST' and req.url.path == '/v1/checkout/sessions':
            f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            sid = f'cs_test_{len(self.sessions) + 1}'
            self.sessions[sid] = {
                'id': sid, 'object': 'checkout.session', 'mode': f['mode'], 'status': 'open', 'payment_status': 'unpaid',
                'amount_total': int(f['line_items[0][price_data][unit_amount]']) * int(f['line_items[0][quantity]']),
                'currency': f['line_items[0][price_data][currency]'], 'client_reference_id': f.get('client_reference_id'),
                'metadata': {k[9:-1]: v for k, v in f.items() if k.startswith('metadata[')},
                'url': 'https://checkout.stripe.com/c/pay/' + sid}
            return httpx.Response(200, json=self.sessions[sid])
        if req.method == 'GET' and req.url.path.startswith('/v1/checkout/sessions/'):
            s = self.sessions.get(req.url.path.rsplit('/', 1)[-1])
            return httpx.Response(200, json=s) if s else httpx.Response(404, json={'error': {'type': 'invalid_request_error'}})
        raise AssertionError('Unexpected Stripe request: ' + req.method + ' ' + req.url.path)

    def form(self, i=0):
        return {k: v[0] for k, v in parse_qs(self.calls[i].content.decode()).items()}

    def pay(self, sid):
        self.sessions[sid].update(status='complete', payment_status='paid')
        return self.sessions[sid]


def sign(body, t=None, secret=WHSEC):
    t = int(time.time()) if t is None else t
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.'.encode() + body, hashlib.sha256).hexdigest()}"


def event(session, kind='checkout.session.completed', eid='evt_1'):
    return json.dumps({'id': eid, 'object': 'event', 'type': kind, 'data': {'object': session}}).encode()


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


def csrf(session):
    return {'X-CSRF-Token': auth.csrf_token(session)}


@pytest.fixture
def stripe(monkeypatch):
    fake = Stripe()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setenv('STRIPE_SECRET_KEY', KEY)
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', WHSEC)
    return fake


@pytest.fixture
def web(owners, monkeypatch):
    for k in ('PUBLIC_BASE_URL', 'RAILWAY_PUBLIC_DOMAIN', 'CHECKOUT_STARTER', 'CHECKOUT_COMMERCIAL', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'):
        monkeypatch.delenv(k, raising=False)
    clients = {name: client_for(tok) for name, tok in owners.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def checkout(web, owners, plan='starter', who='alice', **extra):
    r = web[who].post('/api/billing/start', json={'plan': plan, **extra}, headers=csrf(owners[who]))
    assert r.status_code == 200, r.text
    return r.json()


def webhook(web, body, sig=None):
    return web['anon'].post('/api/billing/webhook/stripe', content=body,
                            headers={'Stripe-Signature': sign(body) if sig is None else sig, 'Content-Type': 'application/json'})


# ---------- plans page ----------

def test_plans_page_shows_current_plan_remaining_and_every_plan(web):
    page = web['alice'].get('/upgrade').text
    assert 'Free' in page and '2 videos left' in page
    for p in plans.public_plans():
        assert f'data-plan-card="{p["key"]}"' in page and p['price_label'].replace("'", '&#39;') in page
        for f in p['features']:
            assert f in page
    assert page.count('Current plan') == 1 and 'data-plan-card="free"' in page
    assert 'mailto:hello@braivex.com' in page and 'Contact us' in page
    assert 'href="/upgrade"' in page and '>Plans<' in page  # nav entry


def test_plans_page_marks_a_paid_plan_and_an_empty_balance(web):
    store.set_plan(ALICE, 'starter', 0)
    page = web['alice'].get('/upgrade').text
    assert 'No videos left' in page
    card = page.split('data-plan-card="starter"', 1)[1].split('</article>', 1)[0]
    assert 'Current plan' in card and 'Buy again' in card


def test_upgrade_ctas_on_account_and_new_reel_pages(web):
    for path in ('/account', '/app'):
        assert 'href="/upgrade"' in web['alice'].get(path).text, path
    store.set_plan(ALICE, 'starter', 0)
    assert 'Buy more videos' in web['alice'].get('/app').text


def test_without_stripe_the_page_falls_back_to_invoice_clearly_labelled(web, owners, monkeypatch):
    monkeypatch.setenv('STRIPE_SECRET_KEY', KEY)  # half-configured is off: no webhook secret, no card checkout
    assert not billing.stripe_enabled()
    page = web['alice'].get('/upgrade').text
    assert 'Card payment is not switched on yet' in page and 'href="/upgrade?plan=starter"' in page
    assert 'Request an invoice' in web['alice'].get('/upgrade?plan=starter').text
    r = web['alice'].post('/api/billing/start', json={'plan': 'starter'}, headers=csrf(owners['alice']))
    assert r.status_code == 400 and 'invoice' in r.json()['detail']
    ref = web['alice'].post('/api/billing/request', json={'plan': 'starter'}, headers=csrf(owners['alice'])).json()['order']['ref']
    assert billing.get_order(ref)['provider'] == 'invoice'


# ---------- checkout session ----------

def test_checkout_session_is_priced_on_the_server(web, owners, stripe):
    out = checkout(web, owners, 'commercial', amount=1, price_usd=1, unit_amount=1, quantity=50, currency='inr')
    assert out['pay_url'] == 'https://checkout.stripe.com/c/pay/cs_test_1'
    f, req = stripe.form(), stripe.calls[0]
    assert req.headers['authorization'].startswith('Basic ') and req.headers['idempotency-key']
    assert f['mode'] == 'payment' and f['line_items[0][price_data][currency]'] == 'usd'
    assert f['line_items[0][price_data][unit_amount]'] == '50000' and f['line_items[0][quantity]'] == '1'
    ref = out['order']['ref']
    order = billing.get_order(ref)
    assert order['provider'] == 'stripe' and order['status'] == 'pending' and order['amount_usd'] == 500
    assert json.loads(order['meta'])['stripe_session'] == 'cs_test_1'
    assert f['metadata[order_ref]'] == ref == f['client_reference_id']
    assert f['metadata[owner_id]'] == database.user_id(ALICE) and f['metadata[plan]'] == 'commercial'
    assert f['success_url'] == f'https://testserver/upgrade/paid?ref={ref}&session_id={{CHECKOUT_SESSION_ID}}'
    assert f['cancel_url'].startswith('https://testserver/upgrade?cancelled=1')
    assert KEY not in json.dumps(out)
    for bad in ('free', 'enterprise', 'gold'):
        assert web['alice'].post('/api/billing/start', json={'plan': bad}, headers=csrf(owners['alice'])).status_code == 400


def test_stripe_failure_cancels_the_order_and_leaks_nothing(web, owners, stripe):
    stripe.handle = lambda req: httpx.Response(500, json={'error': {'message': KEY}})
    r = web['alice'].post('/api/billing/start', json={'plan': 'starter'}, headers=csrf(owners['alice']))
    assert r.status_code == 502 and KEY not in r.text
    assert [o['status'] for o in billing.orders(ALICE)] == ['cancelled']


# ---------- webhook ----------

def test_signature_valid_invalid_and_stale():
    body, now = b'{"id":"evt_1"}', int(time.time())
    assert billing.stripe_signature_ok(body, sign(body, now), WHSEC)
    assert billing.stripe_signature_ok(body, 't=%d,v1=%s,%s' % (now, '0' * 64, sign(body, now).split(',')[1]), WHSEC)  # rolled secret
    assert not billing.stripe_signature_ok(body, sign(body, now, 'whsec_other'), WHSEC)
    assert not billing.stripe_signature_ok(body + b' ', sign(body, now), WHSEC)
    assert not billing.stripe_signature_ok(body, sign(body, now - 301), WHSEC)
    assert not billing.stripe_signature_ok(body, sign(body, now + 301), WHSEC)
    assert not billing.stripe_signature_ok(body, sign(body, now).replace('v1=', 'v0='), WHSEC)
    for junk in ('', 'garbage', 't=abc,v1=00', f't={now}'):
        assert not billing.stripe_signature_ok(body, junk, WHSEC)
    assert not billing.stripe_signature_ok(body, sign(body, now, ''), '')


def test_paid_event_grants_credits_once_even_when_replayed(web, owners, stripe):
    ref = checkout(web, owners)['order']['ref']
    body = event(stripe.pay('cs_test_1'))
    assert webhook(web, body, 'bad').status_code == 400
    assert webhook(web, body, sign(body, int(time.time()) - 600)).status_code == 400
    assert billing.get_order(ref)['status'] == 'pending'
    for _ in range(3):
        r = webhook(web, body)
        assert r.status_code == 200 and r.json()['status'] == 'paid'
    assert webhook(web, event(stripe.sessions['cs_test_1'], 'checkout.session.async_payment_succeeded', 'evt_2')).status_code == 200
    account = plans.account_view(ALICE)
    assert account['plan'] == 'starter' and account['credits'] == plans.PLANS['starter']['videos']
    assert 'cs_test_1' in billing.get_order(ref)['note']


def test_unpaid_or_foreign_sessions_grant_nothing(web, owners, stripe):
    ref = checkout(web, owners)['order']['ref']
    unpaid = stripe.sessions['cs_test_1'] | {'status': 'complete'}
    assert webhook(web, event(unpaid)).status_code == 200
    other_app = {'id': 'cs_test_x', 'object': 'checkout.session', 'mode': 'payment', 'payment_status': 'paid',
                 'amount_total': 100, 'currency': 'usd', 'metadata': {}}
    assert webhook(web, event(other_app)).json().get('ignored')
    assert webhook(web, event(stripe.pay('cs_test_1'), 'payment_intent.succeeded')).json().get('ignored')
    assert billing.get_order(ref)['status'] == 'pending' and plans.account_view(ALICE)['credits'] == 0


@pytest.mark.parametrize('tamper', [
    {'amount_total': 100}, {'currency': 'eur'}, {'mode': 'subscription'}, {'payment_status': 'no_payment_required'},
    {'id': 'cs_test_other'}, {'metadata': 'plan=commercial'}, {'metadata': 'owner_id=bob'},
])
def test_mismatched_session_is_refused(web, owners, stripe, tamper):
    ref = checkout(web, owners)['order']['ref']
    s = dict(stripe.pay('cs_test_1'))
    if 'metadata' in tamper:
        k, v = tamper['metadata'].split('=')
        s['metadata'] = {**s['metadata'], k: database.user_id(BOB) if v == 'bob' else v}
    else:
        s.update(tamper)
    r = webhook(web, event(s))
    assert r.status_code == 400, r.text
    assert billing.get_order(ref)['status'] == 'pending' and plans.account_view(ALICE)['credits'] == 0


# ---------- return page and ownership ----------

def test_return_page_confirms_with_stripe_and_shows_paid_or_pending(web, owners, stripe):
    ref = checkout(web, owners)['order']['ref']
    pending = web['alice'].get(f'/upgrade/paid?ref={ref}&session_id=cs_test_1').text
    assert 'Confirming your payment' in pending and billing.get_order(ref)['status'] == 'pending'
    stripe.pay('cs_test_1')
    paid = web['alice'].get(f'/upgrade/paid?ref={ref}&session_id=cs_test_1').text
    assert 'Paid' in paid and billing.get_order(ref)['status'] == 'paid'
    assert plans.account_view(ALICE)['credits'] == 3
    web['alice'].get(f'/upgrade/paid?ref={ref}&session_id=cs_test_1')
    assert plans.account_view(ALICE)['credits'] == 3


def test_one_customer_cannot_see_or_settle_another_customers_order(web, owners, stripe):
    ref = checkout(web, owners)['order']['ref']
    bob_ref = checkout(web, owners, who='bob')['order']['ref']
    stripe.pay('cs_test_1')
    assert ref not in web['bob'].get(f'/upgrade/paid?ref={ref}&session_id=cs_test_1').text
    assert ref not in web['bob'].get(f'/upgrade?ref={ref}').text
    assert ref not in web['bob'].get('/upgrade').text
    assert all(o['ref'] != ref for o in web['bob'].get('/api/billing/orders').json()['orders'])
    web['bob'].get(f'/upgrade/paid?ref={bob_ref}&session_id=cs_test_1')  # Alice's paid session on Bob's order
    assert billing.get_order(bob_ref)['status'] == 'pending' and plans.account_view(BOB)['credits'] == 0
    assert web['bob'].post('/api/billing/settle', json={'ref': bob_ref}, headers=csrf(owners['bob'])).status_code == 403
    assert billing.get_order(ref)['status'] == 'pending'  # Bob's visit did not settle Alice's order either
    assert ref in web['alice'].get('/upgrade').text


def test_settings_list_stripe_keys_as_secrets_without_values(stripe):
    rows = {s['key']: s for s in server.settings_view()}
    for k in ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'):
        assert rows[k]['secret'] and rows[k]['configured'] and rows[k]['value'] == ''
