"""Real isolated PostgreSQL and encryption; only Google's HTTP boundary is synthetic."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from app import auth, gdrive
from fakes import connect


@pytest.mark.parametrize('user,session', [(None, ''), ('bob@example.test', 'alice'), ('alice@example.test', 'bad')])
def test_oauth_requires_current_matching_session(owners, google, user, session):
    with pytest.raises(ValueError):
        gdrive.auth_url('https://app.test/callback', user, owners.get(session, session))
    assert not gdrive.connected('alice@example.test')


def test_callback_bound_to_exact_initiating_session(owners, google):
    url = gdrive.auth_url('https://app.test/callback', 'alice@example.test', owners['alice'])
    state = parse_qs(urlparse(url).query)['state'][0]
    other = auth.issue('alice@example.test')[0]
    for user, token in [(None, ''), ('bob@example.test', owners['bob']), ('alice@example.test', other)]:
        with pytest.raises(ValueError):
            gdrive.exchange('code', state, 'https://app.test/callback', user, token)
    assert not gdrive.connected('alice@example.test')
    gdrive.exchange('code', state, 'https://app.test/callback', 'alice@example.test', owners['alice'])
    with pytest.raises(ValueError):
        gdrive.exchange('code', state, 'https://app.test/callback', 'alice@example.test', owners['alice'])


def test_expired_state_and_redirect_mismatch_rejected(owners, google, db):
    url = gdrive.auth_url('https://app.test/callback', 'alice@example.test', owners['alice'])
    state = parse_qs(urlparse(url).query)['state'][0]
    with pytest.raises(ValueError):
        gdrive.exchange('code', state, 'https://evil.test/callback', 'alice@example.test', owners['alice'])
    with db.connect() as c:
        c.execute('UPDATE drive_oauth_states SET expires_at=%s', (time.time() - 1,))
    with pytest.raises(ValueError):
        gdrive.exchange('code', state, 'https://app.test/callback', 'alice@example.test', owners['alice'])


@pytest.mark.parametrize('bad', ['scope', 'refresh', 'identity'])
def test_missing_grant_requirements_never_connected(owners, google, bad):
    if bad == 'scope': google.scope = 'openid email'
    if bad == 'refresh': google.refresh = None
    if bad == 'identity': google.sub = ''
    with pytest.raises(RuntimeError): connect(owners, google)
    assert not gdrive.connected('alice@example.test')


def test_encrypted_storage_bound_to_owner(owners, google, db):
    connect(owners, google)
    connect(owners, google, 'bob')
    with db.connect() as c:
        rows = c.execute('SELECT owner_id,credentials FROM drive_connections').fetchall()
        raw = str(rows)
        assert 'synthetic-refresh-secret' not in raw and 'synthetic-access-secret' not in raw
        c.execute('UPDATE drive_connections SET credentials=%s WHERE owner_id=%s', (rows[0]['credentials'], rows[1]['owner_id']))
    with pytest.raises(RuntimeError): gdrive.access_token('bob@example.test')
    assert not gdrive.connected('bob@example.test')


def test_disconnect_leaves_other_owner_connected(owners, google):
    connect(owners, google); connect(owners, google, 'bob')
    gdrive.disconnect('bob@example.test')
    assert gdrive.connected('alice@example.test') and not gdrive.connected('bob@example.test')
    assert gdrive.status('bob@example.test')['email'] is None
    assert gdrive.access_token('alice@example.test') == 'synthetic-access-secret'


def test_reconnect_new_identity_cannot_inherit_refresh(owners, google):
    connect(owners, google)
    google.sub, google.refresh = 'another-google', None
    with pytest.raises(RuntimeError): connect(owners, google)
    assert gdrive.status('alice@example.test')['email'] == 'google-alice@gmail.test'


def test_inactive_owner_cannot_access_drive(owners, google, db):
    connect(owners, google)
    with db.connect() as c: c.execute('UPDATE users SET active=FALSE WHERE email=%s', ('alice@example.test',))
    assert not gdrive.connected('alice@example.test')
    with pytest.raises((ValueError, RuntimeError)): gdrive.access_token('alice@example.test')


def test_disconnect_revocation_failure_explicit_but_local_access_disabled(owners, google):
    connect(owners, google)
    google.fail_revoke = True
    with pytest.raises(RuntimeError, match='revoke|revocation'): gdrive.disconnect('alice@example.test')
    assert not gdrive.connected('alice@example.test')
    with pytest.raises(RuntimeError): gdrive.access_token('alice@example.test')


def test_expired_grant_requires_reconnect_without_secret_leak(owners, google, db):
    connect(owners, google)
    tok = gdrive._load('alice@example.test')
    tok['expires_at'] = 0
    gdrive._save('alice@example.test', tok)
    google.refresh_error = True
    with pytest.raises(RuntimeError, match='[Rr]econnect') as err: gdrive.access_token('alice@example.test')
    assert 'sensitive' not in str(err.value)
    assert not gdrive.connected('alice@example.test')


def test_refresh_cannot_resurrect_disconnect(owners, google):
    connect(owners, google)
    tok = gdrive._load('alice@example.test'); tok['expires_at'] = 0
    gdrive._save('alice@example.test', tok)
    google.hook = lambda req: gdrive.disconnect('alice@example.test')
    with pytest.raises(RuntimeError): gdrive.access_token('alice@example.test')
    assert not gdrive.connected('alice@example.test')


def test_private_upload_receipt_and_owner_folder(owners, google, tmp_path):
    connect(owners, google)
    connect(owners, google, 'bob')
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'synthetic-video')
    a = gdrive.upload(path, 'https://listing.test/1', 'alice@example.test', job_id='job-a')
    b = gdrive.upload(path, 'https://listing.test/1', 'bob@example.test', job_id='job-b')
    assert a['confirmed'] and a['sharing'] == 'private' and a['id'] != b['id']
    assert not any('/permissions' in r.url.path for r in google.calls)
    assert len(set(google.folder_queries)) == 2 and all('appProperties' in q for q in google.folder_queries)
    assert gdrive.upload(path, 'https://listing.test/1', 'alice@example.test', job_id='job-a')['id'] == a['id']


@pytest.mark.parametrize('bad', ['receipt', 'range'])
def test_invalid_upload_progress_never_confirmed(owners, google, tmp_path, bad):
    connect(owners, google)
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'x' * (256 * 1024 + 1))
    google.bad_receipt = bad == 'receipt'; google.bad_range = bad == 'range'
    with pytest.raises(RuntimeError): gdrive.upload(path, 'listing', 'alice@example.test', job_id='bad', chunk=256 * 1024)
    assert gdrive.receipt('alice@example.test', 'bad') is None


def test_interrupted_upload_reconciles_same_file(owners, google, tmp_path, db):
    connect(owners, google)
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'video')
    google.interrupt = True
    with pytest.raises(RuntimeError) as err: gdrive.upload(path, 'listing', 'alice@example.test', job_id='retry')
    assert 'sensitive' not in str(err.value)
    result = gdrive.upload(path, 'listing', 'alice@example.test', job_id='retry')
    assert result['confirmed'] and len(google.files) == 1
    with db.connect() as c:
        raw = str(c.execute('SELECT * FROM drive_uploads').fetchall())
        assert 'https://www.googleapis.com/upload/session/' not in raw


def test_share_and_stream_require_own_receipt(owners, google, tmp_path):
    connect(owners, google); connect(owners, google, 'bob')
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'video')
    gdrive.upload(path, 'listing', 'alice@example.test', job_id='own')
    with pytest.raises(ValueError): gdrive.set_sharing('bob@example.test', 'own', public=True)
    with pytest.raises(ValueError):
        with gdrive.open_stream('bob@example.test', 'own'): pass
    google.fail_permission = True
    with pytest.raises(RuntimeError): gdrive.set_sharing('alice@example.test', 'own', public=True)
    assert gdrive.receipt('alice@example.test', 'own')['sharing'] == 'private'
    google.fail_permission = False
    assert gdrive.set_sharing('alice@example.test', 'own', public=True)['sharing'] == 'public'
    with gdrive.open_stream('alice@example.test', 'own', range_header='bytes=0-4') as response:
        assert response.status_code == 206 and response.read() == b'video'


def test_upload_completion_cannot_survive_disconnect(owners, google, tmp_path):
    connect(owners, google)
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'video')
    original = google.handle
    def boundary(req):
        response = original(req)
        if req.url.path.startswith('/upload/session/'):
            gdrive.disconnect('alice@example.test')
        return response
    # Hook at final receipt, after Google has received bytes.
    google.handle = boundary
    with pytest.raises(RuntimeError): gdrive.upload(path, 'listing', 'alice@example.test', job_id='race')
    assert gdrive.receipt('alice@example.test', 'race') is None


def test_upload_resumes_when_google_holds_nothing(owners, google, tmp_path):
    connect(owners, google)
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'x' * (256 * 1024 + 1))
    google.drop_chunk = True
    result = gdrive.upload(path, 'listing', 'alice@example.test', job_id='resume', chunk=256 * 1024)
    assert result['confirmed'] and google.files[result['id']]['size'] == str(256 * 1024 + 1)


def test_upload_pinned_to_admitted_generation(owners, google, tmp_path, db):
    connect(owners, google)
    with db.connect() as c:
        admitted = gdrive.usable_generation(c, db.user_id('alice@example.test', c))
    connect(owners, google)
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'video')
    with pytest.raises(RuntimeError, match='reconnected'):
        gdrive.upload(path, 'listing', 'alice@example.test', job_id='pin', generation=admitted)
    assert gdrive.receipt('alice@example.test', 'pin') is None


def test_deactivated_owner_disconnect_revokes(owners, google, db):
    connect(owners, google)
    owner = db.user_id('alice@example.test')
    with db.connect() as c:
        c.execute('UPDATE users SET active=FALSE WHERE id=%s', (owner,))
    gdrive.disconnect_owner(owner)
    assert any(r.url.path == '/revoke' for r in google.calls)
    with db.connect() as c:
        assert gdrive.usable_generation(c, owner) is None
