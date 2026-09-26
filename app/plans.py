"""Plans, credits and free-tier guardrails for ReelSieve.

Layered abuse prevention (industry practice: never rely on one signal):
  1. account        — free tier gets FREE_LIFETIME videos, ever
  2. IP network     — keyed HMAC of /24 (v4) or /64 (v6): FREE_PER_NET free videos per 30 days across all accounts
  3. cooldown       — at most one free video per FREE_COOLDOWN_H hours per account
  4. email hygiene  — disposable/temporary domains refused at signup
  5. idempotent     — re-running the SAME listing never costs a second free credit
Raw IPs are never stored; the network hash is pseudonymised, kept on free videos only (see store.py). No device
fingerprint is collected. Paid plans skip 2–3; they are spend-limited by credits.
"""
import os,re,time
from app import store, database
PLANS={
 'free':      {'key':'free','name':'Free','price_label':'$0','period':'2 videos','videos':2,'price_usd':0,
               'max_seconds':60,'ai_motion':False,'cta_label':'Start free','cta_href':'/signup'},
 'starter':   {'key':'starter','name':'Starter','price_label':'$100','period':'one-off · 3 videos','videos':3,'price_usd':100,
               'max_seconds':90,'ai_motion':True,'highlight':True,'cta_label':'Choose Starter','cta_href':'/signup?plan=starter'},
 'commercial':{'key':'commercial','name':'Commercial','price_label':'$500','period':'one-off · 20 videos','videos':20,'price_usd':500,
               'max_seconds':90,'ai_motion':True,'cta_label':'Choose Commercial','cta_href':'/signup?plan=commercial'},
 'enterprise':{'key':'enterprise','name':'Enterprise','price_label':"Let's talk",'period':'unlimited videos','videos':None,'price_usd':None,
               'max_seconds':90,'ai_motion':True,'cta_label':'Talk to us','cta_href':'mailto:hello@braivex.com?subject=ReelSieve%20Enterprise'},
}


def _features(p):
    """Only what the product does today, built from the limits it enforces (tests/test_plan_features.py)."""
    return [f for f in [
        'Unlimited videos' if p['videos'] is None else f"{p['videos']} videos",
        f"Up to {p['max_seconds']} seconds per video",
        'AI camera motion' if p['ai_motion'] else None,
        'Full HD 16:9 or 9:16 video, plus a 720p copy',
        'Saved to your Google Drive, private until you share it',
        'Failed renders refunded, and remaking a listing is free',
    ] if f]


for _p in PLANS.values():
    _p['features'] = _features(_p)
ORDER=['free','starter','commercial','enterprise']
FREE_LIFETIME=int(os.getenv('FREE_LIFETIME','2'))
FREE_PER_NET=int(os.getenv('FREE_PER_NET','12'))  # offices and mobile carriers share a /24; the per-account cap is the real control
FREE_COOLDOWN_H=float(os.getenv('FREE_COOLDOWN_H','0'))  # 2 lifetime videos is the real cap; a cooldown only hurts first-run UX
DISPOSABLE=set('''mailinator.com guerrillamail.com 10minutemail.com tempmail.com temp-mail.org yopmail.com throwawaymail.com
sharklasers.com getnada.com trashmail.com maildrop.cc dispostable.com fakeinbox.com mailnesia.com mintemail.com
moakt.com emailondeck.com tempr.email discard.email spamgourmet.com mytemp.email burnermail.io grr.la spam4.me
mailcatch.com inboxbear.com tempmailo.com tmpmail.org luxusmail.org anonbox.net'''.split())
ROLE_LOCAL={'admin','info','support','contact','sales','billing','noreply','no-reply','postmaster','webmaster','abuse','test'}
def _default_plan(user, conn=None):
    """Admins (the people running this install) are never metered; everyone else starts free."""
    with database.transaction(conn) as c:
        row = c.execute('SELECT role FROM users WHERE id=%s', (database.user_id(user, c),)).fetchone()
        return 'enterprise' if row['role'] == 'admin' else 'free'
def plan_of(user):
    a=store.get_account(user) or {}
    return PLANS.get(a.get('plan') or 'free',PLANS['free'])
def account_view(user):
    a=store.ensure_account(user,_default_plan(user));p=PLANS.get(a.get('plan') or 'free',PLANS['free'])
    used=store.count_usage(user=user)
    if p['key']=='free':remaining=max(0,FREE_LIFETIME-used)
    elif p['videos'] is None:remaining=None
    else:remaining=max(0,int(a.get('credits') or 0))
    return {'user':user,'plan':p['key'],'plan_name':p['name'],'credits':a.get('credits') or 0,'used':used,'remaining':remaining,
            'max_seconds':p['max_seconds'],'ai_motion':p['ai_motion'],'blocked':bool(a.get('blocked'))}
def check_email(email):
    e=(email or '').strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]{2,}',e):return 'Enter a valid email address'
    local,dom=e.rsplit('@',1)
    if dom in DISPOSABLE:return 'Please use a permanent email address — disposable inboxes are not accepted'
    if local in ROLE_LOCAL:return 'Please use a personal work address rather than a shared inbox'
    if len(local)<2:return 'Enter a valid email address'
    return None
def signup_guard(email,ip):
    """Refuse obvious multi-account farming at the door. Returns None or a message."""
    err=check_email(email)
    if err:return err
    if store.count_usage(ip=ip,since_days=30)>=FREE_PER_NET*2:
        return 'This network has made a lot of free videos today. Choose a plan, or email hello@braivex.com and we will lift it.'
    return None
def can_generate(user,listing_url,ip=None,conn=None):
    """(ok, reason, meta). Paid: needs credits. Free: layered guardrails. Same listing never costs twice."""
    with database.transaction(conn) as c:
        identity=c.execute('SELECT active FROM users WHERE id=%s', (database.user_id(user,c),)).fetchone()
        if not identity['active']:return False,'This account is no longer active',{}
    a=store.ensure_account(user,_default_plan(user,conn),conn=conn);p=PLANS.get(a.get('plan') or 'free',PLANS['free'])
    if a.get('blocked'):return False,'This account is on hold. Email hello@braivex.com.',{}
    if store.count_usage(user=user,listing_url=listing_url,conn=conn)>0:
        return True,None,{'free_rerun':True,'reason':'same listing already generated — no credit used'}
    if p['key']!='free':
        if p['videos'] is None:return True,None,{}
        if int(a.get('credits') or 0)<=0:return False,f'No credits left on {p["name"]}. Top up to keep going.',{'upgrade':True}
        return True,None,{}
    used=store.count_usage(user=user,conn=conn)
    if used>=FREE_LIFETIME:
        return False,f'Free plan covers {FREE_LIFETIME} videos and you have used them. Starter is $100 for 3 with AI camera motion.',{'upgrade':True}
    if ip and store.count_usage(ip=ip,since_days=30,conn=conn)>=FREE_PER_NET:
        return False,'A lot of free videos have come from this network. Choose a plan, or email hello@braivex.com and we will lift it.',{'upgrade':True}
    last=store.last_usage_ts(user,conn=conn)
    if last and (time.time()-last)<FREE_COOLDOWN_H*3600:
        wait=FREE_COOLDOWN_H-(time.time()-last)/3600
        return False,f'Free plan makes one video every {int(FREE_COOLDOWN_H)} hours — next one in about {max(1,int(wait))} h. Starter removes the wait.',{'upgrade':True}
    return True,None,{'free_remaining':FREE_LIFETIME-used-1}
def reserve(user, listing_url, job_id, ip=None, conn=None):
    """Validate and reserve once. A caller can atomically insert its job using conn."""
    if not job_id:
        raise ValueError('A job ID is required')
    with database.transaction(conn) as c:
        owner = database.user_id(user, c)
        # Serialize admission with deactivation, retaining historical owner IDs.
        identity = c.execute('SELECT active FROM users WHERE id=%s FOR SHARE', (owner,)).fetchone()
        if not identity['active']:
            raise ValueError('This account is no longer active')
        # All quota signals use deterministic advisory locks, including cross-owner
        # free network budgets. The row lock also coordinates billing/admin edits.
        keys = ['job:' + job_id, 'owner:' + owner]
        if ip:
            keys.append('net:' + store.ip_hash(ip))
        for key in sorted(keys):
            c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (key,))
        existing = c.execute('SELECT * FROM usage WHERE job_id=%s', (job_id,)).fetchone()
        if existing:
            if existing['owner_id'] != owner or existing['listing_key'] != store.listing_key(listing_url):
                raise ValueError('Job reservation does not match this request')
            if existing['refunded_at'] is not None:
                raise ValueError('This reservation was released; start a new job')
            return {'free_rerun': existing['kind'] == 'rerun', 'reserved': True}
        store.ensure_account(user, _default_plan(user, c), conn=c)
        a = c.execute('SELECT * FROM accounts WHERE owner_id=%s FOR UPDATE', (owner,)).fetchone()
        ok, reason, meta = can_generate(user, listing_url, ip, conn=c)
        if not ok:
            raise ValueError(reason)
        p = PLANS.get(a['plan'], PLANS['free'])
        rerun = bool(meta.get('free_rerun'))
        debited = not rerun and p['key'] != 'free' and p['videos'] is not None
        if debited:
            c.execute('UPDATE accounts SET credits=credits-1 WHERE owner_id=%s', (owner,))
        # The network hash is kept only where the free-tier guard counts it: free videos, not reruns or paid plans.
        store.record_usage(user, p['key'], listing_url, job_id, ip if p['key'] == 'free' and not rerun else None,
                           kind='rerun' if rerun else 'video', credits=0 if rerun else 1,
                           debited=debited, conn=c)
        return {**meta, 'reserved': True}


def consume(user, listing_url, job_id, ip=None):
    reserve(user, listing_url, job_id, ip)


def refund(job_id, conn=None):
    """Release a reservation once, in the caller's transaction when supplied."""
    with database.transaction(conn) as c:
        # Same job lock as reserve; row update arbitrates concurrent refunds.
        c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('job:' + job_id,))
        row = c.execute('UPDATE usage SET refunded_at=%s WHERE job_id=%s AND refunded_at IS NULL RETURNING *',
                        (time.time(), job_id)).fetchone()
        if not row:
            return False
        if row['debited']:
            c.execute('UPDATE accounts SET credits=credits+%s WHERE owner_id=%s', (row['credits'], row['owner_id']))
        return True

def public_plans():
    return [dict(PLANS[k]) for k in ORDER]
PRODUCTS=[
 {'name':'Loculens','tagline':'Local SEO and review intelligence','url':'https://loculens.braivex.com'},
 {'name':'HireSieve','tagline':'Sift the CVs that actually fit','url':'https://hiresieve.braivex.com'},
 # briefsieve.braivex.com has no DNS record yet (checked 20 Sep 2026) — point at the studio site until it does
 {'name':'BriefSieve','tagline':'Turn long briefs into decisions','url':'https://braivex.com'},
 {'name':'HouSieve','tagline':'Property data, sifted','url':'https://housieve.braivex.com'},
 {'name':'Braivex','tagline':'The studio behind them','url':'https://braivex.com'},
]
