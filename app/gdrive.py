"""Per-account Google Drive delivery (OAuth 2.0, scope drive.file: only files this app creates).

Every account connects its OWN Google account. Credentials live encrypted in PostgreSQL, bound
inside the ciphertext to the owner and connection generation, so a row copied to another owner
or an old connection is unusable. OAuth state is one-time, expires, and is bound to the app
session that started it. No token ever touches the local filesystem.

Network calls never run inside a database transaction: every write that follows one is a
compare-and-set on the connection generation, so a disconnect or reconnect that happens while
Google is answering wins over the stale refresh or upload.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
import httpx

from app import auth, database, photos

SCOPES = 'https://www.googleapis.com/auth/drive.file openid email'
DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN = 'https://oauth2.googleapis.com/token'
REVOKE = 'https://oauth2.googleapis.com/revoke'
USERINFO = 'https://www.googleapis.com/oauth2/v3/userinfo'
API = 'https://www.googleapis.com/drive/v3'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
FOLDER_MIME = 'application/vnd.google-apps.folder'
STATE_TTL = 600
NOT_CONNECTED = 'Google Drive is not connected for this account — connect it in Account'
RECONNECT = 'Google access expired or was revoked — reconnect Google Drive in Account'
_COLS = ('u.id AS owner_id, d.generation, d.status, d.credentials, d.google_sub, '
         'd.google_email, d.folder_id')


def configured():
    return bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))


def folder_name():
    return os.getenv('GDRIVE_FOLDER') or 'Listing Reels'


def _digest(value):
    return hashlib.sha256((value or '').encode()).hexdigest()


def _fernet():
    primary = os.getenv('TOKEN_ENCRYPTION_KEY', '').strip()
    if not primary:
        raise RuntimeError('TOKEN_ENCRYPTION_KEY is required')
    old = [k.strip() for k in os.getenv('TOKEN_ENCRYPTION_OLD_KEYS', '').split(',') if k.strip()]
    return MultiFernet([Fernet(k) for k in [primary, *old]])


def _encrypt(owner_id, generation, tok):
    payload = {'owner': owner_id, 'generation': generation, 'token': tok}
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def _decrypt(row):
    """Usable credentials for this exact owner and connection generation, else None."""
    if not row or row['status'] != 'connected' or not row['credentials']:
        return None
    try:
        payload = json.loads(_fernet().decrypt(row['credentials'].encode()))
    except (InvalidToken, ValueError):
        return None
    if payload.get('owner') != row['owner_id'] or payload.get('generation') != row['generation']:
        return None
    return payload.get('token')


def _row(c, user):
    if not user:
        raise RuntimeError('Google Drive is per account — no user in context')
    return c.execute(f'SELECT {_COLS} FROM users u LEFT JOIN drive_connections d ON d.owner_id=u.id '
                     'WHERE u.email=%s AND u.active', (auth.norm(user),)).fetchone()


def _active_row(c, user, owner_id=None):
    """owner_id: by durable owner, also while an account is being deactivated or erased (sign-in already ended)."""
    row = (c.execute(f'SELECT {_COLS} FROM users u LEFT JOIN drive_connections d ON d.owner_id=u.id WHERE u.id=%s',
                     (owner_id,)).fetchone() if owner_id else _row(c, user))
    if not row:
        raise RuntimeError(NOT_CONNECTED)
    return row


def _owner_lock(c, owner_id):
    c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('reelsieve-drive-' + owner_id,))


def _client(timeout):
    return httpx.Client(timeout=timeout)


def _call(what, method, url, timeout=30, **kw):
    """One Google request; transport failures never carry URLs or bodies into the message."""
    try:
        with _client(timeout) as h:
            return h.request(method, url, **kw)
    except httpx.HTTPError:
        raise RuntimeError(f'Google did not respond to the {what} — try again shortly') from None


def _require(r, what, ok=(200,)):
    if r.status_code == 401:
        raise RuntimeError('Google rejected the Drive credentials — reconnect Google Drive in Account')
    if r.status_code not in ok:
        raise RuntimeError(f'Google Drive {what} failed (status {r.status_code})')
    return r


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _error_code(r):
    err = _json(r).get('error')
    return err if isinstance(err, str) else ''


def _load(user):
    with database.connect() as c:
        return _decrypt(_row(c, user))


def _save(user, tok):
    """Replace this owner's current credentials in place (same generation)."""
    with database.connect() as c:
        row = _active_row(c, user)
        if row['status'] != 'connected':
            raise RuntimeError(NOT_CONNECTED)
        c.execute('UPDATE drive_connections SET credentials=%s,updated=%s WHERE owner_id=%s AND generation=%s',
                  (_encrypt(row['owner_id'], row['generation'], tok), time.time(), row['owner_id'], row['generation']))


def status(user):
    with database.connect() as c:
        row = _row(c, user)
    tok = _decrypt(row)
    connected_now = bool(tok and tok.get('refresh_token'))
    reconnect = bool(row and row['status'] == 'reconnect_required')
    return {'configured': configured(), 'connected': connected_now, 'reconnect': reconnect,
            'email': row['google_email'] if row and (connected_now or reconnect) else None,
            'folder': folder_name()}


def connected(user):
    return status(user)['connected']


def usable_generation(c, owner_id):
    """Inside the caller's transaction: the generation of a usable connection, else None."""
    row = c.execute('SELECT owner_id,generation,status,credentials FROM drive_connections WHERE owner_id=%s FOR SHARE',
                    (owner_id,)).fetchone()
    tok = _decrypt(row)
    return row['generation'] if tok and tok.get('refresh_token') else None


def auth_url(redirect_uri, user, session_token):
    """Start consent for the signed-in owner; the state only completes in that same session."""
    if not user or auth.check(session_token or '') != auth.norm(user):
        raise ValueError('Sign in again before connecting Google Drive')
    if not configured():
        raise RuntimeError('Google Drive is not set up on this install yet')
    state, now = secrets.token_urlsafe(32), time.time()
    with database.connect() as c:
        owner_id = _active_row(c, user)['owner_id']
        c.execute('DELETE FROM drive_oauth_states WHERE expires_at<%s', (now,))
        c.execute('INSERT INTO drive_oauth_states(state_hash,owner_id,session_hash,redirect_uri,created,expires_at) '
                  'VALUES(%s,%s,%s,%s,%s,%s)',
                  (_digest(state), owner_id, _digest(session_token), redirect_uri, now, now + STATE_TTL))
    return AUTH + '?' + urlencode({
        'client_id': os.environ['GOOGLE_CLIENT_ID'], 'redirect_uri': redirect_uri, 'response_type': 'code',
        'scope': SCOPES, 'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'true',
        'state': state})


def exchange(code, state, redirect_uri, user, session_token):
    """Finish consent. State is consumed only by the exact owner, session and redirect that created it."""
    if not user or auth.check(session_token or '') != auth.norm(user):
        raise ValueError('Sign in again, then connect Google Drive from Account')
    with database.connect() as c:
        owner_id = _active_row(c, user)['owner_id']
        consumed = c.execute('DELETE FROM drive_oauth_states WHERE state_hash=%s AND owner_id=%s AND session_hash=%s '
                             'AND redirect_uri=%s AND expires_at>%s RETURNING owner_id',
                             (_digest(state), owner_id, _digest(session_token), redirect_uri, time.time())).fetchone()
    if not consumed:
        raise ValueError('OAuth state mismatch or expired — try Connect again')
    r = _call('sign-in', 'POST', TOKEN, data={
        'code': code, 'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
        'redirect_uri': redirect_uri, 'grant_type': 'authorization_code'})
    if r.status_code != 200:
        err = _error_code(r)
        hint = {'invalid_client': 'the Client ID / Client secret pair is wrong (re-copy the secret from Google Cloud Console → Credentials)',
                'redirect_uri_mismatch': f"add {redirect_uri} to the OAuth client's authorised redirect URIs",
                'invalid_grant': 'the code expired or was reused — click Connect again'}.get(err, '')
        raise RuntimeError(f'Google token exchange failed ({r.status_code} {err}). {hint}'.strip())
    tok = _json(r)
    if DRIVE_SCOPE not in (tok.get('scope') or '').split() or not tok.get('access_token'):
        raise RuntimeError('Google did not grant Drive access — tick the Google Drive permission and connect again')
    who = _call('identity check', 'GET', USERINFO, timeout=15,
                headers={'Authorization': 'Bearer ' + tok['access_token']})
    info = _json(who) if who.status_code == 200 else {}
    if not info.get('sub'):
        raise RuntimeError('Google did not confirm which account was connected — connect again')
    creds = {'access_token': tok['access_token'], 'refresh_token': tok.get('refresh_token'), 'scope': tok['scope'],
             'expires_at': time.time() + int(tok.get('expires_in', 3600)) - 60}
    now = time.time()
    with database.connect() as c:
        _owner_lock(c, owner_id)
        prev = c.execute('SELECT owner_id,generation,status,credentials,google_sub,folder_id '
                         'FROM drive_connections WHERE owner_id=%s', (owner_id,)).fetchone()
        same = bool(prev and prev['google_sub'] == info['sub'])
        if not creds['refresh_token']:
            # Google omits the refresh token for an existing grant; only the SAME Google identity may keep it.
            creds['refresh_token'] = ((_decrypt(prev) or {}).get('refresh_token') if same else None)
        if not creds['refresh_token']:
            raise RuntimeError('Google did not return offline access — remove ReelSieve at '
                               'https://myaccount.google.com/permissions and connect again')
        generation = prev['generation'] + 1 if prev else 1
        c.execute('INSERT INTO drive_connections(owner_id,generation,status,credentials,google_sub,google_email,scope,'
                  'folder_id,connected_at,updated) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                  'ON CONFLICT (owner_id) DO UPDATE SET generation=EXCLUDED.generation,status=EXCLUDED.status,'
                  'credentials=EXCLUDED.credentials,google_sub=EXCLUDED.google_sub,google_email=EXCLUDED.google_email,'
                  'scope=EXCLUDED.scope,folder_id=EXCLUDED.folder_id,connected_at=EXCLUDED.connected_at,updated=EXCLUDED.updated',
                  (owner_id, generation, 'connected', _encrypt(owner_id, generation, creds), info['sub'],
                   info.get('email'), tok['scope'], prev['folder_id'] if same else None, now, now))
    return status(user)


def _access(user, owner_id=None):
    """(access token, connection row). Refreshes outside any transaction; the write-back is fenced."""
    with database.connect() as c:
        row = _active_row(c, user, owner_id)
    tok = _decrypt(row)
    if not tok or not tok.get('refresh_token'):
        raise RuntimeError(RECONNECT if row['status'] == 'reconnect_required' else NOT_CONNECTED)
    if tok.get('access_token') and time.time() < tok.get('expires_at', 0):
        return tok['access_token'], row
    r = _call('token refresh', 'POST', TOKEN, data={
        'refresh_token': tok['refresh_token'], 'client_id': os.environ['GOOGLE_CLIENT_ID'],
        'client_secret': os.environ['GOOGLE_CLIENT_SECRET'], 'grant_type': 'refresh_token'})
    fence = (row['owner_id'], row['generation'])
    if r.status_code != 200:
        if _error_code(r) == 'invalid_grant':
            with database.connect() as c:
                c.execute("UPDATE drive_connections SET status='reconnect_required',credentials=NULL,"
                          'generation=generation+1,updated=%s WHERE owner_id=%s AND generation=%s AND status=%s',
                          (time.time(), *fence, 'connected'))
            raise RuntimeError(RECONNECT)
        raise RuntimeError(f'Google token refresh failed (status {r.status_code}) — try again shortly')
    fresh = _json(r)
    if not fresh.get('access_token'):
        raise RuntimeError('Google token refresh returned no access token — try again shortly')
    tok.update(access_token=fresh['access_token'], expires_at=time.time() + int(fresh.get('expires_in', 3600)) - 60)
    if fresh.get('refresh_token'):
        tok['refresh_token'] = fresh['refresh_token']
    with database.connect() as c:
        kept = c.execute("UPDATE drive_connections SET credentials=%s,updated=%s WHERE owner_id=%s AND generation=%s "
                         "AND status='connected' RETURNING owner_id",
                         (_encrypt(*fence, tok), time.time(), *fence)).fetchone()
    if not kept:
        raise RuntimeError('Google Drive was disconnected or replaced — reconnect Google Drive in Account')
    return tok['access_token'], row


def _pinned(user, generation, owner_id=None):
    """_access, refused when the connection is no longer the one a job was admitted on (None: any)."""
    token, conn = _access(user, owner_id)
    if generation is not None and conn['generation'] != generation:
        raise RuntimeError('Google Drive was reconnected or disconnected after this reel started — start it again')
    return token, conn


def access_token(user):
    return _access(user)[0]


def disconnect(user):
    """Disable local access first, then revoke with Google. A failed revocation is reported, never hidden."""
    with database.connect() as c:
        row = _row(c, user)
    if row and row['generation'] is not None:
        disconnect_owner(row['owner_id'])


def drop_grant(c, owner_id):
    """Inside the caller's transaction: clear this owner's stored Drive grant. The row stays with a new generation, so
    an upload still holding the old one is refused (a deleted row would restart at generation 1 on reconnect and let it
    through). Returns the token to revoke at Google once that transaction has committed, or None."""
    _owner_lock(c, owner_id)
    row = c.execute('SELECT owner_id,generation,status,credentials FROM drive_connections WHERE owner_id=%s',
                    (owner_id,)).fetchone()
    if not row:
        return None
    tok = _decrypt(row) or {}
    c.execute("UPDATE drive_connections SET generation=generation+1,status='disconnected',credentials=NULL,"
              'google_sub=NULL,google_email=NULL,scope=NULL,folder_id=NULL,updated=%s WHERE owner_id=%s',
              (time.time(), owner_id))
    c.execute('DELETE FROM drive_oauth_states WHERE owner_id=%s', (owner_id,))
    return tok.get('refresh_token') or tok.get('access_token')


def revoke(grant):
    """True when Google confirmed it revoked the grant."""
    try:
        with _client(15) as h:
            return h.post(REVOKE, data={'token': grant}).status_code == 200
    except httpx.HTTPError:
        return False


def disconnect_owner(owner_id):
    """Disconnect by durable owner ID, also for an account that has just been deactivated."""
    with database.connect() as c:
        grant = drop_grant(c, owner_id)
    if grant and not revoke(grant):
        raise RuntimeError('Disconnected here, but Google did not confirm revocation — remove ReelSieve at '
                           'https://myaccount.google.com/permissions')


def _folder(owner_id, token, generation):
    """This owner's app folder, found by private appProperties, never by display name."""
    with database.connect() as c:
        row = c.execute("SELECT folder_id FROM drive_connections WHERE owner_id=%s AND generation=%s AND status='connected'",
                        (owner_id, generation)).fetchone()
    if not row:
        raise RuntimeError('Google Drive was disconnected or replaced — reconnect Google Drive in Account')
    if row['folder_id']:
        return row['folder_id']
    headers = {'Authorization': 'Bearer ' + token}
    q = (f"appProperties has {{ key='owner' and value='{owner_id}' }} and mimeType = '{FOLDER_MIME}' "
         'and trashed = false')
    params, fid = {'q': q, 'fields': 'nextPageToken,files(id,appProperties)', 'spaces': 'drive', 'pageSize': 100}, None
    while not fid:
        page = _json(_require(_call('folder lookup', 'GET', API + '/files', headers=headers, params=params), 'folder lookup'))
        # The Inputs folders carry the owner label too (with kind=inputs): the app folder is the one without a kind.
        fid = next((f['id'] for f in page.get('files') or [] if 'kind' not in (f.get('appProperties') or {})), None)
        if not page.get('nextPageToken'):
            break
        params['pageToken'] = page['nextPageToken']
    # ponytail: two first-ever uploads racing can each create a folder; add a creation lock if seen in practice.
    fid = fid or _json(_require(_call(
        'folder creation', 'POST', API + '/files', headers=headers, params={'fields': 'id'},
        json={'name': folder_name(), 'mimeType': FOLDER_MIME, 'appProperties': {'owner': owner_id}}),
        'folder creation')).get('id')
    if not fid:
        raise RuntimeError('Google Drive folder creation failed (no folder id)')
    with database.connect() as c:
        c.execute('UPDATE drive_connections SET folder_id=%s WHERE owner_id=%s AND generation=%s',
                  (fid, owner_id, generation))
    return fid


def safe_name(listing_url, suffix='.mp4'):
    """File name = the listing URL (Drive allows '/' and ':'), trimmed of tracking params; an own-photo reel's title as typed."""
    u = listing_url.strip()
    if re.match(r'https?://', u, re.I):
        u = re.sub(r'[?#].*$', '', u)
    u = re.sub(r'[\x00-\x1f]', '', u)
    return (u[:180] or 'reel') + suffix


def _receipt(row):
    return {'id': row['file_id'], 'name': row['name'], 'webViewLink': row['web_view_link'], 'size': row['size'],
            'sharing': row['sharing'], 'job_id': row['job_id'], 'variant': row['variant'], 'confirmed': True}


def receipt(user, job_id, variant='primary'):
    """This owner's confirmed delivery for a job, or None. Never resolves another owner's file."""
    with database.connect() as c:
        row = _row(c, user)
        if not row:
            return None
        up = c.execute("SELECT * FROM drive_uploads WHERE owner_id=%s AND job_id=%s AND variant=%s AND status='confirmed'",
                       (row['owner_id'], job_id, variant)).fetchone()
    return _receipt(up) if up else None


def receipts_for(user, job_ids):
    """{job_id: {variant: receipt}} of this owner's confirmed deliveries, in one query."""
    if not job_ids:
        return {}
    with database.connect() as c:
        row = _row(c, user)
        if not row:
            return {}
        rows = c.execute("SELECT * FROM drive_uploads WHERE owner_id=%s AND job_id=ANY(%s) AND status='confirmed'",
                         (row['owner_id'], list(job_ids))).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['job_id'], {})[r['variant']] = _receipt(r)
    return out


def _matches(info, file_id, size, props):
    got = info.get('appProperties') or {}
    return (info.get('id') == file_id and str(info.get('size')) == str(size)
            and all(got.get(k) == v for k, v in props.items()))


def _confirm(owner_id, job_id, variant, file_id, generation, info):
    with database.connect() as c:
        row = c.execute("UPDATE drive_uploads SET status='confirmed',name=%s,web_view_link=%s,size=%s,confirmed_at=%s "
                        'WHERE owner_id=%s AND job_id=%s AND variant=%s AND file_id=%s AND EXISTS ('
                        "SELECT 1 FROM drive_connections WHERE owner_id=%s AND generation=%s AND status='connected') "
                        'RETURNING *',
                        (info.get('name'), info.get('webViewLink'), int(info['size']), time.time(),
                         owner_id, job_id, variant, file_id, owner_id, generation)).fetchone()
    if not row:
        raise RuntimeError('Google Drive was disconnected during the upload — reconnect and upload again')
    return _receipt(row)


def _resumable(headers, meta, f, size, mime, chunk, keep_going=None, fields='id,size'):
    """One resumable upload session into Drive (the documented way for files over 5 MB); returns the file
    resource Google sends back when the last byte lands. `f` is any seekable binary file."""
    start = _require(_call('upload start', 'POST', UPLOAD, timeout=60, json=meta,
                           params={'uploadType': 'resumable', 'fields': fields},
                           headers={**headers, 'X-Upload-Content-Type': mime,
                                    'X-Upload-Content-Length': str(size)}), 'upload start')
    session = start.headers.get('Location')
    if not session:
        raise RuntimeError('Google Drive upload start failed (no upload session)')
    sent, info, stalls = 0, None, 0
    try:
        with _client(600) as h:
            while info is None:
                if keep_going and not keep_going():
                    raise RuntimeError('Upload stopped before it finished — nothing was delivered')
                f.seek(sent)
                data = f.read(chunk)
                end = sent + len(data) - 1
                r = h.put(session, content=data, headers={'Content-Range': f'bytes {sent}-{end}/{size}',
                                                          'Content-Type': mime})
                if r.status_code in (200, 201):
                    info = _json(r)
                elif r.status_code == 308:
                    # Resume from what Google says it holds (no Range = nothing yet). It can never
                    # hold more than was sent, and a transfer that keeps making no progress stops.
                    got = r.headers.get('Range', '')
                    got = int(got.rsplit('-', 1)[1]) + 1 if got.startswith('bytes=0-') else 0
                    if got > end + 1:
                        raise RuntimeError('Google Drive reported inconsistent upload progress — upload again')
                    stalls = stalls + 1 if got <= sent else 0
                    if stalls > 3:
                        raise RuntimeError('Google Drive stopped accepting the upload — upload again')
                    sent = got
                else:
                    _require(r, 'upload')
    except httpx.HTTPError:
        raise RuntimeError('The upload to Google Drive was interrupted — it will be reconciled on retry') from None
    return info


def upload(path, listing_url, user, description='', chunk=8 * 1024 * 1024, public=False, job_id=None, variant='primary',
           generation=None, keep_going=None):
    """Resumable upload into this owner's own Drive; private unless the owner asked otherwise.

    Idempotent per (owner, job, variant): the Drive file ID is generated and recorded before any
    bytes move, so a retry after an interrupted upload finds the finished file instead of
    creating a duplicate. Returns only a receipt Google confirmed (ID, size and properties).
    `generation` pins the upload to the connection a job was admitted with: after a reconnect
    or disconnect it fails instead of delivering into a different Google account. `keep_going`
    is asked before every chunk and before confirming, so a worker that must stop does so promptly.
    """
    if not job_id or len(str(job_id)) > 200 or not re.fullmatch(r'[a-z0-9_-]{1,32}', variant or ''):
        raise ValueError('upload needs a job id and a simple variant name')
    path = Path(path)
    size = path.stat().st_size
    if not size:
        raise ValueError('Refusing to upload an empty file')
    done = receipt(user, job_id, variant)
    if done:
        return set_sharing(user, job_id, True, variant) if public and done['sharing'] != 'public' else done
    token, conn = _pinned(user, generation)
    owner_id, generation = conn['owner_id'], conn['generation']
    headers = {'Authorization': 'Bearer ' + token}
    props = {'owner': owner_id, 'job': str(job_id), 'variant': variant}
    with database.connect() as c:
        intent = c.execute('SELECT file_id,generation FROM drive_uploads WHERE owner_id=%s AND job_id=%s AND variant=%s',
                           (owner_id, job_id, variant)).fetchone()
    file_id = intent['file_id'] if intent and intent['generation'] == generation else None
    if file_id:
        r = _call('upload check', 'GET', f'{API}/files/{file_id}', headers=headers,
                  params={'fields': 'id,name,size,webViewLink,appProperties'})
        if r.status_code == 200:
            if not _matches(_json(r), file_id, size, props):
                raise RuntimeError('A different file already exists for this delivery in Google Drive')
            result = _confirm(owner_id, job_id, variant, file_id, generation, _json(r))
            return set_sharing(user, job_id, True, variant) if public else result
        _require(r, 'upload check', ok=(404,))
    else:
        ids = _json(_require(_call('upload preparation', 'GET', API + '/files/generateIds', headers=headers,
                                   params={'count': 1, 'space': 'drive'}), 'upload preparation')).get('ids') or []
        if not ids:
            raise RuntimeError('Google Drive upload preparation failed (no file id)')
        file_id = ids[0]
        with database.connect() as c:
            c.execute('INSERT INTO drive_uploads(owner_id,job_id,variant,file_id,generation,created) VALUES(%s,%s,%s,%s,%s,%s) '
                      "ON CONFLICT (owner_id,job_id,variant) DO UPDATE SET file_id=EXCLUDED.file_id,generation=EXCLUDED.generation "
                      "WHERE drive_uploads.status='pending'",
                      (owner_id, job_id, variant, file_id, generation, time.time()))
    meta = {'id': file_id, 'name': safe_name(listing_url, '.mp4' if variant == 'primary' else f'-{variant}.mp4'), 'parents': [_folder(owner_id, token, generation)],
            'description': (description or '')[:900], 'appProperties': props}
    with path.open('rb') as f:
        info = _resumable(headers, meta, f, size, 'video/mp4', chunk, keep_going,
                          fields='id,name,size,webViewLink,appProperties')
    if not _matches(info, file_id, size, props):
        raise RuntimeError('Google Drive did not confirm the upload — upload again')
    if keep_going and not keep_going():
        raise RuntimeError('Upload stopped before it finished — nothing was delivered')
    result = _confirm(owner_id, job_id, variant, file_id, generation, info)
    return set_sharing(user, job_id, True, variant) if public else result


# ---------------- a reel's own photos: in the owner's Drive only, referenced by file id ----------------

INPUT_MAX = 16 * 1024 * 1024  # a cleaned photo is a JPEG at most 2560 px long; anything bigger is not ours
DRIVE_ID = re.compile(r'[A-Za-z0-9_-]{1,200}')


def _subfolder(headers, parent, name, props, find=False):
    """A folder inside `parent`, found by private appProperties (never by display name) or created."""
    if find:
        q = ' and '.join([f"appProperties has {{ key='{k}' and value='{v}' }}" for k, v in props.items()]
                         + [f"'{parent}' in parents", f"mimeType = '{FOLDER_MIME}'", 'trashed = false'])
        found = _json(_require(_call('folder lookup', 'GET', API + '/files', headers=headers,
                                     params={'q': q, 'fields': 'files(id)', 'spaces': 'drive', 'pageSize': 1}),
                               'folder lookup')).get('files') or []
        if found:
            return found[0]['id']
    fid = _json(_require(_call('folder creation', 'POST', API + '/files', headers=headers, params={'fields': 'id'},
                               json={'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent], 'appProperties': props}),
                         'folder creation')).get('id')
    if not fid:
        raise RuntimeError('Google Drive folder creation failed (no folder id)')
    return fid


def _check_id(fid):
    if not DRIVE_ID.fullmatch(str(fid or '')):
        raise RuntimeError('This reel refers to a Google Drive file it cannot use — start it again')
    return fid


def upload_inputs(user, job_id, images, generation, chunk=8 * 1024 * 1024):
    """Save a reel's cleaned photos (JPEG bytes) in this owner's Drive under <app folder>/Inputs/<job id>/.
    Returns (folder id, [file ids]); a failure part-way deletes the folder so nothing half-made is left."""
    token, conn = _pinned(user, generation)
    owner_id, headers = conn['owner_id'], {'Authorization': 'Bearer ' + token}
    inputs = _subfolder(headers, _folder(owner_id, token, generation), 'Inputs', {'owner': owner_id, 'kind': 'inputs'}, find=True)
    folder = _subfolder(headers, inputs, job_id, {'owner': owner_id, 'kind': 'inputs', 'job': job_id})

    def one(i):
        meta = {'name': f'photo-{i + 1:02d}.jpg', 'parents': [folder], 'mimeType': 'image/jpeg',
                'appProperties': {'owner': owner_id, 'job': job_id, 'input': str(i + 1)}}
        info = _resumable(headers, meta, io.BytesIO(images[i]), len(images[i]), 'image/jpeg', chunk)
        if not info or not info.get('id') or str(info.get('size')) != str(len(images[i])):
            raise RuntimeError('Google Drive did not confirm a photo upload — try again')
        return info['id']
    try:
        with ThreadPoolExecutor(4) as pool:  # ponytail: 4 parallel sessions; raise if 40-photo uploads feel slow
            return folder, list(pool.map(one, range(len(images))))
    except Exception:
        try:
            _call('photo clean-up', 'DELETE', f'{API}/files/{folder}', headers=headers)
        except RuntimeError:
            pass  # the original error matters more; the folder holds only this reel's photos
        raise


CHANGED = 'A photo in Google Drive changed after upload — start the reel again'


def download_inputs(user, file_ids, dest, generation, keep_going=None):
    """Fetch a reel's photos from the owner's Drive into disposable scratch as p01.jpg, p02.jpg, …

    The customer owns these files and can replace one under the same id ('Upload new version'), so every download
    goes through the same check and re-encode as the upload (photos.clean) before any decoder on this machine sees it."""
    token, _ = _pinned(user, generation)
    headers, dest, paths = {'Authorization': 'Bearer ' + token}, Path(dest), []
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with _client(120) as h:
            for i, fid in enumerate(file_ids):
                if keep_going and not keep_going():
                    raise RuntimeError('Stopped before the photos were fetched')
                with h.stream('GET', f'{API}/files/{_check_id(fid)}', params={'alt': 'media'}, headers=headers) as r:
                    if r.status_code == 404:
                        raise RuntimeError('A photo was removed from your Google Drive before the reel was made — start it again')
                    _require(r, 'photo download')
                    data = bytearray()
                    for part in r.iter_bytes():
                        data += part
                        if len(data) > INPUT_MAX:
                            raise RuntimeError(CHANGED)
                try:
                    jpeg = photos.clean(bytes(data))
                except photos.PhotoError:
                    raise RuntimeError(CHANGED) from None
                out = dest / f'p{i + 1:02d}.jpg'
                out.write_bytes(jpeg)
                paths.append(out)
    except httpx.HTTPError:
        raise RuntimeError('Google Drive did not respond while fetching your photos — try again shortly') from None
    return paths


def delete_inputs(user, folder_id, generation, file_ids=(), owner_id=None):
    """Permanently delete the photos we uploaded for a reel (by their stored ids) from the owner's Drive, then move
    their folder to the Drive bin rather than deleting it: deleting a folder also deletes everything in it, and the
    customer may have put files of their own there. Gone already is fine. owner_id: see _active_row."""
    token, _ = _pinned(user, generation, owner_id)
    headers = {'Authorization': 'Bearer ' + token}
    for fid in file_ids:
        _require(_call('photo clean-up', 'DELETE', f'{API}/files/{_check_id(fid)}', headers=headers),
                 'photo clean-up', ok=(200, 204, 404))
    _require(_call('photo clean-up', 'PATCH', f'{API}/files/{_check_id(folder_id)}', headers=headers,
                   params={'fields': 'id'}, json={'trashed': True}), 'photo clean-up', ok=(200, 404))


def set_sharing(user, job_id, public, variant='primary'):
    """Owner-controlled anyone-with-link sharing for one delivered file. Recorded only after Google confirms."""
    done = receipt(user, job_id, variant)
    if not done:
        raise ValueError('No delivered file for this job')
    token, conn = _access(user)
    headers = {'Authorization': 'Bearer ' + token}
    base = f"{API}/files/{done['id']}/permissions"
    if public:
        r = _require(_call('sharing change', 'POST', base, headers=headers, params={'fields': 'id,type,role'},
                           json={'role': 'reader', 'type': 'anyone'}), 'sharing change')
        perm = _json(r)
        if perm.get('type') != 'anyone' or perm.get('role') != 'reader' or not perm.get('id'):
            raise RuntimeError('Google Drive did not confirm the sharing change')
        perm_id = perm['id']
    else:
        listed = _json(_require(_call('sharing lookup', 'GET', base, headers=headers,
                                      params={'fields': 'permissions(id,type)'}), 'sharing lookup'))
        for p in listed.get('permissions') or []:
            if p.get('type') == 'anyone':
                _require(_call('sharing change', 'DELETE', f"{base}/{p['id']}", headers=headers),
                         'sharing change', ok=(200, 204, 404))
        perm_id = None
    with database.connect() as c:
        row = c.execute('UPDATE drive_uploads SET sharing=%s,permission_id=%s WHERE owner_id=%s AND job_id=%s '
                        "AND variant=%s AND status='confirmed' RETURNING *",
                        ('public' if public else 'private', perm_id, conn['owner_id'], job_id, variant)).fetchone()
    return _receipt(row)


@contextmanager
def open_stream(user, job_id, variant='primary', range_header=None):
    """Stream this owner's delivered file from Drive. The file ID always comes from the owner's receipt."""
    done = receipt(user, job_id, variant)
    if not done:
        raise ValueError('No delivered file for this job')
    headers = {'Authorization': 'Bearer ' + access_token(user)}
    if range_header:
        headers['Range'] = range_header
    try:
        with _client(600) as h, h.stream('GET', f"{API}/files/{done['id']}", params={'alt': 'media'},
                                         headers=headers) as r:
            _require(r, 'download', ok=(200, 206))
            yield r
    except httpx.HTTPError:
        raise RuntimeError('Google Drive did not respond to the download — try again shortly') from None
