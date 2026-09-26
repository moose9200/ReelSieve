"""Billing for ReelSieve — built so an Indian business can take money today, with or without Stripe.

With STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET set, customers pay on hosted Stripe Checkout and a signed
`checkout.session.completed` webhook settles the order (see the Stripe section below). Otherwise,
three routes to a paid plan, in the order they cost you least:
  1. `link`    — a hosted checkout link per plan (Skydo InstaLink, Dodo, Razorpay, PayPal…). Set in Settings.
  2. `invoice` — the customer asks for an invoice; you send a Skydo InstaLink or virtual-account details and
                 mark it paid in Settings → Orders. Credits are granted the moment you mark it.
  3. `webhook` — when your provider can call back, POST /api/billing/webhook/<provider> with an HMAC signature
                 and the order is settled automatically.
Every path ends in `settle()`, so the customer experience is identical however the money arrives.
Skydo specifics, checked against their own FAQ and app on 20 Sep 2026:
  · an InstaLink is SINGLE USE — "cannot be used again" once paid — so it can never be a plan-wide link
  · it carries no custom reference the payer or a query string can set; the merchant types invoice number and
    description at creation, which is why we mint one link per order and store it on that order
  · there is no redirect/return URL and no public API or webhooks yet, so settlement is confirmed by a human
  · money lands as an unmapped payment that the merchant maps to an invoice in the dashboard
Fees: international accounts are flat USD 19 up to $2,000, $29 to $10,000, then 0.3% (+GST). InstaLinks are
priced separately — ACH debit 2%, minimum $9. So Starter ($100) costs $9 via InstaLink against $19 by transfer,
and Commercial ($500) costs $10 against $19. Minimum transaction is $50 and InstaLinks expect a US payer.
"""
import os,re,json,time,hmac,hashlib,secrets
import httpx
from app import store,plans,database
from psycopg.errors import UniqueViolation
PROVIDERS={
 'skydo':{'name':'Skydo InstaLink','kind':'order-link','note':'RBI-authorised, zero FX markup. Each link is single use, so mint one per order and attach it below.'},
 'razorpay':{'name':'Razorpay','kind':'link','note':'Cards and UPI. International needs activation.'},
 'dodo':{'name':'Dodo Payments','kind':'link','note':'Merchant of record; fast onboarding, handles global tax.'},
 'paypal':{'name':'PayPal','kind':'link','note':'Instant to set up, best for small tickets.'},
 'invoice':{'name':'Invoice / bank transfer','kind':'invoice','note':'You send a link or account details, then mark it paid.'},
}
PLACEHOLDER=re.compile(r'(example\.|localhost|127\.0\.0\.1|abc123|your[-_]?link|xxxx|placeholder|<|\{)',re.I)
# Some providers mint a link per payment, not per product. Skydo's InstaLink is one of them: its own FAQ says a
# link "cannot be used again" once paid. Reused as a plan-wide link it works for exactly one customer and then
# silently fails for everyone after, so we refuse it here and route those providers through per-order links.
SINGLE_USE=re.compile(r'(dashboard\.skydo\.com/pay/|/pay/pyl_)',re.I)
def single_use_link(v):
    return bool(v) and bool(SINGLE_USE.search(v))
def checkout_link(plan_key):
    """The reusable hosted-checkout link for a plan, or '' when it is missing, a placeholder, or single-use.
    Anything we return here is shown to every customer who picks that plan, so it has to survive reuse.
    Falling back to '' sends them down the invoice path, which works, rather than to a dead payment page."""
    v=(os.getenv(f'CHECKOUT_{plan_key.upper()}') or '').strip()
    if not v.startswith('https://') or PLACEHOLDER.search(v) or single_use_link(v):return ''
    return v
def create_order(user,plan_key,provider='invoice',note='',meta=None):
    p=plans.PLANS.get(plan_key)
    if not p or p['price_usd'] in (None,0):raise ValueError('That plan is not purchasable here')
    for _ in range(6):
        ref='RS-'+time.strftime('%y%m%d')+'-'+secrets.token_hex(3).upper()
        try:
            with database.connect() as c:
                c.execute('INSERT INTO orders(ref,ts,owner_id,plan,amount_usd,provider,status,note,meta) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                          (ref,time.time(),database.user_id(user,c),plan_key,float(p['price_usd']),provider,'pending',note[:400],json.dumps(meta or {})))
                return get_order(ref,conn=c)
        except UniqueViolation:
            continue
    raise ValueError('Could not allocate an order reference — try again')


def get_order(ref,conn=None):
    with database.transaction(conn) as c:
        return c.execute('SELECT o.*,u.email AS "user" FROM orders o JOIN users u ON u.id=o.owner_id WHERE ref=%s',(ref,)).fetchone()


def get_order_for(ref,user,admin=False):
    if not ref:return None
    with database.connect() as c:
        q='SELECT o.*,u.email AS "user" FROM orders o JOIN users u ON u.id=o.owner_id WHERE ref=%s'
        args=[ref]
        if not admin:
            q+=' AND u.email=%s'
            args.append((user or '').strip().lower())
        return c.execute(q,args).fetchone()


def set_pay_link(ref,url):
    u=(url or '').strip()
    if u and not u.startswith('https://'):raise ValueError('The payment link must start with https://')
    if u and PLACEHOLDER.search(u):raise ValueError('That looks like a placeholder, not a real payment link')
    with database.connect() as c:
        o=c.execute('SELECT * FROM orders WHERE ref=%s FOR UPDATE',(ref,)).fetchone()
        if not o:raise ValueError('No such order')
        if o['status'] in ('paid','cancelled'):raise ValueError(f'Order {ref} is already {o["status"]}')
        c.execute('UPDATE orders SET pay_link=%s WHERE ref=%s',(u or None,ref))
        return get_order(ref,conn=c)


VIEW=('ref','ts','plan','amount_usd','provider','status','paid_at','pay_link','note','user')
def view(o):
    """What order pages and JSON responses show: no owner id and no payer or payment-session metadata."""
    return {k:o[k] for k in VIEW} if o else o
def orders(user=None,limit=200):
    return [view(o) for o in store._owned_rows('orders',user,limit)]


def pending_count():
    with database.connect() as c:
        return c.execute("SELECT count(*) AS n FROM orders WHERE status IN ('pending','reported')").fetchone()['n']

def pay_url(plan_key,ref,base):
    """Static provider links can't tell tenants apart, so every order carries its own reference.
    We append it in the shapes the common providers read, and show it to the customer to quote."""
    link=checkout_link(plan_key)
    if not link:return None
    sep='&' if '?' in link else '?'
    from urllib.parse import quote as q
    ret=f'{base.rstrip("/")}/upgrade/paid?ref={q(ref)}'
    return f'{link}{sep}ref={q(ref)}&client_reference_id={q(ref)}&reference={q(ref)}&redirect_url={q(ret)}'
def mark_reported(ref):
    with database.connect() as c:
        c.execute("UPDATE orders SET status='reported' WHERE ref=%s AND status='pending'",(ref,))
        return get_order(ref,conn=c)


def settle(ref,by='admin',provider=None):
    """Lock the order and grant credits in the same transaction as the paid record."""
    with database.connect() as c:
        o=c.execute('SELECT * FROM orders WHERE ref=%s FOR UPDATE',(ref,)).fetchone()
        if not o:raise ValueError('No such order')
        if o['status'] in ('paid','cancelled'):return get_order(ref,conn=c)
        p=plans.PLANS[o['plan']]
        c.execute('INSERT INTO accounts(owner_id,created) VALUES(%s,%s) ON CONFLICT(owner_id) DO NOTHING',
                  (o['owner_id'],time.time()))
        c.execute('UPDATE accounts SET plan=%s,credits=credits+%s WHERE owner_id=%s',
                  (o['plan'],int(p['videos'] or 0),o['owner_id']))
        # billing_email: the tax-record snapshot that survives account erasure (retention.FINANCIAL_RECORDS_YEARS).
        c.execute("UPDATE orders SET status='paid',paid_at=%s,note=COALESCE(note,'')||%s,provider=COALESCE(%s,provider),"
                  "billing_email=(SELECT email FROM users WHERE id=orders.owner_id) WHERE ref=%s",
                  (time.time(),f' · settled by {by}',provider,ref))
        return get_order(ref,conn=c)


def cancel(ref,note=''):
    with database.connect() as c:
        o=c.execute('SELECT * FROM orders WHERE ref=%s FOR UPDATE',(ref,)).fetchone()
        if not o:raise ValueError('No such order')
        if o['status']=='paid':raise ValueError(f'{ref} is already paid — refund it in your provider instead')
        c.execute("UPDATE orders SET status='cancelled',note=COALESCE(note,'')||%s WHERE ref=%s",(' · '+note[:200],ref))
        return get_order(ref,conn=c)
# ---------- webhook ----------
def verify(provider,body,signature,secret=None):
    """HMAC-SHA256 of the raw body, hex or base64, constant-time. Providers differ only in header name."""
    sec=(secret or os.getenv('BILLING_WEBHOOK_SECRET') or '').encode()
    if not sec or not signature:return False
    mac=hmac.new(sec,body,hashlib.sha256)
    import base64
    return any(hmac.compare_digest(signature.strip(),x) for x in (mac.hexdigest(),base64.b64encode(mac.digest()).decode()))
# ---------- Stripe Checkout (hosted) ----------
# Docs checked 26 Sep 2026: docs.stripe.com/api/checkout/sessions/create, /webhooks (manual verification),
# /checkout/fulfillment. Plain HTTPS through httpx; no SDK. Prices come from plans.PLANS, never the client.
STRIPE_API = 'https://api.stripe.com/v1/checkout/sessions'
STRIPE_TOLERANCE = 300  # Stripe's libraries default to 5 minutes


def stripe_enabled():
    return bool((os.getenv('STRIPE_SECRET_KEY') or '').strip() and (os.getenv('STRIPE_WEBHOOK_SECRET') or '').strip())


def _stripe(method, url, **kw):
    """One Stripe call. Errors never carry the key, URL or response body into the message."""
    try:
        with httpx.Client(timeout=20) as h:
            r = h.request(method, url, auth=(os.environ['STRIPE_SECRET_KEY'].strip(), ''), **kw)
    except httpx.HTTPError:
        raise RuntimeError('Stripe did not respond — try again shortly') from None
    if r.status_code != 200:
        raise RuntimeError(f'Stripe refused the request ({r.status_code})')
    return r.json()


def start_stripe_checkout(user, plan_key, base):
    """Order first, then a hosted Checkout Session for exactly that order's server-side price."""
    o = create_order(user, plan_key, 'stripe')
    p, ref = plans.PLANS[plan_key], o['ref']
    form = {'mode': 'payment', 'client_reference_id': ref, 'customer_email': o['user'],
            'line_items[0][quantity]': '1', 'line_items[0][price_data][currency]': 'usd',
            'line_items[0][price_data][unit_amount]': str(int(round(o['amount_usd'] * 100))),
            'line_items[0][price_data][product_data][name]': f"ReelSieve {p['name']} · {p['videos']} videos",
            'metadata[app]': 'reelsieve', 'metadata[order_ref]': ref, 'metadata[owner_id]': o['owner_id'],
            'metadata[plan]': plan_key,
            'success_url': f'{base}/upgrade/paid?ref={ref}&session_id={{CHECKOUT_SESSION_ID}}',
            'cancel_url': f'{base}/upgrade?cancelled=1'}
    try:
        s = _stripe('POST', STRIPE_API, data=form, headers={'Idempotency-Key': 'reelsieve-' + ref})
    except RuntimeError:
        cancel(ref, 'Stripe checkout could not start')
        raise
    with database.connect() as c:
        meta = {**json.loads(o['meta'] or '{}'), 'stripe_session': s['id']}
        c.execute('UPDATE orders SET meta=%s WHERE ref=%s', (json.dumps(meta), ref))
        return get_order(ref, conn=c), s['url']


def stripe_session(session_id):
    if not re.fullmatch(r'cs_[A-Za-z0-9_]{1,250}', session_id or ''):
        raise ValueError('Not a Checkout Session ID')
    return _stripe('GET', f'{STRIPE_API}/{session_id}')


def stripe_signature_ok(body, header, secret=None, now=None):
    """Stripe-Signature: t=<ts>,v1=<hex>[,v1=…]. HMAC-SHA256 of '<t>.<raw body>', v1 only, 5-minute window."""
    sec = (os.getenv('STRIPE_WEBHOOK_SECRET') if secret is None else secret) or ''
    parts = [x.split('=', 1) for x in (header or '').split(',') if '=' in x]
    ts, sigs = [v for k, v in parts if k.strip() == 't'], [v.strip() for k, v in parts if k.strip() == 'v1']
    if not sec or len(ts) != 1 or not ts[0].strip().isdigit() or not sigs:
        return False
    if abs((time.time() if now is None else now) - int(ts[0])) > STRIPE_TOLERANCE:
        return False
    want = hmac.new(sec.encode(), ts[0].strip().encode() + b'.' + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, s) for s in sigs)


def fulfil_stripe_session(session, by='stripe'):
    """Settle the order a paid Checkout Session belongs to, once. Returns None when it is not ours or not paid yet.
    Raises ValueError when it claims to be ours but does not match the order we created."""
    meta = session.get('metadata') or {}
    o = get_order(meta.get('order_ref') or '') if meta.get('app') == 'reelsieve' else None
    if not o:
        return None
    if session.get('payment_status') == 'unpaid':
        return o
    problems = [name for name, ok in [
        ('session', session.get('id') == json.loads(o['meta'] or '{}').get('stripe_session')),
        ('owner', meta.get('owner_id') == o['owner_id']), ('plan', meta.get('plan') == o['plan']),
        ('reference', session.get('client_reference_id') == o['ref']), ('mode', session.get('mode') == 'payment'),
        ('payment status', session.get('payment_status') == 'paid'), ('currency', session.get('currency') == 'usd'),
        ('order status', o['status'] != 'cancelled'),
        ('amount', session.get('amount_total') == int(round(o['amount_usd'] * 100))
                   == int(plans.PLANS[o['plan']]['price_usd'] * 100)),
    ] if not ok]
    if problems:
        raise ValueError(f"Checkout Session does not match order {o['ref']}: " + ', '.join(problems))
    return settle(o['ref'], by=f"{by}:{session['id']}", provider='stripe')


def ref_from_payload(payload):
    """Find our order ref wherever the provider hid it."""
    def walk(o):
        if isinstance(o,dict):
            for k,v in o.items():
                if isinstance(v,str) and v.startswith('RS-'):return v
                r=walk(v)
                if r:return r
        elif isinstance(o,list):
            for v in o:
                r=walk(v)
                if r:return r
        return None
    return walk(payload)
