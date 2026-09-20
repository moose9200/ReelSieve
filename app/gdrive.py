"""Google Drive auto-upload for finished reels (OAuth 2.0, scope drive.file — only files this app creates).

Multi-tenant: every tenant connects their OWN Google account and their reels land in their own Drive.
Tokens are stored one file per user, so one tenant's refresh token can never be used to reach another
tenant's Drive, and disconnecting one account leaves the others alone. The `drive.file` scope also means
this app can only ever see the files it created itself — never the rest of anyone's Drive.

Token files live on the persistent volume when there is one (Railway rebuilds the container filesystem on
every deploy, so a token stored beside the code is lost the next time we ship).
"""
import os,json,time,re,secrets,threading,hashlib
from pathlib import Path
from urllib.parse import urlencode
import httpx
ROOT=Path(__file__).resolve().parent.parent
def _token_dir():
    ex=os.getenv('GDRIVE_TOKEN_DIR')
    if ex:return Path(ex)
    legacy=os.getenv('GDRIVE_TOKEN_PATH')
    if legacy:return Path(legacy).parent
    vol=Path(os.getenv('DATA_DIR') or '/data')
    try:
        if vol.is_dir() and os.access(vol,os.W_OK):return vol/'gdrive'
    except Exception:pass
    return ROOT/'.listing-reel'
TOKEN_DIR=_token_dir()
LEGACY_PATH=Path(os.getenv('GDRIVE_TOKEN_PATH') or (ROOT/'.listing-reel'/'google-token.json'))
SCOPES='https://www.googleapis.com/auth/drive.file openid email'
AUTH='https://accounts.google.com/o/oauth2/v2/auth';TOKEN='https://oauth2.googleapis.com/token';API='https://www.googleapis.com/drive/v3';UPLOAD='https://www.googleapis.com/upload/drive/v3/files'
_lock=threading.Lock();_pending_state={}
def configured():return bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))
def _key(user):
    """A stable filename per account. Hashed so the directory listing is not a list of customer emails."""
    return hashlib.sha256((user or '').strip().lower().encode()).hexdigest()[:32]
def _path(user):
    if not user:raise RuntimeError('Google Drive is per account — no user in context')
    return TOKEN_DIR/f'google-{_key(user)}.json'
def _load(user):
    try:return json.loads(_path(user).read_text())
    except Exception:return None
def _save(user,tok):
    p=_path(user);p.parent.mkdir(parents=True,exist_ok=True)
    tok['user']=user;p.write_text(json.dumps(tok));os.chmod(p,0o600)
def adopt_legacy(user):
    """One-off: the single shared token predates per-account Drive. Hand it to the operator who made it."""
    if not user or _path(user).exists() or not LEGACY_PATH.exists():return False
    try:
        t=json.loads(LEGACY_PATH.read_text())
        if not t.get('refresh_token'):return False
        _save(user,t);LEGACY_PATH.unlink()
        return True
    except Exception:return False
def status(user):
    t=_load(user)
    return {'configured':configured(),'connected':bool(t and t.get('refresh_token')),'email':(t or {}).get('email'),
            'folder':os.getenv('GDRIVE_FOLDER') or 'Listing Reels'}
def connected(user):
    t=_load(user);return bool(t and t.get('refresh_token'))
def disconnect(user):
    p=_path(user)
    if p.exists():p.unlink()
def auth_url(redirect_uri,user):
    """The state carries which tenant started this, so the callback cannot write into another account."""
    state=secrets.token_urlsafe(24);_pending_state[state]=(time.time(),user)
    for k,(ts,_u) in list(_pending_state.items()):
        if time.time()-ts>900:_pending_state.pop(k,None)
    return AUTH+'?'+urlencode({'client_id':os.environ['GOOGLE_CLIENT_ID'],'redirect_uri':redirect_uri,'response_type':'code','scope':SCOPES,'access_type':'offline','prompt':'consent','include_granted_scopes':'true','state':state})
def exchange(code,state,redirect_uri,user=None):
    ent=_pending_state.pop(state,None)
    if not ent or time.time()-ent[0]>900:raise ValueError('OAuth state mismatch or expired — try Connect again')
    owner=ent[1]
    if user and owner and user!=owner:raise ValueError('That Google sign-in was started by a different account')
    r=httpx.post(TOKEN,data={'code':code,'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'redirect_uri':redirect_uri,'grant_type':'authorization_code'},timeout=30)
    if r.status_code!=200:
        try:e=r.json()
        except Exception:e={}
        err=e.get('error','');desc=e.get('error_description','')
        hint={'invalid_client':'the Client ID / Client secret pair is wrong (re-copy the secret from Google Cloud Console → Credentials; it starts with GOCSPX-)','redirect_uri_mismatch':f'add {redirect_uri} to the OAuth client\'s authorised redirect URIs','invalid_grant':'the code expired or was reused — click Connect again'}.get(err,'')
        raise RuntimeError(f'Google token exchange failed ({r.status_code} {err}): {desc}. {hint}'.strip())
    tok=r.json()
    if not tok.get('refresh_token'):
        old=_load(owner) or {};tok['refresh_token']=old.get('refresh_token')
    tok['expires_at']=time.time()+int(tok.get('expires_in',3600))-60
    try:
        u=httpx.get('https://www.googleapis.com/oauth2/v3/userinfo',headers={'Authorization':'Bearer '+tok['access_token']},timeout=15).json();tok['email']=u.get('email')
    except Exception:pass
    _save(owner,tok);return owner,tok
def access_token(user):
    with _lock:
        t=_load(user)
        if not t or not t.get('refresh_token'):raise RuntimeError('Google Drive not connected for this account')
        if t.get('access_token') and time.time()<t.get('expires_at',0):return t['access_token']
        r=httpx.post(TOKEN,data={'refresh_token':t['refresh_token'],'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'grant_type':'refresh_token'},timeout=30);r.raise_for_status();n=r.json()
        t['access_token']=n['access_token'];t['expires_at']=time.time()+int(n.get('expires_in',3600))-60;_save(user,t);return t['access_token']
def _h(user):return {'Authorization':'Bearer '+access_token(user)}
def folder_id(user):
    name=os.getenv('GDRIVE_FOLDER') or 'Listing Reels';t=_load(user) or {}
    if t.get('folder_id') and t.get('folder_name')==name:return t['folder_id']
    q=f"name = '{name.replace(chr(39),chr(92)+chr(39))}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r=httpx.get(API+'/files',params={'q':q,'fields':'files(id,name)','spaces':'drive'},headers=_h(user),timeout=30);r.raise_for_status();files=r.json().get('files',[])
    if files:fid=files[0]['id']
    else:
        r=httpx.post(API+'/files',json={'name':name,'mimeType':'application/vnd.google-apps.folder'},params={'fields':'id'},headers=_h(user),timeout=30);r.raise_for_status();fid=r.json()['id']
    t.update(folder_id=fid,folder_name=name);_save(user,t);return fid
def safe_name(listing_url,suffix='.mp4'):
    """File name = the listing URL (Drive allows '/' and ':'), trimmed of tracking params."""
    u=re.sub(r'[?#].*$','',listing_url.strip());u=re.sub(r'[\x00-\x1f]','',u);return (u[:180] or 'reel')+suffix
def upload(path,listing_url,user,description='',chunk=8*1024*1024,public=True):
    """Resumable upload into this tenant's own Drive. Returns {id, name, webViewLink, webContentLink}."""
    path=Path(path);meta={'name':safe_name(listing_url),'parents':[folder_id(user)],'description':description[:900]}
    r=httpx.post(UPLOAD,params={'uploadType':'resumable','fields':'id,name,webViewLink,webContentLink'},headers={**_h(user),'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(path.stat().st_size)},json=meta,timeout=60);r.raise_for_status();session=r.headers['Location']
    size=path.stat().st_size;sent=0;resp=None
    with path.open('rb') as f, httpx.Client(timeout=600) as c:
        while sent<size:
            data=f.read(chunk);end=sent+len(data)-1
            resp=c.put(session,content=data,headers={'Content-Range':f'bytes {sent}-{end}/{size}','Content-Type':'video/mp4'})
            if resp.status_code not in (200,201,308):resp.raise_for_status()
            sent=end+1
    info=resp.json() if resp is not None and resp.status_code in (200,201) else {}
    if public and info.get('id'):
        # The reel is meant to be shown to a host, so the link has to open without a Google sign-in.
        try:httpx.post(f"{API}/files/{info['id']}/permissions",json={'role':'reader','type':'anyone'},headers=_h(user),timeout=30)
        except Exception:pass
    return info
