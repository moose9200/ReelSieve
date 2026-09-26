"""Per-account Google Drive delivery (OAuth 2.0, scope drive.file: only files this app creates).

Every account connects its OWN Google account. Credentials live encrypted in PostgreSQL, bound
inside the ciphertext to the owner and connection generation, so a row copied to another owner
or an old connection is unusable. OAuth state is one-time, expires, and is bound to the app
session that started it. No token ever touches the local filesystem.

Network calls never run inside a database transaction: every write that follows one is a
compare-and-set on the connection generation, so a disconnect or reconnect that happens while
Google is answering wins over the stale refresh or upload.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
import httpx

from app import auth, database

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


def _active_row(c, user):
    row = _row(c, user)
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


def _access(user):
    """(access token, connection row). Refreshes outside any transaction; the write-back is fenced."""
    with database.connect() as c:
        row = _active_row(c, user)
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


def access_token(user):
    return _access(user)[0]


def disconnect(user):
    """Disable local access first, then revoke with Google. A failed revocation is reported, never hidden."""
    with database.connect() as c:
        row = _row(c, user)
        if not row or row['generation'] is None:
            return
        _owner_lock(c, row['owner_id'])
        tok = _decrypt(row)
        c.execute("UPDATE drive_connections SET generation=generation+1,status='disconnected',credentials=NULL,"
                  'google_sub=NULL,google_email=NULL,scope=NULL,folder_id=NULL,updated=%s WHERE owner_id=%s',
                  (time.time(), row['owner_id']))
        c.execute('DELETE FROM drive_oauth_states WHERE owner_id=%s', (row['owner_id'],))
    grant = (tok or {}).get('refresh_token') or (tok or {}).get('access_token')
    if not grant:
        return
    try:
        with _client(15) as h:
            r = h.post(REVOKE, data={'token': grant})
        revoked = r.status_code == 200
    except httpx.HTTPError:
        revoked = False
    if not revoked:
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
    found = _json(_require(_call('folder lookup', 'GET', API + '/files', headers=headers,
                                 params={'q': q, 'fields': 'files(id)', 'spaces': 'drive', 'pageSize': 1}),
                           'folder lookup')).get('files') or []
    # ponytail: two first-ever uploads racing can each create a folder; add a creation lock if seen in practice.
    fid = found[0]['id'] if found else _json(_require(_call(
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
    """File name = the listing URL (Drive allows '/' and ':'), trimmed of tracking params."""
    u = re.sub(r'[?#].*$', '', listing_url.strip())
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


def upload(path, listing_url, user, description='', chunk=8 * 1024 * 1024, public=False, job_id=None, variant='primary'):
    """Resumable upload into this owner's own Drive; private unless the owner asked otherwise.

    Idempotent per (owner, job, variant): the Drive file ID is generated and recorded before any
    bytes move, so a retry after an interrupted upload finds the finished file instead of
    creating a duplicate. Returns only a receipt Google confirmed (ID, size and properties).
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
    token, conn = _access(user)
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
    meta = {'id': file_id, 'name': safe_name(listing_url), 'parents': [_folder(owner_id, token, generation)],
            'description': (description or '')[:900], 'appProperties': props}
    start = _require(_call('upload start', 'POST', UPLOAD, timeout=60, json=meta,
                           params={'uploadType': 'resumable', 'fields': 'id,name,size,webViewLink,appProperties'},
                           headers={**headers, 'X-Upload-Content-Type': 'video/mp4',
                                    'X-Upload-Content-Length': str(size)}), 'upload start')
    session = start.headers.get('Location')
    if not session:
        raise RuntimeError('Google Drive upload start failed (no upload session)')
    sent, info = 0, None
    try:
        with path.open('rb') as f, _client(600) as h:
            while info is None:
                f.seek(sent)
                data = f.read(chunk)
                end = sent + len(data) - 1
                r = h.put(session, content=data, headers={'Content-Range': f'bytes {sent}-{end}/{size}',
                                                          'Content-Type': 'video/mp4'})
                if r.status_code in (200, 201):
                    info = _json(r)
                elif r.status_code == 308:
                    # Google reports what it holds; progress must advance and never exceed what was sent.
                    got = r.headers.get('Range', '')
                    got = int(got.rsplit('-', 1)[1]) + 1 if got.startswith('bytes=0-') else 0
                    if not sent < got <= end + 1:
                        raise RuntimeError('Google Drive reported inconsistent upload progress — upload again')
                    sent = got
                else:
                    _require(r, 'upload')
    except httpx.HTTPError:
        raise RuntimeError('The upload to Google Drive was interrupted — it will be reconciled on retry') from None
    if not _matches(info, file_id, size, props):
        raise RuntimeError('Google Drive did not confirm the upload — upload again')
    result = _confirm(owner_id, job_id, variant, file_id, generation, info)
    return set_sharing(user, job_id, True, variant) if public else result


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
