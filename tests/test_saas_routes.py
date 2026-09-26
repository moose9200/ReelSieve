"""HTTP boundary: FastAPI TestClient over real isolated PostgreSQL, synthetic Google, fake renders."""
import re
import sys
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import auth, gdrive, jobs, server, worker
from fakes import connect

URL = 'https://www.airbnb.co.uk/rooms/4242'
SUCCESS = r'''
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / 'a.mp4').write_bytes(b'primary'); (d / 'b.mp4').write_bytes(b'small')
print(json.dumps({'result': {'video': str(d / 'a.mp4'), 'video_720': str(d / 'b.mp4'), 'duration': 31,
      'listing': {'url': 'https://www.airbnb.co.uk/rooms/4242', 'title': 'Sea view flat', 'city': 'Brighton', 'host': 'Sam'}}}), flush=True)
'''


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


def csrf(session):
    return {'X-CSRF-Token': auth.csrf_token(session)}


@pytest.fixture
def web(owners, google, monkeypatch, tmp_path):
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    monkeypatch.delenv('RAILWAY_PUBLIC_DOMAIN', raising=False)
    clients = {name: client_for(tok) for name, tok in owners.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def done_job(owners, google, db):
    """Alice has a finished, delivered reel (render replaced by a tiny child process)."""
    connect(owners, google)
    job = jobs.admit('alice@example.test', URL, {'message': 'Hi {host_name}, search {search_phrase}'})
    worker.process(jobs.claim('w', 30), lambda j, d: [sys.executable, '-c', SUCCESS, str(d)])
    assert jobs.get('alice@example.test', job['id'])['status'] == 'done'
    return job['id']


def test_anonymous_is_rejected_and_health_checks_database(web):
    anon = web['anon']
    assert anon.get('/api/jobs/abcdef12').status_code == 401
    r = anon.get('/app', follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'].startswith('/login?next=')
    assert anon.get('/healthz').json()['db'] is True


def test_public_setup_is_closed_and_signup_is_member(web, db):
    anon = web['anon']
    assert anon.get('/setup', follow_redirects=False).headers['location'] == '/signup'
    page = anon.get('/signup')
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    r = anon.post('/signup', data={'csrf': token, 'user': 'carol@example.org', 'password': 'long-enough-pass'}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    assert auth.role('carol@example.org') == 'member'


def test_anonymous_forms_need_their_own_browser_nonce(web):
    fresh = client_for()
    r = fresh.post('/login', data={'csrf': auth.csrf_token('anon:'), 'user': 'alice@example.test', 'password': 'synthetic-password'})
    assert r.status_code == 403
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', fresh.get('/login').text).group(1)
    other = client_for()
    other.get('/login')
    assert other.post('/login', data={'csrf': token, 'user': 'alice@example.test', 'password': 'synthetic-password'}).status_code == 403
    r = fresh.post('/login', data={'csrf': token, 'user': 'alice@example.test', 'password': 'synthetic-password',
                                   'next': '//evil.example/steal'}, follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'] == '/app'


@pytest.mark.parametrize('path', ['/api/jobs', '/api/account/password', '/api/gdrive/disconnect', '/api/outreach/queue',
                                  '/api/billing/request', '/api/jobs/abcdef12/cancel', '/logout', '/oauth/google/start'])
def test_every_mutation_requires_csrf(web, owners, path):
    alice = web['alice']
    assert alice.post(path, json={}).status_code == 403
    assert alice.post(path, json={}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert alice.post(path, json={}, headers=csrf(owners['bob'])).status_code == 403


def test_webhook_is_signature_checked_not_csrf(web):
    assert web['anon'].post('/api/billing/webhook/skydo', content=b'{}').status_code == 401


def test_missing_drive_blocks_generation_without_charge(web, owners, db):
    r = web['bob'].post('/api/jobs', json={'url': URL}, headers=csrf(owners['bob']))
    assert r.status_code == 412 and 'Google Drive' in r.json()['detail']
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM usage').fetchone()['n'] == 0
    assert 'Connect your Google Drive first' in web['bob'].get('/app').text


def test_owner_boundary_on_every_job_route(web, owners, google, db):
    jid = done_job(owners, google, db)
    alice, bob = web['alice'], web['bob']
    assert alice.get(f'/api/jobs/{jid}').json()['stream_url'] == f'/api/jobs/{jid}/video'
    for method, path, body in [('get', f'/api/jobs/{jid}', None), ('get', f'/jobs/{jid}', None),
                               ('get', f'/api/jobs/{jid}/video', None), ('post', f'/api/jobs/{jid}/cancel', {}),
                               ('post', f'/api/jobs/{jid}/share', {'public': True}),
                               ('post', f'/api/jobs/{jid}/opened-in-browser', {'message': 'x'})]:
        r = getattr(bob, method)(path, **({'json': body, 'headers': csrf(owners['bob'])} if method == 'post' else {}))
        assert r.status_code == 404, path
    assert jid not in bob.get('/reels').text and bob.get('/api/reels/index').json() == {}
    auth.create_user('ops@example.test', 'synthetic-password', 'admin')
    admin = client_for(auth.issue('ops@example.test')[0])
    assert admin.get(f'/api/jobs/{jid}').status_code == 404


def test_stream_and_explicit_sharing(web, owners, google, db):
    jid = done_job(owners, google, db)
    alice = web['alice']
    r = alice.get(f'/api/jobs/{jid}/video', headers={'Range': 'bytes=0-4'})
    assert r.status_code == 206 and r.content == b'video' and r.headers['cache-control'] == 'private, no-store'
    view = alice.get(f'/api/jobs/{jid}').json()
    assert view['shared'] is False and view['reel_link'] is None and '(reel link not shared yet)' not in view['message_final']
    google.fail_permission = True
    assert alice.post(f'/api/jobs/{jid}/share', json={'public': True}, headers=csrf(owners['alice'])).status_code == 502
    assert alice.get(f'/api/jobs/{jid}').json()['shared'] is False
    google.fail_permission = False
    shared = alice.post(f'/api/jobs/{jid}/share', json={'public': True}, headers=csrf(owners['alice'])).json()
    assert shared['shared'] and shared['reel_link'].startswith('https://drive.google.com/')


def test_cancel_queued_job_via_route(web, owners, google, db):
    connect(owners, google)
    r = web['alice'].post('/api/jobs', json={'url': URL}, headers={**csrf(owners['alice']), 'Idempotency-Key': 'k1'})
    jid = r.json()['id']
    again = web['alice'].post('/api/jobs', json={'url': URL}, headers={**csrf(owners['alice']), 'Idempotency-Key': 'k1'})
    assert again.json()['id'] == jid
    out = web['alice'].post(f'/api/jobs/{jid}/cancel', headers=csrf(owners['alice'])).json()
    assert out['status'] == 'cancelled' and not out['cancellable']


def test_password_change_revokes_the_session(web, owners):
    alice = web['alice']
    r = alice.post('/api/account/password', json={'current': 'synthetic-password', 'new': 'another-long-pass'}, headers=csrf(owners['alice']))
    assert r.json()['relogin'] == '/login?notice=pw'
    stale = client_for(owners['alice'])
    assert stale.get('/api/account').status_code == 401


def test_oauth_start_is_post_and_callback_bound_to_session(web, owners, google):
    alice = web['alice']
    assert alice.get('/oauth/google/start', follow_redirects=False).headers['location'] == '/account'
    r = alice.post('/oauth/google/start', headers=csrf(owners['alice']), follow_redirects=False)
    assert r.headers['location'].startswith(gdrive.AUTH)
    state = parse_qs(urlparse(r.headers['location']).query)['state'][0]
    other = client_for(auth.issue('alice@example.test')[0])
    r = other.get(f'/oauth/google/callback?code=c&state={state}', follow_redirects=False)
    assert 'connect%20failed' in r.headers['location'] and not gdrive.connected('alice@example.test')
    r = alice.get(f'/oauth/google/callback?code=c&state={state}', follow_redirects=False)
    assert r.headers['location'] == '/account?saved=1' and gdrive.connected('alice@example.test')


def test_disconnect_reports_failed_revocation(web, owners, google):
    connect(owners, google)
    google.fail_revoke = True
    body = web['alice'].post('/api/gdrive/disconnect', headers=csrf(owners['alice'])).json()
    assert body['connected'] is False and 'revocation' in body['warning']


def test_tracker_labels_linkedin_searches_honestly_and_keeps_only_exact_airbnb_profiles(web, owners, db):
    alice = web['alice']
    search = 'https://www.linkedin.com/search/results/people/?keywords=property%20manager%20William%20Poole'
    items = [{'name': 'William', 'url': search, 'city': 'Poole', 'airbnb_profile': 'https://www.airbnb.co.uk/users/show/111'},
             {'name': 'Trinh', 'url': search, 'city': 'Poole', 'airbnb_profile': 'javascript:alert(1)'}]
    assert alice.post('/api/outreach/queue', json={'channel': 'linkedin', 'items': items}, headers=csrf(owners['alice'])).status_code == 200
    page = alice.get('/outreach').text
    assert page.count('Search LinkedIn ↗') == 2 and 'LinkedIn profile ↗' not in page
    assert page.count('href="https://www.airbnb.co.uk/users/show/111" target="_blank" rel="noopener"') == 1
    assert 'javascript:alert' not in page
    csv_text = alice.get('/api/outreach/export.csv').text
    assert 'LinkedIn search' in csv_text and 'https://www.airbnb.co.uk/users/show/111' in csv_text and 'javascript' not in csv_text


def test_outreach_and_orders_are_owner_scoped(web, owners, db):
    alice, bob = web['alice'], web['bob']
    rid = alice.post('/api/outreach/queue', json={'items': [{'name': 'Host', 'url': 'https://www.airbnb.co.uk/users/show/1'}]},
                     headers=csrf(owners['alice'])).json()['ids'][0]
    assert bob.post('/api/outreach/status', json={'id': rid, 'status': 'won'}, headers=csrf(owners['bob'])).status_code == 404
    assert bob.post('/api/outreach/note', json={'id': rid, 'note': 'x'}, headers=csrf(owners['bob'])).status_code == 404
    assert 'Host' not in bob.get('/api/outreach/export.csv').text
    ref = alice.post('/api/billing/request', json={'plan': 'starter'}, headers=csrf(owners['alice'])).json()['order']['ref']
    assert all(o['ref'] != ref for o in bob.get('/api/billing/orders').json()['orders'])
    assert bob.get('/api/billing/orders?all=1').json().get('pending') is None


def test_deactivation_stops_jobs_and_revokes_drive(web, owners, google, db):
    connect(owners, google)
    job = jobs.admit('alice@example.test', URL, {})
    auth.create_user('ops@example.test', 'synthetic-password', 'admin')
    ops_session = auth.issue('ops@example.test')[0]
    admin = client_for(ops_session)
    r = admin.post('/api/users/delete', json={'user': 'alice@example.test'}, headers=csrf(ops_session))
    assert r.status_code == 200 and r.json()['warning'] is None
    assert any(req.url.path == '/revoke' for req in google.calls)
    with db.connect() as c:
        row = c.execute('SELECT status FROM jobs WHERE id=%s', (job['id'],)).fetchone()
        usage = c.execute('SELECT refunded_at FROM usage WHERE job_id=%s', (job['id'],)).fetchone()
    assert row['status'] == 'cancelled' and usage['refunded_at'] is not None
    assert web['alice'].get('/api/account').status_code == 401


def test_removed_operator_surfaces_are_gone(web, owners):
    alice = web['alice']
    for path in ['/api/airbnb/connect', '/api/tunnel/start', '/api/outreach/send', '/api/jobs/abcdef12/upload-drive',
                 '/api/jobs/abcdef12/send-to-host', '/settings']:
        assert alice.post(path, headers=csrf(owners['alice'])).status_code in (404, 405)
    assert alice.get('/media/abc/reel.mp4').status_code == 404


def test_startup_refuses_missing_configuration(monkeypatch):
    monkeypatch.delenv('TOKEN_ENCRYPTION_KEY', raising=False)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://x@127.0.0.1:1/x')
    monkeypatch.setenv('SESSION_SECRET', 'x')
    with pytest.raises(RuntimeError, match='TOKEN_ENCRYPTION_KEY'):
        server.validate_config()


def test_admin_sets_plan_and_credits_members_cannot(web, owners):
    from app import plans
    auth.create_user('ops@example.test', 'synthetic-password', 'admin')
    ops = auth.issue('ops@example.test')[0]
    admin = client_for(ops)
    r = admin.post('/api/users/plan', json={'user': 'bob@example.test', 'plan': 'starter', 'credits': 7}, headers=csrf(ops))
    assert r.status_code == 200 and r.json()['account']['plan'] == 'starter' and r.json()['account']['credits'] == 7
    assert plans.account_view('bob@example.test')['remaining'] == 7
    listed = {u['user']: u for u in admin.get('/api/users').json()['users']}
    assert listed['bob@example.test']['plan'] == 'starter' and listed['bob@example.test']['credits'] == 7
    assert web['alice'].post('/api/users/plan', json={'user': 'alice@example.test', 'plan': 'commercial', 'credits': 99},
                             headers=csrf(owners['alice'])).status_code == 403
    assert admin.post('/api/users/plan', json={'user': 'nobody@example.test', 'plan': 'starter', 'credits': 1}, headers=csrf(ops)).status_code == 404
    for bad in [{'plan': 'starter', 'credits': -1}, {'plan': 'starter', 'credits': 'lots'}, {'plan': 'gold', 'credits': 1}]:
        assert admin.post('/api/users/plan', json={'user': 'bob@example.test', **bad}, headers=csrf(ops)).status_code == 400
    assert plans.account_view('bob@example.test')['credits'] == 7
