"""Auth for ReelSieve: multi-user accounts (email + password, salted PBKDF2), HMAC-signed session cookie, CSRF tokens,
login rate limiting. Store: AUTH_PATH (default .listing-reel/auth.json; /data/auth.json on Railway).
Env override for emergencies/reset: APP_USER + APP_PASSWORD (grants that login without touching the store)."""
import os,json,hmac,hashlib,secrets,time,base64,re,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
AUTH_PATH=Path(os.getenv('AUTH_PATH') or (ROOT/'.listing-reel'/'auth.json'))
COOKIE='reelsieve_session';LONG_TTL=int(os.getenv('SESSION_TTL_DAYS','30'))*86400;SHORT_TTL=12*3600
_lock=threading.Lock();_fails={}   # ip -> [timestamps]
EMAIL=re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
def _load():
    try:d=json.loads(AUTH_PATH.read_text())
    except Exception:d={}
    if d.get('hash') and 'users' not in d:   # migrate single-account file
        d['users']={(d.get('user') or 'admin'):{'salt':d['salt'],'hash':d['hash'],'role':'admin','created':d.get('created',time.time())}}
        for k in ('user','salt','hash','created'):d.pop(k,None)
        _save(d)
    d.setdefault('users',{});return d
def _save(d):AUTH_PATH.parent.mkdir(parents=True,exist_ok=True);AUTH_PATH.write_text(json.dumps(d));os.chmod(AUTH_PATH,0o600)
def _hash(pw,salt):return hashlib.pbkdf2_hmac('sha256',pw.encode(),bytes.fromhex(salt),200_000).hex()
def norm(u):return (u or '').strip().lower()
def secret():
    s=os.getenv('SESSION_SECRET')
    if s:return s
    with _lock:
        d=_load()
        if not d.get('secret'):d['secret']=secrets.token_hex(32);_save(d)
        return d['secret']
def has_account():return bool(os.getenv('APP_PASSWORD') or _load()['users'])
def users():
    d=_load();return [{'user':u,'role':v.get('role','member'),'created':v.get('created')} for u,v in sorted(d['users'].items())]
def validate_password(pw):
    if len(pw)<8:raise ValueError('Password must be at least 8 characters')
    if pw.lower() in ('password','12345678','qwertyui'):raise ValueError('Choose a less common password')
def create_user(user,pw,role='member'):
    user=norm(user)
    if not EMAIL.match(user):raise ValueError('Enter a valid email address')
    validate_password(pw)
    with _lock:
        d=_load()
        if user in d['users']:raise ValueError('That email already has an account')
        salt=secrets.token_hex(16);d['users'][user]={'salt':salt,'hash':_hash(pw,salt),'role':role if d['users'] else 'admin','created':time.time()};_save(d)
def delete_user(user,by):
    user=norm(user)
    with _lock:
        d=_load()
        if user not in d['users']:raise ValueError('No such user')
        if user==norm(by):raise ValueError("You can't remove your own account")
        if d['users'][user].get('role')=='admin' and sum(1 for v in d['users'].values() if v.get('role')=='admin')<=1:raise ValueError('Keep at least one admin')
        del d['users'][user];_save(d)
def set_password(user,pw):
    validate_password(pw);user=norm(user)
    with _lock:
        d=_load()
        if user not in d['users']:raise ValueError('No such user')
        salt=secrets.token_hex(16);d['users'][user].update(salt=salt,hash=_hash(pw,salt),changed=time.time());_save(d)
def role(user):
    if os.getenv('APP_PASSWORD') and norm(user)==norm(os.getenv('APP_USER') or 'admin'):return 'admin'
    return (_load()['users'].get(norm(user)) or {}).get('role','member')
def verify(user,pw):
    user=norm(user);env_pw=os.getenv('APP_PASSWORD')
    if env_pw and user==norm(os.getenv('APP_USER') or 'admin'):return hmac.compare_digest(pw,env_pw)
    rec=_load()['users'].get(user)
    if not rec:_hash(pw,'00'*16);return False   # constant-ish time
    return hmac.compare_digest(_hash(pw,rec['salt']),rec['hash'])
# --- rate limiting: 5 failures per 10 minutes per IP ---
def too_many(ip):
    now=time.time();f=[t for t in _fails.get(ip,[]) if now-t<600];_fails[ip]=f;return len(f)>=5
def record_fail(ip):_fails.setdefault(ip,[]).append(time.time())
def clear_fails(ip):_fails.pop(ip,None)
# --- sessions ---
def issue(user,long=True):
    exp=int(time.time())+(LONG_TTL if long else SHORT_TTL);payload=f'{norm(user)}|{exp}|{secrets.token_hex(4)}';sig=hmac.new(secret().encode(),payload.encode(),hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{payload}|{sig}'.encode()).decode(),(LONG_TTL if long else None)
def check(token):
    try:
        user,exp,nonce,sig=base64.urlsafe_b64decode(token.encode()).decode().split('|')
        if int(exp)<time.time():return None
        if not hmac.compare_digest(hmac.new(secret().encode(),f'{user}|{exp}|{nonce}'.encode(),hashlib.sha256).hexdigest(),sig):return None
        if os.getenv('APP_PASSWORD') and user==norm(os.getenv('APP_USER') or 'admin'):return user
        return user if user in _load()['users'] else None
    except Exception:return None
# --- CSRF (double-submit, derived from session) ---
def csrf_token(session_token):return hmac.new(secret().encode(),('csrf|'+(session_token or 'anon')).encode(),hashlib.sha256).hexdigest()[:32]
def csrf_ok(session_token,submitted):return bool(submitted) and hmac.compare_digest(csrf_token(session_token),submitted)
