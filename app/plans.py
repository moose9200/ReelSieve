"""Plans, credits and free-tier guardrails for ReelSieve.

Layered abuse prevention (industry practice: never rely on one signal):
  1. account        — free tier gets FREE_LIFETIME videos, ever
  2. IP network     — HMAC of /24 (v4) or /64 (v6): FREE_PER_NET free videos per 30 days across all accounts
  3. device         — HMAC of a client fingerprint: FREE_PER_DEVICE free videos
  4. cooldown       — at most one free video per FREE_COOLDOWN_H hours per account
  5. email hygiene  — disposable/temporary domains refused at signup
  6. idempotent     — re-running the SAME listing never costs a second free credit
Raw IPs/fingerprints are never stored (see store.py). Paid plans skip 2–4; they are spend-limited by credits.
"""
import os,re,time
from app import store
PLANS={
 'free':      {'key':'free','name':'Free','price_label':'$0','period':'2 videos','videos':2,'price_usd':0,
               'max_seconds':60,'ai_motion':False,'vertical':False,'drive':False,'outro':'ReelSieve',
               'features':['2 videos total','Up to 60 seconds','1080p MP4 download','Route-ordered walkthrough','Real review captions','ReelSieve outro'],
               'cta_label':'Start free','cta_href':'/signup'},
 'starter':   {'key':'starter','name':'Starter','price_label':'$100','period':'one-off · 3 videos','videos':3,'price_usd':100,
               'max_seconds':90,'ai_motion':True,'vertical':True,'drive':True,'outro':'own','highlight':True,
               'features':['3 videos','AI camera motion (Higgsfield Seedance)','Up to 90 seconds','1080p + 9:16 vertical cut','Google Drive delivery','No ReelSieve outro'],
               'cta_label':'Choose Starter','cta_href':'/signup?plan=starter'},
 'commercial':{'key':'commercial','name':'Commercial','price_label':'$500','period':'one-off · 20 videos','videos':20,'price_usd':500,
               'max_seconds':90,'ai_motion':True,'vertical':True,'drive':True,'outro':'own',
               'features':['20 videos','AI camera motion','Up to 90 seconds','Bulk queue','Brand your own outro','Priority rendering','Email + Drive delivery'],
               'cta_label':'Choose Commercial','cta_href':'/signup?plan=commercial'},
 'enterprise':{'key':'enterprise','name':'Enterprise','price_label':"Let's talk",'period':'unlimited · your brand','videos':None,'price_usd':None,
               'max_seconds':90,'ai_motion':True,'vertical':True,'drive':True,'outro':'own',
               'features':['Unlimited volume','Your brand throughout','API access','Bulk import from a spreadsheet','Dedicated support','White-label option'],
               'cta_label':'Talk to us','cta_href':'mailto:hello@braivex.com?subject=ReelSieve%20Enterprise'},
}
ORDER=['free','starter','commercial','enterprise']
FREE_LIFETIME=int(os.getenv('FREE_LIFETIME','2'))
FREE_PER_NET=int(os.getenv('FREE_PER_NET','12'))  # offices and mobile carriers share a /24; the per-account cap is the real control
FREE_PER_DEVICE=int(os.getenv('FREE_PER_DEVICE','2'))
FREE_COOLDOWN_H=float(os.getenv('FREE_COOLDOWN_H','0'))  # 2 lifetime videos is the real cap; a cooldown only hurts first-run UX
DISPOSABLE=set('''mailinator.com guerrillamail.com 10minutemail.com tempmail.com temp-mail.org yopmail.com throwawaymail.com
sharklasers.com getnada.com trashmail.com maildrop.cc dispostable.com fakeinbox.com mailnesia.com mintemail.com
moakt.com emailondeck.com tempr.email discard.email spamgourmet.com mytemp.email burnermail.io grr.la spam4.me
mailcatch.com inboxbear.com tempmailo.com tmpmail.org luxusmail.org anonbox.net'''.split())
ROLE_LOCAL={'admin','info','support','contact','sales','billing','noreply','no-reply','postmaster','webmaster','abuse','test'}
def _default_plan(user):
    """Admins (the people running this install) are never metered; everyone else starts free."""
    try:
        from app import auth;return 'enterprise' if auth.role(user)=='admin' else 'free'
    except Exception:return 'free'
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
            'max_seconds':p['max_seconds'],'ai_motion':p['ai_motion'],'vertical':p['vertical'],'blocked':bool(a.get('blocked'))}
def check_email(email):
    e=(email or '').strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]{2,}',e):return 'Enter a valid email address'
    local,dom=e.rsplit('@',1)
    if dom in DISPOSABLE:return 'Please use a permanent email address — disposable inboxes are not accepted'
    if local in ROLE_LOCAL:return 'Please use a personal work address rather than a shared inbox'
    if len(local)<2:return 'Enter a valid email address'
    return None
def signup_guard(email,ip,fp):
    """Refuse obvious multi-account farming at the door. Returns None or a message."""
    err=check_email(email)
    if err:return err
    if store.count_usage(ip=ip,since_days=30)>=FREE_PER_NET*2:
        return 'This network has made a lot of free videos today. Choose a plan, or email hello@braivex.com and we will lift it.'
    return None
def can_generate(user,listing_url,ip=None,fp=None):
    """(ok, reason, meta). Paid: needs credits. Free: layered guardrails. Same listing never costs twice."""
    a=store.ensure_account(user,_default_plan(user));p=PLANS.get(a.get('plan') or 'free',PLANS['free'])
    if a.get('blocked'):return False,'This account is on hold. Email hello@braivex.com.',{}
    if store.count_usage(user=user,listing_url=listing_url)>0:
        return True,None,{'free_rerun':True,'reason':'same listing already generated — no credit used'}
    if p['key']!='free':
        if p['videos'] is None:return True,None,{}
        if int(a.get('credits') or 0)<=0:return False,f'No credits left on {p["name"]}. Top up to keep going.',{'upgrade':True}
        return True,None,{}
    used=store.count_usage(user=user)
    if used>=FREE_LIFETIME:
        return False,f'Free plan covers {FREE_LIFETIME} videos and you have used them. Starter is $100 for 3 with AI camera motion.',{'upgrade':True}
    if ip and store.count_usage(ip=ip,since_days=30)>=FREE_PER_NET:
        return False,'A lot of free videos have come from this network. Choose a plan, or email hello@braivex.com and we will lift it.',{'upgrade':True}
    if fp and store.count_usage(fp=fp)>=FREE_PER_DEVICE:
        return False,'The free allowance for this device is used up. Choose a plan to continue.',{'upgrade':True}
    last=store.last_usage_ts(user)
    if last and (time.time()-last)<FREE_COOLDOWN_H*3600:
        wait=FREE_COOLDOWN_H-(time.time()-last)/3600
        return False,f'Free plan makes one video every {int(FREE_COOLDOWN_H)} hours — next one in about {max(1,int(wait))} h. Starter removes the wait.',{'upgrade':True}
    return True,None,{'free_remaining':FREE_LIFETIME-used-1}
def consume(user,listing_url,job_id,ip=None,fp=None):
    a=store.ensure_account(user,_default_plan(user));p=PLANS.get(a.get('plan') or 'free',PLANS['free'])
    if store.count_usage(user=user,listing_url=listing_url)>0:
        store.record_usage(user,p['key'],listing_url,job_id,ip,fp,kind='rerun',credits=0);return
    store.record_usage(user,p['key'],listing_url,job_id,ip,fp,kind='video',credits=1)
    if p['key']!='free' and p['videos'] is not None:store.add_credits(user,-1)
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
