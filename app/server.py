#!/usr/bin/env python3
"""Listing Reel by Braivex — paste an Airbnb URL, get a 30 s cinematic reel, hand it to the host via Airbnb messaging.
Run: .venv/bin/uvicorn app.server:app --port 8787   (from the project root)"""
import os,re,json,uuid,threading,time,traceback,secrets
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv,dotenv_values
from urllib.parse import quote
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;ENV=ROOT/'.env.local';JOBS=Path(os.getenv('JOBS_DIR') or (ROOT/'jobs'));JOBS.mkdir(parents=True,exist_ok=True);PORT=int(os.getenv('PORT','8787'))
load_dotenv(ENV)
if not os.getenv('PUBLIC_BASE_URL') and os.getenv('RAILWAY_PUBLIC_DOMAIN'):os.environ['PUBLIC_BASE_URL']='https://'+os.environ['RAILWAY_PUBLIC_DOMAIN']
from app import pipeline,hostmsg,search as listing_search,gdrive,auth,store,plans,cohost,linkedin,billing
app=FastAPI(title='ReelSieve by Braivex');app.mount('/static',StaticFiles(directory=HERE/'static'),name='static')
tpl=Jinja2Templates(directory=HERE/'templates');tpl.env.autoescape=True
from starlette.middleware.base import BaseHTTPMiddleware
def _role_admin(user):
    try:return bool(user) and auth.role(user)=='admin'
    except Exception:return False
PUBLIC_PREFIXES=('/static/','/media/','/oauth/google/callback','/favicon.ico')
PUBLIC_EXACT=('/','/login','/signup','/setup','/logout','/forgot','/healthz','/privacy','/terms')
class LoginGate(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        path=request.url.path;request.state.user=None
        if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT:
            request.state.user=auth.check(request.cookies.get(auth.COOKIE,''));request.state.is_admin=_role_admin(request.state.user);return await call_next(request)
        user=auth.check(request.cookies.get(auth.COOKIE,''))
        if not user:
            if path.startswith('/api/'):return JSONResponse({'detail':'Sign in required'},status_code=401)
            from urllib.parse import quote as _q;target='/login' if auth.has_account() else '/setup'
            return RedirectResponse(target+'?next='+_q(str(request.url.path)+('?'+str(request.url.query) if request.url.query else '')),status_code=303)
        if request.method in ('POST','PUT','DELETE') and not path.startswith('/api/'):
            form=await request.form()
            if not auth.csrf_ok(request.cookies.get(auth.COOKIE,''),form.get('csrf')):return HTMLResponse('Invalid or expired form token — reload and try again',status_code=403)
            # BaseHTTPMiddleware has already drained the body, so the endpoint's own request.form() would come back
            # empty. Hand the parsed form down through the shared scope instead.
            request.scope['_form']=dict(form)
        request.state.user=user;request.state.is_admin=_role_admin(user);return await call_next(request)
app.add_middleware(LoginGate)
def _secure(request):return request.url.scheme=='https' or 'railway.app' in request.headers.get('host','') or 'https' in request.headers.get('x-forwarded-proto','')
def _ip(request):return (request.headers.get('x-forwarded-for','').split(',')[0].strip() or (request.client.host if request.client else '?'))
def _login_ctx(request,**kw):
    tok=request.cookies.get(auth.COOKIE,'');return {'csrf':auth.csrf_token(tok),'allow_setup':not auth.has_account(),**kw}
def _set_session(resp,request,user,long=True):
    tok,ttl=auth.issue(user,long);resp.set_cookie(auth.COOKIE,tok,max_age=ttl,httponly=True,samesite='lax',secure=_secure(request));return resp
@app.get('/favicon.ico')
def favicon():return FileResponse(HERE/'static'/'brand'/'favicon.ico',media_type='image/x-icon')
@app.get('/healthz')
def healthz():return {'ok':True}
@app.get('/setup',response_class=HTMLResponse)
def setup_page(request:Request,next:str='/app'):
    if auth.has_account():return RedirectResponse('/login?notice=exists',status_code=303)
    return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=True,next=next))
@app.post('/setup')
async def setup_post(request:Request):
    if auth.has_account():return RedirectResponse('/login',status_code=303)
    f=await request.form();u=(f.get('user') or '').strip();p1=f.get('password') or '';p2=f.get('password2') or '';nxt=f.get('next') or '/'
    if not auth.csrf_ok(request.cookies.get(auth.COOKIE,''),f.get('csrf')):return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=True,user=u,error='Form expired — try again'),status_code=400)
    if p1!=p2:return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=True,user=u,error='Passwords do not match'),status_code=400)
    try:auth.create_user(u,p1,'admin')
    except ValueError as e:return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=True,user=u,error=str(e)),status_code=400)
    store.ensure_account(u,'enterprise',_ip(request),None);store.set_plan(u,'enterprise')
    return _set_session(RedirectResponse(nxt if nxt.startswith('/') else '/app',status_code=303),request,u,True)
@app.get('/login',response_class=HTMLResponse)
def login_page(request:Request,next:str='/app',notice:str=''):
    if not auth.has_account():return RedirectResponse('/setup',status_code=303)
    if request.state.user:return RedirectResponse(next if next.startswith('/') else '/app',status_code=303)
    msg={'exists':'An account already exists — sign in.','out':'You have been signed out.','created':'Account created — sign in.'}.get(notice,'')
    return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=False,next=next,notice=msg))
@app.post('/login')
async def login_post(request:Request):
    f=await request.form();u=(f.get('user') or '').strip();p=f.get('password') or '';nxt=f.get('next') or '/app';remember=f.get('remember')=='1';ip=_ip(request)
    if not nxt.startswith('/'):nxt='/app'
    if not auth.csrf_ok(request.cookies.get(auth.COOKIE,''),f.get('csrf')):return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=False,next=nxt,user=u,error='Form expired — try again'),status_code=400)
    if auth.too_many(ip):return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=False,next=nxt,user=u,error='Too many attempts — wait 10 minutes'),status_code=429)
    if not auth.verify(u,p):
        auth.record_fail(ip);time.sleep(0.6);return tpl.TemplateResponse(request,'login.html',_login_ctx(request,setup=False,next=nxt,user=u,error='Wrong email or password'),status_code=401)
    auth.clear_fails(ip);return _set_session(RedirectResponse(nxt,status_code=303),request,u,remember)
@app.get('/logout')
def logout():
    r=RedirectResponse('/login?notice=out',status_code=303);r.delete_cookie(auth.COOKIE);return r
@app.get('/forgot',response_class=HTMLResponse)
def forgot(request:Request):return tpl.TemplateResponse(request,'forgot.html',{})
@app.post('/api/account/password')
async def change_password(request:Request):
    b=await request.json();cur=b.get('current') or '';new=b.get('new') or ''
    if not auth.verify(request.state.user,cur):raise HTTPException(400,'Current password is wrong')
    try:auth.set_password(request.state.user,new)
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True}
def _require_admin(request):
    if auth.role(request.state.user)!='admin':raise HTTPException(403,'Admin only')
@app.get('/api/account')
def api_account(request:Request):return plans.account_view(request.state.user)
@app.post('/api/users/plan')
async def api_user_plan(request:Request):
    _require_admin(request);b=await request.json();u=(b.get('user') or '').strip().lower();pl=b.get('plan') or 'free'
    if pl not in plans.PLANS:raise HTTPException(400,'Unknown plan')
    cr=b.get('credits');store.ensure_account(u);store.set_plan(u,pl,int(cr) if cr not in (None,'') else plans.PLANS[pl]['videos'] or 0)
    return {'ok':True,'account':plans.account_view(u)}
@app.get('/api/users')
def api_users(request:Request):
    _require_admin(request);accs={a['user']:a for a in store.all_accounts()}
    return {'users':[{**u,**{k:accs.get(u['user'],{}).get(k) for k in ('plan','credits','blocked')}} for u in auth.users()],'me':request.state.user,'plan_keys':plans.ORDER}
@app.post('/api/users')
async def api_users_add(request:Request):
    _require_admin(request);b=await request.json()
    try:auth.create_user(b.get('user',''),b.get('password',''),'admin' if b.get('role')=='admin' else 'member')
    except ValueError as e:raise HTTPException(400,str(e))
    return {'users':auth.users()}
@app.post('/api/users/delete')
async def api_users_del(request:Request):
    _require_admin(request);b=await request.json()
    try:auth.delete_user(b.get('user',''),request.state.user)
    except ValueError as e:raise HTTPException(400,str(e))
    return {'users':auth.users()}
@app.post('/api/users/password')
async def api_users_pw(request:Request):
    _require_admin(request);b=await request.json()
    try:auth.set_password(b.get('user',''),b.get('password',''))
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True}
SECRET_KEYS=['HF_KEY','GOOGLE_CLIENT_SECRET','BILLING_WEBHOOK_SECRET'];SETTING_KEYS=['HF_KEY','PUBLIC_BASE_URL','DEFAULT_MESSAGE','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','GDRIVE_FOLDER','CHECKOUT_STARTER','CHECKOUT_COMMERCIAL','BILLING_WEBHOOK_SECRET','BILLING_NOTE']
HINTS={'HF_KEY':'Higgsfield API key, key-id:key-secret','PUBLIC_BASE_URL':'Where this app is reachable from the internet (optional; tunnel is used otherwise)','DEFAULT_MESSAGE':'Template for the Airbnb message. Tokens: {host_name} {listing_title} {city} {search_phrase} {reel_link}. Keep it link-free — Airbnb filters URLs before a booking','GOOGLE_CLIENT_ID':'OAuth client ID from Google Cloud Console (Web application)','GOOGLE_CLIENT_SECRET':'OAuth client secret','GDRIVE_FOLDER':'Drive folder name for uploads (default: Listing Reels)','CHECKOUT_STARTER':'Hosted checkout link for Starter ($100) — Skydo InstaLink, Dodo, Razorpay or PayPal','CHECKOUT_COMMERCIAL':'Hosted checkout link for Commercial ($500)','BILLING_WEBHOOK_SECRET':'Shared secret your payment provider signs webhooks with','BILLING_NOTE':'Line shown to customers who choose invoice (e.g. how fast you send it)'}
DAILY_CAP=int(os.getenv('OUTREACH_DAILY_CAP','5'))
COHOST_MESSAGE=("Hi {name} — I'm Hemant from ReelSieve (Braivex). I make short cinematic walkthrough videos for short-let "
 "listings, built from the photos and reviews already on them. I made one for a {city} property this week and thought of you.\n\n"
 "Happy to make one for {listing_title} free so you can see it — no strings, no card. If it is useful I do them at volume for operators.\n\n"
 "If you'd rather I sent it elsewhere, tell me where and I will.")
_jobs={};_lock=threading.Lock();_airbnb_cache={'t':0,'v':{'connected':False}}
def default_message():return os.getenv('DEFAULT_MESSAGE') or hostmsg.DEFAULT_MESSAGE
def airbnb_status(max_age=60):
    if time.time()-_airbnb_cache['t']>max_age:
        v=hostmsg.status()
        if not v.get('busy'):_airbnb_cache.update(t=time.time(),v=v)
    return _airbnb_cache['v']
def settings_view():
    cur=dotenv_values(ENV) if ENV.exists() else {}
    s={k.lower():{'configured':bool((cur.get(k) or os.getenv(k) or '').strip()),'hint':HINTS[k],'value':'' if k in SECRET_KEYS else (cur.get(k) or os.getenv(k) or '')} for k in SETTING_KEYS}
    s['default_message']['value']=s['default_message']['value'] or hostmsg.DEFAULT_MESSAGE
    s['airbnb_connected']=airbnb_status().get('connected',False);s['tunnel']=hostmsg.tunnel_status();s['gdrive']=gdrive.status();return s
def save_settings(form):
    """Merge into .env.local (0600). Blank secret = keep existing. Values never logged or rendered."""
    cur=dotenv_values(ENV) if ENV.exists() else {}
    for k in SETTING_KEYS:
        v=(form.get(k.lower()) or '').strip().strip('\'"')
        if k in SECRET_KEYS:
            if v:
                if k=='HF_KEY' and not re.fullmatch(r'[A-Za-z0-9_-]{8,}:[A-Za-z0-9_-]{8,}',v):raise HTTPException(400,'HF_KEY must be key-id:key-secret')
                cur[k]=v
        else:cur[k]=v.replace('\n','\\n')
    ENV.write_text('\n'.join(f'{k}={v}' for k,v in cur.items() if v)+'\n');os.chmod(ENV,0o600)
    for k,v in cur.items():
        if v:os.environ[k]=v.replace('\\n','\n')
        elif k in os.environ and k not in SECRET_KEYS:del os.environ[k]
KEEP_LOCAL=os.getenv('KEEP_LOCAL_AFTER_UPLOAD','0').lower() in ('1','true','yes')
def purge_local(jid):
    """After a successful Drive upload: remove every video/render artefact from this server. Keeps job.json + listing.json only."""
    import shutil;d=JOBS/jid;freed=0
    for pth in list(d.iterdir()):
        if pth.name in ('job.json','listing.json','result.json'):continue
        try:
            if pth.is_dir():freed+=sum(f.stat().st_size for f in pth.rglob('*') if f.is_file());shutil.rmtree(pth,ignore_errors=True)
            else:freed+=pth.stat().st_size;pth.unlink()
        except Exception:pass
    return freed
def drive_fields(j):
    fid=j.get('drive_id')
    if not fid:return {}
    return {'drive_embed':f'https://drive.google.com/file/d/{fid}/preview','drive_download':f'https://drive.google.com/uc?export=download&id={fid}','drive_thumb':f'https://drive.google.com/thumbnail?id={fid}&sz=w640'}
def is_admin(request):
    v=getattr(request.state,'is_admin',None)
    return _role_admin(request.state.user) if v is None else bool(v)
def _deny():raise HTTPException(404)
def owns(j,user,admin=False):
    """A job belongs to the user who made it. Legacy jobs (no user field) belong to admins only."""
    o=(j or {}).get('user')
    return bool(admin) if not o else (o==user)
def job_public(j):return {k:v for k,v in j.items() if k!='thread'}
def persist(j):(JOBS/j['id']/'job.json').write_text(json.dumps(job_public(j),indent=1))
def load_jobs(user=None,admin=False):
    out=[]
    for d in sorted(JOBS.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
        f=d/'job.json'
        if f.exists():
            try:
                j=json.loads(f.read_text())
                if user is None or owns(j,user,admin):out.append(j)
            except Exception:pass
    return out
def reel_link_for(j):
    if j.get('drive_link') and (j.get('local_deleted') or not j.get('video_url')):return j['drive_link']
    base=hostmsg.public_base()
    if base and j.get('video_url'):
        t=j.get('share_token') or share_token(j.get('id') or '')
        return f"{base}{j['video_url']}"+(f'?t={t}' if t else '')
    return j.get('drive_link') or None
def search_phrase(j):
    L=j.get('listing') or {};title=re.split(r'\s[|·-]\s',(L.get('title') or ''))[0].strip();city=(L.get('city') or '').strip()
    return ' '.join(x for x in [title,city,'video walkthrough ReelSieve'] if x).strip() or 'ReelSieve'
def finalize_message(j):
    link=reel_link_for(j);msg=(j.get('message') or default_message());L=j.get('listing') or {}
    if '{reel_link}' in msg and '{search_phrase}' not in msg and 'ReelSieve' not in msg:msg=default_message()   # legacy link-based template → link-free default
    host=(L.get('host') or '').strip();msg=msg.replace('{host_name}',host if host else 'there').replace('Hi there!','Hi!')
    msg=msg.replace('{listing_title}',L.get('title') or 'your listing').replace('{city}',L.get('city') or '').replace('{search_phrase}',search_phrase(j))
    return msg.replace('{reel_link}',link) if link else msg.replace('{reel_link}','(reel link not available yet)')
def enrich(j):
    """Derived, non-persisted fields for the UI."""
    j=dict(j);lid=(j.get('listing') or {}).get('id') or (re.search(r'/rooms/(\d+)',j.get('url','')) or [None,None])[1]
    j.update(drive_fields(j));j['contact_url']=hostmsg.contact_url(lid) if lid else None;j['reel_link']=reel_link_for(j);j['search_phrase']=search_phrase(j);j['youtube_title']=search_phrase(j).replace(' ReelSieve',' — by ReelSieve');j['message_final']=finalize_message(j);return j
def run_job(jid,url,ai_motion,renderer='v2',max_seconds=None):
    j=_jobs[jid];steps=['Fetching','Reviews','Downloaded','Scored','Audit','AI motion plan','Seedance','Estimating depth','Rendering','Rendered','Uploading']
    def cb(msg):
        with _lock:
            j['log'].append(time.strftime('%H:%M:%S ')+msg);j['step']=msg
            for i,s in enumerate(steps):
                if msg.startswith(s):j['progress']=max(j['progress'],int(8+i*9))
            persist(j)
    try:
        j['status']='running';persist(j)
        res=pipeline.run(url,JOBS/jid,None,ai_motion,cb,None,renderer,max_seconds)
        with _lock:
            j.update(status='done',progress=100,step='Done',video_url=f"/media/{jid}/{Path(res['video']).name}",listing={**res['listing'],'location':res['listing'].get('city')},duration=res['duration'],audit=res.get('audit'),ai_plan=res.get('ai_plan'),selection=res.get('selection'),photo_scores=res.get('photo_scores'))
            if j.get('send_to_host'):
                j['host_status']='skipped';j['host_error']='Ready — open the pre-filled Airbnb message below and press Send there'
            else:j['host_status']='skipped';j['host_error']='not requested'
            j['log'].append(time.strftime('%H:%M:%S ')+'Reel ready. Host message is prepared for you to review and send.')
        persist(j)
        if gdrive.status().get('connected'):
            try:
                cb('Uploading to Google Drive');info=gdrive.upload(res['video'],(res['listing'].get('url') or url),description=f"{res['listing'].get('title','')} · {res['listing'].get('city','')} · Listing Reel by Braivex")
                with _lock:j.update(drive_status='uploaded',drive_link=info.get('webViewLink'),drive_name=info.get('name'),drive_id=info.get('id'))
                cb(f"Google Drive: uploaded as {info.get('name')}")
                if info.get('id') and not KEEP_LOCAL:
                    freed=purge_local(jid)
                    with _lock:j.update(local_deleted=True,video_url=None)
                    cb(f'Removed local copy from this server ({freed/1e6:.0f} MB freed) — the reel now lives in Google Drive')
            except Exception as e:
                with _lock:j.update(drive_status='failed',drive_error=f'{type(e).__name__}: {str(e)[:160]}')
                cb(f'Google Drive upload failed: {type(e).__name__}: {str(e)[:120]}')
        else:
            with _lock:j.update(drive_status='skipped',drive_error='Google Drive not connected (Settings)')
    except Exception as e:
        with _lock:j.update(status='failed',error=f'{type(e).__name__}: {str(e)[:300]}',step='Failed');j['log'].append(traceback.format_exc()[-600:])
    persist(j)
def _migrate_jobs_owner():
    """Jobs made before multi-user existed have no owner. Give them to the first admin so they stay visible
    to the operator and invisible to everyone else. Runs once at startup, cheap and idempotent."""
    try:
        admins=[u['user'] for u in auth.users() if u.get('role')=='admin']
        if not admins:return 0
        owner=admins[0];n=0
        for d in JOBS.iterdir():
            f=d/'job.json'
            if not f.exists():continue
            try:j=json.loads(f.read_text())
            except Exception:continue
            if not j.get('user'):
                j['user']=owner;f.write_text(json.dumps(j,indent=1));n+=1
        return n
    except Exception:return 0
_MIGRATED=_migrate_jobs_owner()
@app.get('/',response_class=HTMLResponse)
def landing(request:Request):
    return tpl.TemplateResponse(request,'landing.html',{'signed_in':bool(request.state.user),'user':request.state.user,
        'plans':plans.public_plans(),'products':plans.PRODUCTS,'sample_video':os.getenv('SAMPLE_VIDEO_URL') or None,'sample_poster':os.getenv('SAMPLE_POSTER_URL') or None})
@app.get('/privacy',response_class=HTMLResponse)
def privacy(request:Request):return tpl.TemplateResponse(request,'legal.html',{'kind':'privacy'})
@app.get('/terms',response_class=HTMLResponse)
def terms(request:Request):return tpl.TemplateResponse(request,'legal.html',{'kind':'terms'})
@app.get('/signup',response_class=HTMLResponse)
def signup_page(request:Request,plan:str='',url:str=''):
    if request.state.user:return RedirectResponse('/app',status_code=303)
    return tpl.TemplateResponse(request,'signup.html',_login_ctx(request,plan=plan,url=url,plans=plans.public_plans()))
@app.post('/signup')
async def signup_post(request:Request):
    f=await request.form();u=(f.get('user') or '').strip();p1=f.get('password') or '';p2=f.get('password2') or ''
    plan=(f.get('plan') or 'free').strip();url=(f.get('url') or '').strip();ip=_ip(request);fp=(f.get('fp') or '')[:400]
    ctx=lambda err:tpl.TemplateResponse(request,'signup.html',_login_ctx(request,user=u,plan=plan,url=url,error=err,plans=plans.public_plans()),status_code=400)
    if not auth.csrf_ok(request.cookies.get(auth.COOKIE,''),f.get('csrf')):return ctx('Form expired — try again')
    if p2 and p1!=p2:return ctx('Passwords do not match')
    guard=plans.signup_guard(u,ip,fp)
    if guard:return ctx(guard)
    try:auth.create_user(u,p1,'member')
    except ValueError as e:return ctx(str(e))
    store.ensure_account(u,plans._default_plan(u),ip,fp)   # first signup on a fresh install is the admin, and admins aren't metered
    nxt='/app'+(('?url='+quote(url)) if url else '')
    if plan in ('starter','commercial'):nxt='/upgrade?plan='+plan
    return _set_session(RedirectResponse(nxt,status_code=303),request,u,True)
@app.get('/upgrade',response_class=HTMLResponse)
def upgrade(request:Request,plan:str='',ref:str=''):
    return tpl.TemplateResponse(request,'upgrade.html',{'plan':plan,'plans':plans.public_plans(),
        'link':billing.checkout_link(plan) if plan else '','ref':ref,'order':billing.get_order(ref) if ref else None,
        'billing_note':os.getenv('BILLING_NOTE') or 'We send the invoice within a few hours and add your credits the moment it clears.',
        'csrf':auth.csrf_token(request.cookies.get(auth.COOKIE,'')),
        'account':plans.account_view(request.state.user) if request.state.user else None,
        'orders':billing.orders(request.state.user)[:5] if request.state.user else []})
@app.post('/api/billing/request')
async def billing_request(request:Request):
    b=await request.json();pl=(b.get('plan') or '').strip()
    if pl not in ('starter','commercial'):raise HTTPException(400,'Choose Starter or Commercial')
    try:o=billing.create_order(request.state.user,pl,b.get('provider') or 'invoice',(b.get('note') or '')[:400],{'ip':_ip(request)})
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True,'order':o}
@app.get('/api/billing/orders')
def billing_orders(request:Request,all:int=0):
    if all and is_admin(request):return {'orders':billing.orders(),'pending':billing.pending_count()}
    return {'orders':billing.orders(request.state.user)}
@app.post('/api/billing/settle')
async def billing_settle(request:Request):
    _require_admin(request);b=await request.json()
    try:o=billing.settle((b.get('ref') or '').strip(),by=request.state.user)
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True,'order':o,'account':plans.account_view(o['user'])}
@app.post('/api/billing/cancel')
async def billing_cancel(request:Request):
    _require_admin(request);b=await request.json()
    return {'ok':True,'order':billing.cancel((b.get('ref') or '').strip(),b.get('note') or 'cancelled')}
@app.post('/api/billing/webhook/{provider}')
async def billing_webhook(provider:str,request:Request):
    """Provider-agnostic: HMAC-SHA256 over the raw body, our order ref anywhere in the payload."""
    raw=await request.body()
    sig=(request.headers.get('x-signature') or request.headers.get('x-razorpay-signature') or
         request.headers.get('x-skydo-signature') or request.headers.get('x-dodo-signature') or
         request.headers.get('x-webhook-signature') or '')
    if not billing.verify(provider,raw,sig):raise HTTPException(401,'Bad signature')
    try:payload=json.loads(raw or b'{}')
    except Exception:raise HTTPException(400,'Bad payload')
    ref=billing.ref_from_payload(payload)
    if not ref:raise HTTPException(400,'No order reference in payload')
    try:o=billing.settle(ref,by=f'webhook:{provider}',provider=provider)
    except ValueError as e:raise HTTPException(404,str(e))
    return {'ok':True,'ref':o['ref'],'status':o['status']}
@app.get('/app',response_class=HTMLResponse)
def index(request:Request,url:str=''):
    jobs=[{'id':j['id'],'status':j['status'],'title':(j.get('listing') or {}).get('title') or j.get('url'),'location':(j.get('listing') or {}).get('location'),'created':j.get('created'),'video_url':j.get('video_url')} for j in load_jobs(request.state.user,is_admin(request))[:12]]
    return tpl.TemplateResponse(request,'index.html',{'jobs':jobs,'hf_configured':bool(os.getenv('HF_KEY')),'airbnb_connected':airbnb_status().get('connected',False),'public_url_ok':bool(hostmsg.public_base()),'default_message':default_message(),'prefill_url':url,'account':plans.account_view(request.state.user)})
@app.post('/api/jobs')
async def create_job(request:Request):
    b=await request.json();url=(b.get('url') or '').strip();ai=bool(b.get('ai_motion'));renderer='v3' if b.get('style')=='tutorial' else 'v2'
    if ai and b.get('ai_resolution') in ('720p','1080p'):os.environ['AI_RESOLUTION']=b['ai_resolution']
    try:pipeline.listing_id(url)
    except ValueError as e:raise HTTPException(400,str(e))
    user=request.state.user;acct=plans.account_view(user);ip=_ip(request);fp=(b.get('fp') or '')[:400]
    ok,why,meta=plans.can_generate(user,url,ip,fp)
    if not ok:raise HTTPException(402 if meta.get('upgrade') else 403,why)
    P=plans.PLANS[acct['plan']]
    if ai and not P['ai_motion']:ai=False
    jid=uuid.uuid4().hex[:10];(JOBS/jid).mkdir(parents=True,exist_ok=True)
    plans.consume(user,url,jid,ip,fp)
    j={'id':jid,'url':url,'ai_motion':ai,'style':renderer,'user':user,'plan':acct['plan'],'max_seconds':P['max_seconds'],'send_to_host':bool(b.get('send_to_host',True)),'message':(b.get('message') or default_message()).strip(),'status':'queued','progress':2,'step':'Queued','log':[],'video_url':None,'error':None,'listing':{'url':url},'created':time.strftime('%Y-%m-%d %H:%M'),'host_status':None,'host_error':None}
    _jobs[jid]=j;persist(j);threading.Thread(target=run_job,args=(jid,url,ai,renderer,P['max_seconds']),daemon=True).start();return {'id':jid,'account':plans.account_view(user)}
def get_job(jid,user=None,admin=False):
    j=_jobs.get(jid)
    if j:
        with _lock:j=job_public(j)
    else:
        f=JOBS/jid/'job.json'
        if not f.exists():raise HTTPException(404)
        j=json.loads(f.read_text())
    if user is not None and not owns(j,user,admin):raise HTTPException(404)
    return j
@app.get('/api/jobs/{jid}')
def job_api(request:Request,jid:str):return enrich(get_job(jid,request.state.user,is_admin(request)))
@app.get('/jobs/{jid}',response_class=HTMLResponse)
def job_page(request:Request,jid:str):return tpl.TemplateResponse(request,'job.html',{'job':enrich(get_job(jid,request.state.user,is_admin(request)))})
@app.post('/api/jobs/{jid}/send-to-host')
async def send_to_host(jid:str,request:Request):
    """Opens Airbnb's contact-host form pre-filled in a headed browser using the connected Airbnb session. Never presses Send."""
    b=await request.json();j=_jobs.get(jid) or get_job(jid);_=owns(j,request.state.user,is_admin(request)) or _deny()
    if j.get('status')!='done':raise HTTPException(400,'Reel not ready yet')
    if not airbnb_status(max_age=0).get('connected'):raise HTTPException(400,'Airbnb not connected — connect it in Settings')
    lid=(j.get('listing') or {}).get('id') or pipeline.listing_id(j['url']);msg=(b.get('message') or '').strip() or finalize_message(j)
    link=reel_link_for(j)
    if link and '{reel_link}' in msg:msg=msg.replace('{reel_link}',link)
    st,info=hostmsg.open_draft(lid,msg)
    j['host_status']=st;j['host_error']=info;j['message']=msg;_jobs[jid]=j if jid not in _jobs else _jobs[jid]
    if jid in _jobs:_jobs[jid].update(host_status=st,host_error=info,message=msg)
    persist(_jobs.get(jid,j));return enrich(j)
@app.post('/api/jobs/{jid}/opened-in-browser')
async def opened_in_browser(jid:str,request:Request):
    """Records that the user opened the contact form in their own browser; the message was copied client-side."""
    b=await request.json();j=_jobs.get(jid) or get_job(jid);_=owns(j,request.state.user,is_admin(request)) or _deny();msg=(b.get('message') or '').strip() or finalize_message(j)
    upd=dict(host_status='draft',host_error='opened in your browser — paste and press Send',message=msg)
    if jid in _jobs:_jobs[jid].update(upd);persist(_jobs[jid]);return enrich(job_public(_jobs[jid]))
    j.update(upd);(JOBS/jid/'job.json').write_text(json.dumps(j,indent=1));return enrich(j)
_places_cache={}
@app.get('/api/places')
def api_places(q:str=''):
    """Location autocomplete for the picker (Photon / OpenStreetMap, no key). Returns 'City, Country' values Airbnb's search accepts."""
    import httpx as _hx
    q=q.strip()
    if len(q)<2:return {'items':[]}
    if q.lower() in _places_cache:return _places_cache[q.lower()]
    try:
        r=_hx.get('https://photon.komoot.io/api/',params={'q':q,'limit':10,'lang':'en','osm_tag':'place'},headers={'User-Agent':'ListingReel/1.0 (braivex.com)'},timeout=8);feats=r.json().get('features',[])
    except Exception:return {'items':[]}
    out=[];seen=set()
    for f in feats:
        pr=f.get('properties',{});name=pr.get('name');country=pr.get('country');region=pr.get('state') or pr.get('county') or ''
        if not name or not country or pr.get('osm_value') not in ('city','town','village','suburb','borough','quarter','neighbourhood','island','county','state','municipality'):continue
        value=f'{name}, {country}';label=', '.join(x for x in [name,region if region and region!=name else '',country] if x)
        if value.lower() in seen:continue
        seen.add(value.lower());c=f.get('geometry',{}).get('coordinates') or [None,None];out.append({'label':label,'value':value,'lat':c[1],'lng':c[0]})
        if len(out)>=6:break
    res={'items':out};_places_cache[q.lower()]=res;return res
@app.get('/api/search')
def api_search(location:str,checkin:str='',checkout:str='',adults:int=2,offset:int=0,pages:int=3):
    """In-app listing picker: public Airbnb search results (no login)."""
    if not location.strip():raise HTTPException(400,'Enter a location')
    try:return listing_search.search(location,checkin or None,checkout or None,adults,offset,min(max(pages,1),5))
    except Exception as e:raise HTTPException(502,f'Search failed: {type(e).__name__}: {str(e)[:120]}')
@app.get('/api/search/more')
def api_search_more(location:str,page:int,checkin:str='',checkout:str='',adults:int=2):
    try:return listing_search.search_page(location,checkin or None,checkout or None,adults,page)
    except Exception as e:raise HTTPException(502,f'Load more failed: {type(e).__name__}: {str(e)[:120]}')
def _redirect_uri(request):
    base=(os.getenv('PUBLIC_BASE_URL') or str(request.base_url)).rstrip('/')
    if 'localhost' in str(request.base_url) or '127.0.0.1' in str(request.base_url):base=str(request.base_url).rstrip('/')
    return base+'/oauth/google/callback'
@app.get('/oauth/google/start')
def google_start(request:Request):
    _require_admin(request)
    if not gdrive.configured():raise HTTPException(400,'Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Settings first')
    return RedirectResponse(gdrive.auth_url(_redirect_uri(request)),status_code=302)
@app.get('/oauth/google/callback')
def google_callback(request:Request,code:str='',state:str='',error:str=''):
    if error or not code:return RedirectResponse('/settings?flash='+(error or 'Google sign-in cancelled'),status_code=303)
    try:gdrive.exchange(code,state,_redirect_uri(request))
    except Exception as e:
        from urllib.parse import quote as _q;return RedirectResponse('/settings?flash='+_q('Google Drive connect failed: '+str(e)[:300]),status_code=303)
    return RedirectResponse('/settings?saved=1',status_code=303)
@app.get('/api/gdrive/status')
def gdrive_status(request:Request):_require_admin(request);return gdrive.status()
@app.post('/api/gdrive/disconnect')
def gdrive_disconnect(request:Request):_require_admin(request);gdrive.disconnect();return gdrive.status()
@app.post('/api/jobs/{jid}/upload-drive')
def upload_drive(request:Request,jid:str):
    j=_jobs.get(jid) or get_job(jid)
    if not owns(j,request.state.user,is_admin(request)):raise HTTPException(404)
    if j.get('status')!='done':raise HTTPException(400,'Reel not ready')
    if not gdrive.status().get('connected'):raise HTTPException(400,'Google Drive not connected')
    vid=JOBS/jid/Path(j['video_url']).name
    try:info=gdrive.upload(vid,(j.get('listing') or {}).get('url') or j['url'],description=(j.get('listing') or {}).get('title',''))
    except Exception as e:raise HTTPException(502,f'Upload failed: {type(e).__name__}: {str(e)[:160]}')
    upd=dict(drive_status='uploaded',drive_link=info.get('webViewLink'),drive_name=info.get('name'),drive_id=info.get('id'))
    if info.get('id') and not KEEP_LOCAL:purge_local(jid);upd.update(local_deleted=True,video_url=None)
    if jid in _jobs:_jobs[jid].update(upd);persist(_jobs[jid]);return enrich(job_public(_jobs[jid]))
    j.update(upd);(JOBS/jid/'job.json').write_text(json.dumps(j,indent=1));return enrich(j)
def listing_id_of(j):
    return (j.get('listing') or {}).get('id') or (re.search(r'/rooms/(\d+)',j.get('url','')) or [None,None])[1]
def poster_for(j):
    """First-frame poster (2 s in) generated once per finished reel; served from /media."""
    if j.get('status')!='done':return None
    if not j.get('video_url'):return drive_fields(j).get('drive_thumb')
    d=JOBS/j['id'];vid=d/Path(j['video_url']).name;pos=d/'poster.jpg'
    if not pos.exists() and vid.exists():
        import subprocess;subprocess.run(['ffmpeg','-y','-v','error','-ss','2','-i',str(vid),'-frames:v','1','-vf','scale=640:-2',str(pos)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return f"/media/{j['id']}/poster.jpg" if pos.exists() else None
def library(user=None,admin=False):
    groups={};order=[]
    for j in load_jobs(user,admin):
        lid=listing_id_of(j) or j.get('url')
        if lid not in groups:
            L=dict(j.get('listing') or {});L.setdefault('id',lid);L.setdefault('url',j.get('url'));groups[lid]={'listing':L,'jobs':[],'latest':j,'poster':None};order.append(lid)
        g=groups[lid];g['jobs'].append(j)
        if (j.get('listing') or {}).get('title') and not g['listing'].get('title'):g['listing'].update({k:v for k,v in j['listing'].items() if v})
        if not g['poster']:g['poster']=poster_for(j)
    return [groups[k] for k in order]
@app.get('/reels',response_class=HTMLResponse)
def reels_page(request:Request):
    groups=library(request.state.user,is_admin(request))
    for g in groups:
        for j in g['jobs']:j.update(drive_fields(j))
    return tpl.TemplateResponse(request,'reels.html',{'groups':groups})
@app.get('/api/reels/index')
def reels_index(request:Request):
    """listing id → reels (for the search results 'Reel ready' marker). Scoped to the signed-in user."""
    out={}
    for j in load_jobs(request.state.user,is_admin(request)):
        lid=listing_id_of(j)
        if lid and j.get('status')=='done':out.setdefault(lid,[]).append({'id':j['id'],'created':j.get('created'),'video_url':j.get('video_url'),'drive_link':j.get('drive_link'),**drive_fields(j)})
    return out
@app.get('/outreach',response_class=HTMLResponse)
def outreach_page(request:Request):
    u=request.state.user
    return tpl.TemplateResponse(request,'outreach.html',{'csrf':auth.csrf_token(request.cookies.get(auth.COOKIE,'')),
        'stats':store.outreach_stats(u),'cities':store.cities(u),'rows':store.outreach_rows(u),
        'default_message':os.getenv('COHOST_MESSAGE') or COHOST_MESSAGE,'linkedin_default':linkedin.CONNECT_DEFAULT,
        'daily_cap':DAILY_CAP,'sent_today':store.sent_today(u)})
@app.get('/api/outreach/cohosts')
def api_cohosts(request:Request,city:str=''):
    if not city.strip():raise HTTPException(400,'Enter a city')
    try:return cohost.discover(city.strip())
    except Exception as e:raise HTTPException(502,f'Lookup failed: {type(e).__name__}: {str(e)[:120]}')
@app.get('/api/outreach/linkedin')
def api_linkedin(request:Request,city:str='',role:str='property manager'):
    if not city.strip():raise HTTPException(400,'Enter a city')
    try:return linkedin.build(city.strip(),role.strip() or 'property manager')
    except Exception as e:raise HTTPException(502,f'Lookup failed: {type(e).__name__}: {str(e)[:120]}')
@app.post('/api/outreach/queue')
async def api_queue(request:Request):
    b=await request.json();u=request.state.user
    if not auth.csrf_ok(request.cookies.get(auth.COOKIE,''),b.get('csrf')):raise HTTPException(403,'Form expired — reload')
    ch=b.get('channel') or 'cohost';out=[]
    for it in (b.get('items') or [])[:25]:
        rid=store.add_outreach(u,ch,it.get('name') or '',it.get('url') or '',it.get('city') or '',it.get('message') or '',meta=it)
        out.append(rid)
    return {'ok':True,'ids':out,'rows':store.outreach_rows(u),'stats':store.outreach_stats(u)}
@app.post('/api/outreach/send')
async def api_send(request:Request):
    """Sends only with confirm=true, capped per day, paced, and stops at the first hard failure."""
    b=await request.json();u=request.state.user
    if not auth.csrf_ok(request.cookies.get(auth.COOKIE,''),b.get('csrf')):raise HTTPException(403,'Form expired — reload')
    if not b.get('confirm'):raise HTTPException(400,'Confirmation required')
    if not airbnb_status(max_age=0).get('connected'):raise HTTPException(400,'Connect your Airbnb account in Settings first')
    already=store.sent_today(u)
    room=max(0,DAILY_CAP-already)
    if room<=0:raise HTTPException(429,f'Daily cap of {DAILY_CAP} reached — try again tomorrow')
    ids=[int(i) for i in (b.get('ids') or [])][:room]
    items=[]
    for i in ids:
        r=store.outreach_get(i)
        if r and r['user']==u and r['status']=='queued':items.append({'id':i,'url':r['url'],'message':r['message'],'name':r['name']})
    res=cohost.send_batch(items,confirm=True)
    sent=0;failed=[]
    for r in res:
        st='sent' if r['status']=='sent' else ('queued' if r['status'] in ('draft','manual') else 'skipped')
        store.outreach_set(r['id'],status=st,sent_at=(time.time() if st=='sent' else None),note=r['info'][:300])
        if st=='sent':sent+=1
        else:failed.append({'id':r['id'],'error':r['info']})
    return {'ok':True,'sent':sent,'failed':failed,'rows':store.outreach_rows(u),'stats':store.outreach_stats(u),'sent_today':store.sent_today(u)}
@app.post('/api/outreach/status')
async def api_out_status(request:Request):
    b=await request.json();u=request.state.user
    r=store.outreach_get(int(b.get('id') or 0))
    if not r or r['user']!=u:raise HTTPException(404)
    st=b.get('status') or 'queued'
    if st not in ('queued','sent','replied','won','skipped'):raise HTTPException(400,'Bad status')
    store.outreach_set(r['id'],status=st,**({'sent_at':time.time()} if st=='sent' and not r.get('sent_at') else {}))
    return {'ok':True,'stats':store.outreach_stats(u)}
@app.post('/api/outreach/note')
async def api_out_note(request:Request):
    b=await request.json();u=request.state.user
    r=store.outreach_get(int(b.get('id') or 0))
    if not r or r['user']!=u:raise HTTPException(404)
    store.outreach_set(r['id'],note=(b.get('note') or '')[:500]);return {'ok':True}
@app.get('/api/outreach/export.csv')
def api_out_csv(request:Request):
    from fastapi.responses import PlainTextResponse
    csv=linkedin.csv_rows(store.outreach_rows(request.state.user))
    return PlainTextResponse(csv,media_type='text/csv',headers={'Content-Disposition':'attachment; filename="reelsieve-outreach.csv"'})
def share_token(jid):
    """Per-job secret for public reel links. Separate from the job id so knowing an id proves nothing."""
    f=JOBS/jid/'job.json'
    if not f.exists():return None
    try:j=json.loads(f.read_text())
    except Exception:return None
    t=j.get('share_token')
    if not t:
        t=secrets.token_urlsafe(18);j['share_token']=t;f.write_text(json.dumps(j,indent=1))
        if jid in _jobs:_jobs[jid]['share_token']=t
    return t
@app.get('/media/{jid}/{name}')
def media(request:Request,jid:str,name:str,t:str=''):
    p=(JOBS/jid/name).resolve()
    if not p.is_file() or JOBS not in p.parents or p.suffix not in ('.mp4','.jpg'):raise HTTPException(404)
    tok=share_token(jid)
    if not (t and tok and secrets.compare_digest(t,tok)):
        u=getattr(request.state,'user',None)
        try:j=get_job(jid,u,is_admin(request))
        except HTTPException:raise HTTPException(404)
        if not owns(j,u,is_admin(request)):raise HTTPException(404)
    return FileResponse(p,media_type='image/jpeg' if p.suffix=='.jpg' else 'video/mp4',filename=None if p.suffix=='.jpg' else name)
@app.get('/settings',response_class=HTMLResponse)
def settings(request:Request,saved:int=0,flash:str=''):
    if not is_admin(request):return RedirectResponse('/account',status_code=303)
    return tpl.TemplateResponse(request,'settings.html',{'s':settings_view(),'saved':bool(saved),'flash':flash,'csrf':auth.csrf_token(request.cookies.get(auth.COOKIE,'')),'is_admin':auth.role(request.state.user)=='admin'})
@app.get('/account',response_class=HTMLResponse)
def account_page(request:Request,saved:int=0):
    return tpl.TemplateResponse(request,'account.html',{'account':plans.account_view(request.state.user),'plans':plans.public_plans(),'csrf':auth.csrf_token(request.cookies.get(auth.COOKIE,''))})
@app.post('/settings')
async def settings_post(request:Request):
    if not is_admin(request):raise HTTPException(403,'Admin only')
    form=request.scope.get('_form') or dict(await request.form())
    save_settings(form);return RedirectResponse('/settings?saved=1',status_code=303)
@app.get('/api/settings')
def settings_api(request:Request):
    _require_admin(request)
    return {k:({'configured':v['configured']} if isinstance(v,dict) and 'configured' in v else v) for k,v in settings_view().items()}
@app.post('/api/airbnb/connect')
def airbnb_connect(request:Request):_require_admin(request);hostmsg.connect();return {'ok':True}
@app.get('/api/airbnb/status')
def airbnb_status_api(request:Request):_require_admin(request);return airbnb_status(max_age=0)
@app.post('/api/airbnb/disconnect')
def airbnb_disconnect(request:Request):_require_admin(request);hostmsg.disconnect();_airbnb_cache.update(t=0);return airbnb_status(max_age=0)
@app.post('/api/tunnel/start')
def tunnel_start(request:Request):_require_admin(request);return hostmsg.tunnel_start(PORT)
@app.get('/api/tunnel/status')
def tunnel_status(request:Request):_require_admin(request);return hostmsg.tunnel_status()
@app.post('/api/tunnel/stop')
def tunnel_stop(request:Request):_require_admin(request);return hostmsg.tunnel_stop()
