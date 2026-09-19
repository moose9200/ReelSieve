#!/usr/bin/env python3
"""Listing Reel by Braivex — paste an Airbnb URL, get a 30 s cinematic reel, hand it to the host via Airbnb messaging.
Run: .venv/bin/uvicorn app.server:app --port 8787   (from the project root)"""
import os,re,json,uuid,threading,time,traceback
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv,dotenv_values
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;ENV=ROOT/'.env.local';JOBS=Path(os.getenv('JOBS_DIR') or (ROOT/'jobs'));JOBS.mkdir(parents=True,exist_ok=True);PORT=int(os.getenv('PORT','8787'))
load_dotenv(ENV)
if not os.getenv('PUBLIC_BASE_URL') and os.getenv('RAILWAY_PUBLIC_DOMAIN'):os.environ['PUBLIC_BASE_URL']='https://'+os.environ['RAILWAY_PUBLIC_DOMAIN']
from app import pipeline,hostmsg,search as listing_search,gdrive
app=FastAPI(title='Listing Reel by Braivex');app.mount('/static',StaticFiles(directory=HERE/'static'),name='static')
tpl=Jinja2Templates(directory=HERE/'templates');tpl.env.autoescape=True
SECRET_KEYS=['HF_KEY','GOOGLE_CLIENT_SECRET'];SETTING_KEYS=['HF_KEY','PUBLIC_BASE_URL','DEFAULT_MESSAGE','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','GDRIVE_FOLDER']
HINTS={'HF_KEY':'Higgsfield API key, key-id:key-secret','PUBLIC_BASE_URL':'Where this app is reachable from the internet (optional; tunnel is used otherwise)','DEFAULT_MESSAGE':'Template for the Airbnb message; {reel_link} is replaced','GOOGLE_CLIENT_ID':'OAuth client ID from Google Cloud Console (Web application)','GOOGLE_CLIENT_SECRET':'OAuth client secret','GDRIVE_FOLDER':'Drive folder name for uploads (default: Listing Reels)'}
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
def job_public(j):return {k:v for k,v in j.items() if k!='thread'}
def persist(j):(JOBS/j['id']/'job.json').write_text(json.dumps(job_public(j),indent=1))
def load_jobs():
    out=[]
    for d in sorted(JOBS.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
        f=d/'job.json'
        if f.exists():
            try:out.append(json.loads(f.read_text()))
            except Exception:pass
    return out
def reel_link_for(j):
    base=hostmsg.public_base()
    if base and j.get('video_url'):return f"{base}{j['video_url']}"
    return j.get('drive_link') or None
def finalize_message(j):
    link=reel_link_for(j);msg=(j.get('message') or default_message())
    return msg.replace('{reel_link}',link) if link else msg.replace('{reel_link}','(reel link — start the tunnel in Settings)')
def enrich(j):
    """Derived, non-persisted fields for the UI."""
    j=dict(j);lid=(j.get('listing') or {}).get('id') or (re.search(r'/rooms/(\d+)',j.get('url','')) or [None,None])[1]
    j['contact_url']=hostmsg.contact_url(lid) if lid else None;j['reel_link']=reel_link_for(j);j['message_final']=finalize_message(j);return j
def run_job(jid,url,ai_motion,renderer='v2'):
    j=_jobs[jid];steps=['Fetching','Reviews','Downloaded','Seedance','Estimating depth','Rendering','Rendered','Uploading']
    def cb(msg):
        with _lock:
            j['log'].append(time.strftime('%H:%M:%S ')+msg);j['step']=msg
            for i,s in enumerate(steps):
                if msg.startswith(s):j['progress']=max(j['progress'],int(8+i*13))
            persist(j)
    try:
        j['status']='running';persist(j)
        res=pipeline.run(url,JOBS/jid,None,ai_motion,cb,None,renderer)
        with _lock:
            j.update(status='done',progress=100,step='Done',video_url=f"/media/{jid}/{Path(res['video']).name}",listing={**res['listing'],'location':res['listing'].get('city')},duration=res['duration'])
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
            except Exception as e:
                with _lock:j.update(drive_status='failed',drive_error=f'{type(e).__name__}: {str(e)[:160]}')
                cb(f'Google Drive upload failed: {type(e).__name__}: {str(e)[:120]}')
        else:
            with _lock:j.update(drive_status='skipped',drive_error='Google Drive not connected (Settings)')
    except Exception as e:
        with _lock:j.update(status='failed',error=f'{type(e).__name__}: {str(e)[:300]}',step='Failed');j['log'].append(traceback.format_exc()[-600:])
    persist(j)
@app.get('/',response_class=HTMLResponse)
def index(request:Request):
    jobs=[{'id':j['id'],'status':j['status'],'title':(j.get('listing') or {}).get('title') or j.get('url'),'location':(j.get('listing') or {}).get('location'),'created':j.get('created'),'video_url':j.get('video_url')} for j in load_jobs()[:12]]
    return tpl.TemplateResponse(request,'index.html',{'jobs':jobs,'hf_configured':bool(os.getenv('HF_KEY')),'airbnb_connected':airbnb_status().get('connected',False),'public_url_ok':bool(hostmsg.public_base()),'default_message':default_message()})
@app.post('/api/jobs')
async def create_job(request:Request):
    b=await request.json();url=(b.get('url') or '').strip();ai=bool(b.get('ai_motion'));renderer='v3' if b.get('style')=='tutorial' else 'v2'
    try:pipeline.listing_id(url)
    except ValueError as e:raise HTTPException(400,str(e))
    jid=uuid.uuid4().hex[:10];(JOBS/jid).mkdir(parents=True,exist_ok=True)
    j={'id':jid,'url':url,'ai_motion':ai,'style':renderer,'send_to_host':bool(b.get('send_to_host',True)),'message':(b.get('message') or default_message()).strip(),'status':'queued','progress':2,'step':'Queued','log':[],'video_url':None,'error':None,'listing':{'url':url},'created':time.strftime('%Y-%m-%d %H:%M'),'host_status':None,'host_error':None}
    _jobs[jid]=j;persist(j);threading.Thread(target=run_job,args=(jid,url,ai,renderer),daemon=True).start();return {'id':jid}
def get_job(jid):
    j=_jobs.get(jid)
    if j:
        with _lock:return job_public(j)
    f=JOBS/jid/'job.json'
    if not f.exists():raise HTTPException(404)
    return json.loads(f.read_text())
@app.get('/api/jobs/{jid}')
def job_api(jid:str):return enrich(get_job(jid))
@app.get('/jobs/{jid}',response_class=HTMLResponse)
def job_page(request:Request,jid:str):return tpl.TemplateResponse(request,'job.html',{'job':enrich(get_job(jid))})
@app.post('/api/jobs/{jid}/send-to-host')
async def send_to_host(jid:str,request:Request):
    """Opens Airbnb's contact-host form pre-filled in a headed browser using the connected Airbnb session. Never presses Send."""
    b=await request.json();j=_jobs.get(jid) or get_job(jid)
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
    b=await request.json();j=_jobs.get(jid) or get_job(jid);msg=(b.get('message') or '').strip() or finalize_message(j)
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
def gdrive_status():return gdrive.status()
@app.post('/api/gdrive/disconnect')
def gdrive_disconnect():gdrive.disconnect();return gdrive.status()
@app.post('/api/jobs/{jid}/upload-drive')
def upload_drive(jid:str):
    j=_jobs.get(jid) or get_job(jid)
    if j.get('status')!='done':raise HTTPException(400,'Reel not ready')
    if not gdrive.status().get('connected'):raise HTTPException(400,'Google Drive not connected')
    vid=JOBS/jid/Path(j['video_url']).name
    try:info=gdrive.upload(vid,(j.get('listing') or {}).get('url') or j['url'],description=(j.get('listing') or {}).get('title',''))
    except Exception as e:raise HTTPException(502,f'Upload failed: {type(e).__name__}: {str(e)[:160]}')
    upd=dict(drive_status='uploaded',drive_link=info.get('webViewLink'),drive_name=info.get('name'),drive_id=info.get('id'))
    if jid in _jobs:_jobs[jid].update(upd);persist(_jobs[jid]);return enrich(job_public(_jobs[jid]))
    j.update(upd);(JOBS/jid/'job.json').write_text(json.dumps(j,indent=1));return enrich(j)
@app.get('/media/{jid}/{name}')
def media(jid:str,name:str):
    p=(JOBS/jid/name).resolve()
    if not p.is_file() or JOBS not in p.parents or p.suffix!='.mp4':raise HTTPException(404)
    return FileResponse(p,media_type='video/mp4',filename=name)
@app.get('/settings',response_class=HTMLResponse)
def settings(request:Request,saved:int=0,flash:str=''):return tpl.TemplateResponse(request,'settings.html',{'s':settings_view(),'saved':bool(saved),'flash':flash})
@app.post('/settings')
async def settings_post(request:Request):
    form=await request.form();save_settings(dict(form));return RedirectResponse('/settings?saved=1',status_code=303)
@app.get('/api/settings')
def settings_api():return {k:({'configured':v['configured']} if isinstance(v,dict) and 'configured' in v else v) for k,v in settings_view().items()}
@app.post('/api/airbnb/connect')
def airbnb_connect():hostmsg.connect();return {'ok':True}
@app.get('/api/airbnb/status')
def airbnb_status_api():return airbnb_status(max_age=0)
@app.post('/api/airbnb/disconnect')
def airbnb_disconnect():hostmsg.disconnect();_airbnb_cache.update(t=0);return airbnb_status(max_age=0)
@app.post('/api/tunnel/start')
def tunnel_start():return hostmsg.tunnel_start(PORT)
@app.get('/api/tunnel/status')
def tunnel_status():return hostmsg.tunnel_status()
@app.post('/api/tunnel/stop')
def tunnel_stop():return hostmsg.tunnel_stop()
