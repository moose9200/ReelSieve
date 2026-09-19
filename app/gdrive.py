"""Google Drive auto-upload for finished reels (OAuth 2.0, scope drive.file — only files this app creates).
Every finished reel is uploaded to a "Listing Reels" folder in the user's own Drive, named after the listing URL,
and shared as 'anyone with the link' so the Drive link doubles as the public reel link.
Tokens live in <ROOT>/.listing-reel/google-token.json (0600); on Railway that path is on the /data volume when GDRIVE_TOKEN_PATH is set."""
import os,json,time,re,secrets,threading
from pathlib import Path
from urllib.parse import urlencode
import httpx
ROOT=Path(__file__).resolve().parent.parent
TOKEN_PATH=Path(os.getenv('GDRIVE_TOKEN_PATH') or (ROOT/'.listing-reel'/'google-token.json'))
SCOPES='https://www.googleapis.com/auth/drive.file openid email'
AUTH='https://accounts.google.com/o/oauth2/v2/auth';TOKEN='https://oauth2.googleapis.com/token';API='https://www.googleapis.com/drive/v3';UPLOAD='https://www.googleapis.com/upload/drive/v3/files'
_lock=threading.Lock();_pending_state={}
def configured():return bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))
def _load():
    try:return json.loads(TOKEN_PATH.read_text())
    except Exception:return None
def _save(tok):
    TOKEN_PATH.parent.mkdir(parents=True,exist_ok=True);TOKEN_PATH.write_text(json.dumps(tok));os.chmod(TOKEN_PATH,0o600)
def status():
    t=_load();return {'configured':configured(),'connected':bool(t and t.get('refresh_token')),'email':(t or {}).get('email'),'folder':os.getenv('GDRIVE_FOLDER') or 'Listing Reels'}
def disconnect():
    if TOKEN_PATH.exists():TOKEN_PATH.unlink()
def auth_url(redirect_uri):
    state=secrets.token_urlsafe(24);_pending_state[state]=time.time()
    return AUTH+'?'+urlencode({'client_id':os.environ['GOOGLE_CLIENT_ID'],'redirect_uri':redirect_uri,'response_type':'code','scope':SCOPES,'access_type':'offline','prompt':'consent','include_granted_scopes':'true','state':state})
def exchange(code,state,redirect_uri):
    if state not in _pending_state or time.time()-_pending_state.pop(state)>900:raise ValueError('OAuth state mismatch or expired — try Connect again')
    r=httpx.post(TOKEN,data={'code':code,'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'redirect_uri':redirect_uri,'grant_type':'authorization_code'},timeout=30);r.raise_for_status();tok=r.json()
    if not tok.get('refresh_token'):
        old=_load() or {};tok['refresh_token']=old.get('refresh_token')
    tok['expires_at']=time.time()+int(tok.get('expires_in',3600))-60
    try:
        u=httpx.get('https://www.googleapis.com/oauth2/v3/userinfo',headers={'Authorization':'Bearer '+tok['access_token']},timeout=15).json();tok['email']=u.get('email')
    except Exception:pass
    _save(tok);return tok
def access_token():
    with _lock:
        t=_load()
        if not t or not t.get('refresh_token'):raise RuntimeError('Google Drive not connected')
        if t.get('access_token') and time.time()<t.get('expires_at',0):return t['access_token']
        r=httpx.post(TOKEN,data={'refresh_token':t['refresh_token'],'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'grant_type':'refresh_token'},timeout=30);r.raise_for_status();n=r.json()
        t['access_token']=n['access_token'];t['expires_at']=time.time()+int(n.get('expires_in',3600))-60;_save(t);return t['access_token']
def _h():return {'Authorization':'Bearer '+access_token()}
def folder_id():
    name=os.getenv('GDRIVE_FOLDER') or 'Listing Reels';t=_load() or {}
    if t.get('folder_id') and t.get('folder_name')==name:return t['folder_id']
    q=f"name = '{name.replace(chr(39),chr(92)+chr(39))}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r=httpx.get(API+'/files',params={'q':q,'fields':'files(id,name)','spaces':'drive'},headers=_h(),timeout=30);r.raise_for_status();files=r.json().get('files',[])
    if files:fid=files[0]['id']
    else:
        r=httpx.post(API+'/files',json={'name':name,'mimeType':'application/vnd.google-apps.folder'},params={'fields':'id'},headers=_h(),timeout=30);r.raise_for_status();fid=r.json()['id']
    t.update(folder_id=fid,folder_name=name);_save(t);return fid
def safe_name(listing_url,suffix='.mp4'):
    """File name = the listing URL (Drive allows '/' and ':'), trimmed of tracking params."""
    u=re.sub(r'[?#].*$','',listing_url.strip());u=re.sub(r'[\x00-\x1f]','',u);return (u[:180] or 'reel')+suffix
def upload(path,listing_url,description='',chunk=8*1024*1024):
    """Resumable upload; returns {id, name, webViewLink, webContentLink}. Shares as anyone-with-link (reader)."""
    path=Path(path);meta={'name':safe_name(listing_url),'parents':[folder_id()],'description':description[:900]}
    r=httpx.post(UPLOAD,params={'uploadType':'resumable','fields':'id,name,webViewLink,webContentLink'},headers={**_h(),'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(path.stat().st_size)},json=meta,timeout=60);r.raise_for_status();session=r.headers['Location']
    size=path.stat().st_size;sent=0;resp=None
    with path.open('rb') as f, httpx.Client(timeout=600) as c:
        while sent<size:
            data=f.read(chunk);end=sent+len(data)-1
            resp=c.put(session,content=data,headers={'Content-Range':f'bytes {sent}-{end}/{size}','Content-Type':'video/mp4'})
            if resp.status_code not in (200,201,308):resp.raise_for_status()
            sent=end+1
    info=resp.json() if resp is not None and resp.status_code in (200,201) else {}
    try:httpx.post(f"{API}/files/{info['id']}/permissions",json={'role':'reader','type':'anyone'},headers=_h(),timeout=30)
    except Exception:pass
    return info
