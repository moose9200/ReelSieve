"""Login gate for BNBsieve: one admin account created on first run (or APP_USER/APP_PASSWORD env), salted PBKDF2 hash,
HMAC-signed session cookie. Store: AUTH_PATH (default .listing-reel/auth.json; /data/auth.json on Railway)."""
import os,json,hmac,hashlib,secrets,time,base64
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
AUTH_PATH=Path(os.getenv('AUTH_PATH') or (ROOT/'.listing-reel'/'auth.json'))
COOKIE='bnbsieve_session';TTL=int(os.getenv('SESSION_TTL_DAYS','30'))*86400
def _load():
    try:return json.loads(AUTH_PATH.read_text())
    except Exception:return {}
def _save(d):AUTH_PATH.parent.mkdir(parents=True,exist_ok=True);AUTH_PATH.write_text(json.dumps(d));os.chmod(AUTH_PATH,0o600)
def _hash(pw,salt):return hashlib.pbkdf2_hmac('sha256',pw.encode(),bytes.fromhex(salt),200_000).hex()
def secret():
    s=os.getenv('SESSION_SECRET')
    if s:return s
    d=_load()
    if not d.get('secret'):d['secret']=secrets.token_hex(32);_save(d)
    return d['secret']
def has_account():return bool(os.getenv('APP_PASSWORD') or _load().get('hash'))
def create_account(user,pw):
    if len(pw)<8:raise ValueError('Password must be at least 8 characters')
    d=_load();salt=secrets.token_hex(16);d.update(user=user.strip() or 'admin',salt=salt,hash=_hash(pw,salt),created=time.time());_save(d)
def verify(user,pw):
    env_pw=os.getenv('APP_PASSWORD')
    if env_pw:return hmac.compare_digest(pw,env_pw) and (user.strip()==(os.getenv('APP_USER') or 'admin'))
    d=_load()
    if not d.get('hash'):return False
    return hmac.compare_digest(_hash(pw,d['salt']),d['hash']) and user.strip()==d.get('user','admin')
def change_password(pw):create_account(_load().get('user') or os.getenv('APP_USER') or 'admin',pw)
def username():return os.getenv('APP_USER') or _load().get('user') or 'admin'
def issue(user):
    exp=int(time.time())+TTL;payload=f'{user}|{exp}';sig=hmac.new(secret().encode(),payload.encode(),hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{payload}|{sig}'.encode()).decode()
def check(token):
    try:
        user,exp,sig=base64.urlsafe_b64decode(token.encode()).decode().split('|')
        if int(exp)<time.time():return None
        ok=hmac.compare_digest(hmac.new(secret().encode(),f'{user}|{exp}'.encode(),hashlib.sha256).hexdigest(),sig);return user if ok else None
    except Exception:return None
