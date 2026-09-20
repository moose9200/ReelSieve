"""Billing for ReelSieve — built so an Indian business can take money today, without Stripe.

Three routes to a paid plan, in the order they cost you least:
  1. `link`    — a hosted checkout link per plan (Skydo InstaLink, Dodo, Razorpay, PayPal…). Set in Settings.
  2. `invoice` — the customer asks for an invoice; you send a Skydo InstaLink or virtual-account details and
                 mark it paid in Settings → Orders. Credits are granted the moment you mark it.
  3. `webhook` — when your provider can call back, POST /api/billing/webhook/<provider> with an HMAC signature
                 and the order is settled automatically.
Every path ends in `settle()`, so the customer experience is identical however the money arrives.
Fee note (Skydo public pricing, Sep 2026): flat $19 up to $2,000, $29 to $10,000, 0.3% above. Flat fees hurt
small tickets — $19 on a $100 sale is 19%, versus 3.8% on $500. Prefer cards for Starter, Skydo for Commercial+.
"""
import os,re,json,time,hmac,hashlib,secrets
from app import store,plans
PROVIDERS={
 'skydo':{'name':'Skydo InstaLink','kind':'link','note':'RBI-authorised, zero FX markup, flat fee. Best above $300.'},
 'razorpay':{'name':'Razorpay','kind':'link','note':'Cards and UPI. International needs activation.'},
 'dodo':{'name':'Dodo Payments','kind':'link','note':'Merchant of record; fast onboarding, handles global tax.'},
 'paypal':{'name':'PayPal','kind':'link','note':'Instant to set up, best for small tickets.'},
 'invoice':{'name':'Invoice / bank transfer','kind':'invoice','note':'You send a link or account details, then mark it paid.'},
}
def _schema():
    with store._lock,store.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, ts REAL, user TEXT, plan TEXT,
          amount_usd REAL, provider TEXT, status TEXT DEFAULT 'pending', paid_at REAL, note TEXT, meta TEXT)""")
        c.execute('CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user)');c.commit()
_schema()
PLACEHOLDER=re.compile(r'(example\.|localhost|127\.0\.0\.1|abc123|your[-_]?link|xxxx|placeholder|<|\{)',re.I)
def checkout_link(plan_key):
    """The configured hosted-checkout link, or '' if it is missing or obviously a placeholder.
    Treating a bad link as unset sends the customer down the invoice path, which works,
    instead of to a dead payment page with money in hand."""
    v=(os.getenv(f'CHECKOUT_{plan_key.upper()}') or '').strip()
    if not v.startswith('https://') or PLACEHOLDER.search(v):return ''
    return v
def create_order(user,plan_key,provider='invoice',note='',meta=None):
    p=plans.PLANS.get(plan_key)
    if not p or p['price_usd'] in (None,0):raise ValueError('That plan is not purchasable here')
    ref='RS-'+time.strftime('%y%m%d')+'-'+secrets.token_hex(3).upper()
    with store._lock,store.conn() as c:
        c.execute('INSERT INTO orders(ref,ts,user,plan,amount_usd,provider,status,note,meta) VALUES(?,?,?,?,?,?,?,?,?)',
                  (ref,time.time(),user,plan_key,float(p['price_usd']),provider,'pending',note[:400],json.dumps(meta or {})));c.commit()
    return get_order(ref)
def get_order(ref):
    with store._lock,store.conn() as c:
        r=c.execute('SELECT * FROM orders WHERE ref=?',(ref,)).fetchone();return dict(r) if r else None
def orders(user=None,limit=200):
    q='SELECT * FROM orders';a=[]
    if user:q+=' WHERE user=?';a.append(user)
    q+=' ORDER BY ts DESC LIMIT ?';a.append(limit)
    with store._lock,store.conn() as c:return [dict(r) for r in c.execute(q,a).fetchall()]
def pending_count():
    with store._lock,store.conn() as c:return c.execute("SELECT COUNT(*) n FROM orders WHERE status IN ('pending','reported')").fetchone()['n']
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
    o=get_order(ref)
    if not o or o['status']!='pending':return o
    with store._lock,store.conn() as c:
        c.execute("UPDATE orders SET status='reported' WHERE ref=? AND status='pending'",(ref,));c.commit()
    return get_order(ref)
def settle(ref,by='admin',provider=None):
    """Mark paid and grant the plan's credits. Idempotent — settling twice never double-credits."""
    o=get_order(ref)
    if not o:raise ValueError('No such order')
    if o['status'] in ('paid','cancelled'):return o
    p=plans.PLANS[o['plan']]
    store.ensure_account(o['user'])
    store.set_plan(o['user'],o['plan'],credits=int((store.get_account(o['user']) or {}).get('credits') or 0)+int(p['videos'] or 0))
    with store._lock,store.conn() as c:
        c.execute('UPDATE orders SET status="paid",paid_at=?,note=COALESCE(note,"")||? ,provider=COALESCE(?,provider) WHERE ref=?',
                  (time.time(),f' · settled by {by}',provider,ref));c.commit()
    return get_order(ref)
def cancel(ref,note=''):
    with store._lock,store.conn() as c:
        c.execute('UPDATE orders SET status="cancelled",note=COALESCE(note,"")||? WHERE ref=?',(' · '+note[:200],ref));c.commit()
    return get_order(ref)
# ---------- webhook ----------
def verify(provider,body,signature,secret=None):
    """HMAC-SHA256 of the raw body, hex or base64, constant-time. Providers differ only in header name."""
    sec=(secret or os.getenv('BILLING_WEBHOOK_SECRET') or '').encode()
    if not sec or not signature:return False
    mac=hmac.new(sec,body,hashlib.sha256)
    import base64
    return any(hmac.compare_digest(signature.strip(),x) for x in (mac.hexdigest(),base64.b64encode(mac.digest()).decode()))
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
