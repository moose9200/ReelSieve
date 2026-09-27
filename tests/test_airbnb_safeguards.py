"""Airbnb fetch safeguards against real isolated PostgreSQL: hard stop on a block, one shared rate limit for every
process, the kill switch, and listing takedowns. Airbnb is always the synthetic fake; nothing reaches the real site."""
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from app import airbnb, auth, cohost, fetch, jobs, pipeline, search, server, store, worker
from fakes import CHALLENGE, connect

ROOT = Path(__file__).resolve().parent.parent
ALICE, ADMIN = 'alice@example.test', 'operator@example.test'
ROOM = 'https://www.airbnb.co.uk/rooms/4242'
PHOTO = 'https://a0.muscache.com/im/pictures/hosting/a.jpeg'
BLOCKED = 'Airbnb is not serving this page to us right now. Try again later.'


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


def post(client, url, body=None):
    return client.post(url, json=body or {}, headers={'X-CSRF-Token': auth.csrf_token(client.cookies.get(auth.COOKIE))})


@pytest.fixture
def web(owners):
    auth.create_user(ADMIN, 'operator-password', 'admin')
    clients = {name: client_for(tok) for name, tok in {**owners, 'admin': auth.issue(ADMIN)[0]}.items()}
    clients['anon'] = client_for()
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


@pytest.fixture
def drive(owners, google, monkeypatch, tmp_path):
    """Alice has Google Drive connected, so admission would otherwise succeed and charge."""
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    worker._stop.clear()
    connect(owners, google)
    return owners


def no_browser(monkeypatch):
    def launch(*a, **k):
        raise AssertionError('headless Chromium was started')
    monkeypatch.setitem(sys.modules, 'playwright', types.ModuleType('playwright'))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', types.SimpleNamespace(sync_playwright=launch))


def fake_browser(monkeypatch, status=200, content='<html><body>Reviews</body></html>', text=''):
    """Headless Chromium stand-in: records where it navigates, answers with the given status and page."""
    visits = []
    page = types.SimpleNamespace(goto=lambda url, **k: (visits.append(url), types.SimpleNamespace(status=status))[1],
                                 wait_for_selector=lambda *a, **k: None, wait_for_timeout=lambda *a: None,
                                 inner_text=lambda sel: text, content=lambda: content)
    browser = types.SimpleNamespace(new_page=lambda **k: page, close=lambda: None)

    class PW:
        chromium = types.SimpleNamespace(launch=lambda **k: browser)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setitem(sys.modules, 'playwright', types.ModuleType('playwright'))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', types.SimpleNamespace(sync_playwright=PW))
    return visits


def blocks(db):
    with db.connect() as c:
        return c.execute('SELECT * FROM airbnb_blocks').fetchall()


def budgets(db):
    with db.connect() as c:
        return {r['bucket']: r['next_at'] for r in c.execute('SELECT * FROM airbnb_rate').fetchall()}


def age_block(db, seconds):
    with db.connect() as c:
        c.execute('UPDATE airbnb_blocks SET ts=ts-%s', (seconds,))


# ---------------- 1. hard stop on a block, cool-down for every process ----------------

@pytest.mark.parametrize('answer', [403, 429, 451, 503, 'challenge'])
def test_a_blocked_listing_page_fails_the_job_with_no_other_route(airbnb_net, db, monkeypatch, tmp_path, caplog, answer):
    no_browser(monkeypatch)
    if answer == 'challenge':
        airbnb_net.pages['/rooms/'] = CHALLENGE
    else:
        airbnb_net.status['/rooms/'] = answer
    with caplog.at_level(logging.WARNING, logger='reelsieve.airbnb'), pytest.raises(airbnb.Unavailable) as err:
        pipeline.run(ROOM + '?adults=2', tmp_path)
    assert str(err.value) == BLOCKED
    assert airbnb_net.calls == ['www.airbnb.co.uk/rooms/4242']  # no reviews browser, no photos, no airbnb.com
    [row] = blocks(db)
    assert (row['status'], row['host'], row['reason']) == ((200, 'www.airbnb.co.uk', 'challenge') if answer == 'challenge'
                                                          else (answer, 'www.airbnb.co.uk', 'status'))
    assert abs(row['ts'] - time.time()) < 60
    assert 'www.airbnb.co.uk' in caplog.text and '4242' not in caplog.text and 'adults' not in caplog.text
    assert '4242' not in json.dumps(row)


def test_a_normal_listing_page_is_not_mistaken_for_a_challenge(airbnb_net, db):
    d = pipeline.scrape_listing(ROOM)
    assert d['city'] == 'Poole' and len(d['photos']) == 2 and blocks(db) == []


def test_a_block_pauses_all_airbnb_fetching_in_every_process_for_30_minutes(airbnb_net, db, monkeypatch):
    monkeypatch.delenv('AIRBNB_BLOCK_COOLDOWN_MIN', raising=False)
    airbnb_net.status['/rooms/'] = 429
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    airbnb_net.status.clear()
    sent = len(airbnb_net.calls)
    for call in (lambda: pipeline.scrape_listing(ROOM), lambda: search.search('Poole'), lambda: cohost.discover('Poole'),
                 lambda: fetch.get(PHOTO), lambda: pipeline.scrape_reviews(ROOM)):
        with pytest.raises(airbnb.Unavailable, match='not serving'):
            call()
    assert len(airbnb_net.calls) == sent  # nothing at all went to Airbnb during the cool-down
    other = subprocess.run([sys.executable, '-c', 'from app import airbnb\ntry:\n    airbnb.gate("https://www.airbnb.com/s/x")\n'
                            'except airbnb.Unavailable as e:\n    print(e)'], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert other.stdout.strip() == BLOCKED  # another process (web or the other worker) sees the same pause
    age_block(db, 29 * 60)
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    age_block(db, 2 * 60)
    assert pipeline.scrape_listing(ROOM)['city'] == 'Poole'


def test_cool_down_length_comes_from_the_environment(airbnb_net, db, monkeypatch):
    monkeypatch.setenv('AIRBNB_BLOCK_COOLDOWN_MIN', '5')
    airbnb_net.status['/rooms/'] = 403
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    airbnb_net.status.clear()
    age_block(db, 4 * 60)
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    age_block(db, 2 * 60)
    assert pipeline.scrape_listing(ROOM)['city'] == 'Poole'


def test_co_host_lookup_never_falls_back_to_airbnb_com_after_a_block(airbnb_net, db):
    airbnb_net.status['/host/'] = 403
    with pytest.raises(airbnb.Unavailable):
        cohost.discover('Poole')
    assert airbnb_net.calls == ['www.airbnb.co.uk/host/poole/co-hosts']


def test_co_host_lookup_keeps_its_single_retry_on_a_network_error(airbnb_net, db):
    airbnb_net.down.add('www.airbnb.co.uk')
    airbnb_net.pages['/host/'] = '<html>co-host <a href="/users/show/55">Ann</a></html>'
    res = cohost.discover('Poole')
    assert airbnb_net.calls == ['www.airbnb.co.uk/host/poole/co-hosts', 'www.airbnb.com/host/poole/co-hosts']
    assert res['source'] == 'network' and [i['name'] for i in res['items']] == ['Ann'] and blocks(db) == []


def test_a_block_during_co_host_discovery_stops_it(airbnb_net, db, monkeypatch):
    airbnb_net.status.update({'/host/': 404, '/rooms/': 429})  # no Co-Host Network page, then blocked on a listing
    monkeypatch.setattr(cohost.listing_search, 'search', lambda *a, **k: {'items': [
        {'id': str(n), 'url': f'https://www.airbnb.co.uk/rooms/{n}', 'reviews': 10, 'rating': 5.0} for n in (1, 2, 3)]})
    with pytest.raises(airbnb.Unavailable):
        cohost.discover('Poole')
    assert [c for c in airbnb_net.calls if '/rooms/' in c] == ['www.airbnb.co.uk/rooms/1']


def test_search_and_image_proxy_stop_on_a_block_and_say_why(web, airbnb_net, db):
    airbnb_net.status['/s/'] = 403
    r = web['alice'].get('/api/search', params={'location': 'Poole'})
    assert r.status_code == 503 and r.json()['detail'] == BLOCKED
    assert airbnb_net.calls == ['www.airbnb.co.uk/s/Poole/homes']
    r = web['alice'].get('/img', params={'u': PHOTO})
    assert r.status_code == 503 and r.json()['detail'] == BLOCKED and len(airbnb_net.calls) == 1


def test_a_blocked_photo_is_a_block_too(web, airbnb_net, db):
    airbnb_net.status['/im/'] = 403
    r = web['alice'].get('/img', params={'u': PHOTO})
    assert r.status_code == 503 and r.json()['detail'] == BLOCKED
    assert blocks(db)[0]['host'] == 'a0.muscache.com'


@pytest.mark.parametrize('pause', ['cool-down', 'switched off'])
def test_worker_fails_fast_with_the_message_and_refunds(drive, db, monkeypatch, pause):
    job = jobs.admit(ALICE, ROOM, {})
    if pause == 'cool-down':
        with db.connect() as c:
            c.execute("INSERT INTO airbnb_blocks(id,ts,status,host,reason) VALUES(1,%s,429,'www.airbnb.co.uk','status')",
                      (time.time(),))
        message = BLOCKED
    else:
        monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '0')
        message = airbnb.DISABLED
    started = time.time()
    worker.process(jobs.claim('w', 30))  # the real render child, which stops before any request
    failed = jobs.get(ALICE, job['id'])
    assert failed['status'] == 'failed' and failed['error'] == message
    assert time.time() - started < 20
    with db.connect() as c:
        assert c.execute('SELECT refunded_at FROM usage WHERE job_id=%s', (job['id'],)).fetchone()['refunded_at']


# ---------------- 2. one shared, polite rate limit ----------------

def test_default_budgets_are_one_page_and_ten_photos_per_second(monkeypatch):
    for k in ('AIRBNB_PAGE_RPS', 'AIRBNB_IMAGE_RPS'):
        monkeypatch.delenv(k, raising=False)
    assert airbnb.interval('page') == 1.0 and airbnb.interval('image') == 0.1
    monkeypatch.setenv('AIRBNB_PAGE_RPS', '0.5')
    monkeypatch.setenv('AIRBNB_IMAGE_RPS', '4')
    assert airbnb.interval('page') == 2.0 and airbnb.interval('image') == 0.25


@pytest.mark.parametrize('url,kind', [
    ('https://www.airbnb.co.uk/rooms/1', 'page'), ('https://www.airbnb.com/host/x/co-hosts', 'page'),
    ('https://airbnb.fr/s/x', 'page'), ('https://www.airbnb.com.au/rooms/1', 'page'), ('https://a0.muscache.com/im/a.jpg', 'image'),
    ('https://muscache.com/a.jpg', 'image'), ('https://www.airbnb.co.uk.evil.test/rooms/1', None), ('https://evilairbnb.com/', None),
    ('https://notmuscache.com/a.jpg', None), ('https://photon.komoot.io/api/', None), ('', None)])
def test_every_airbnb_and_muscache_host_is_limited_and_nothing_else(url, kind):
    assert airbnb.kind(url) == kind


LIMITED = ('import json, time\nfrom app import airbnb\nout = []\nfor _ in range(5):\n'
           '    airbnb.gate("https://www.airbnb.co.uk/rooms/1")\n    out.append(time.time())\nprint(json.dumps(out))')


def test_the_web_and_both_workers_share_one_page_budget(db, monkeypatch):
    monkeypatch.setenv('AIRBNB_PAGE_RPS', '10')
    procs = [subprocess.Popen([sys.executable, '-c', LIMITED], cwd=ROOT, stdout=subprocess.PIPE, text=True) for _ in range(3)]
    stamps = sorted(t for p in procs for t in json.loads(p.communicate(timeout=60)[0]))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 15 and min(gaps) > 0.07 and stamps[-1] - stamps[0] >= 1.3


def test_every_airbnb_request_passes_the_gate_in_its_own_budget(web, airbnb_net, db, monkeypatch, tmp_path):
    kinds = []
    real = airbnb.gate
    monkeypatch.setattr(airbnb, 'gate', lambda url: (kinds.append(airbnb.kind(url)), real(url))[1])
    airbnb_net.pages.update({'/s/': '<html></html>', '/host/': '<html>co-host <a href="/users/show/55">Ann</a></html>'})
    assert web['alice'].get('/api/search', params={'location': 'Poole'}).status_code == 200
    assert web['alice'].get('/api/outreach/cohosts', params={'city': 'Poole'}).status_code == 200
    assert web['alice'].get('/img', params={'u': PHOTO}).status_code == 200
    d = pipeline.scrape_listing(ROOM)
    pipeline.download_photos(d, tmp_path / 'img')
    assert kinds == ['page', 'page', 'image', 'page', 'image', 'image'] and len(kinds) == len(airbnb_net.calls)
    assert budgets(db)['page'] > 0 and budgets(db)['image'] > 0


def test_photos_download_in_parallel_within_the_image_budget(airbnb_net, db, monkeypatch, tmp_path):
    monkeypatch.setenv('AIRBNB_IMAGE_RPS', '20')
    d = {'photos': [{'label': '', 'url': f'https://a0.muscache.com/im/pictures/hosting/{n}.jpeg'} for n in range(10)]}
    started = time.time()
    pipeline.download_photos(d, tmp_path / 'img')
    assert len(list((tmp_path / 'img').iterdir())) == 10 and time.time() - started >= 0.4  # 10 photos at 20/s
    assert budgets(db)['page'] == 0


def test_non_airbnb_fetches_are_not_limited(airbnb_net, db):
    with pytest.raises(AssertionError, match='Unexpected synthetic'):
        fetch.get('https://example.org/other')  # went straight to the (fake) network
    assert budgets(db) == {'page': 0, 'image': 0}


# ---------------- 3. kill switch ----------------

def test_switch_off_refuses_listing_links_at_admission_without_charge(drive, db, monkeypatch):
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '0')
    with pytest.raises(jobs.AdmissionError) as err:
        jobs.admit(ALICE, ROOM, {})
    assert err.value.status == 503 and str(err.value) == airbnb.DISABLED
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'] == 0
        assert c.execute('SELECT count(*) AS n FROM usage').fetchone()['n'] == 0
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '1')
    assert jobs.admit(ALICE, ROOM, {})['status'] == 'queued'


def test_switch_off_shows_a_notice_on_search_and_co_hosts_only(web, airbnb_net, monkeypatch):
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '0')
    app_page, outreach = web['alice'].get('/app').text, web['alice'].get('/outreach').text
    search_card = app_page.split('search-card', 1)[1].split('</section>', 1)[0]
    co_card = outreach.split('id="cohost-card"', 1)[1].split('</section>', 1)[0]
    assert airbnb.DISABLED in search_card and airbnb.DISABLED in co_card
    assert 'id="search-btn" disabled' in search_card and 'id="co-find" disabled' in co_card
    r = post(web['alice'], '/api/jobs', {'url': ROOM})
    assert r.status_code == 503 and r.json()['detail'] == airbnb.DISABLED
    for path, params in (('/api/search', {'location': 'Poole'}), ('/api/search/more', {'location': 'Poole', 'page': 1}),
                         ('/api/outreach/cohosts', {'city': 'Poole'}), ('/api/outreach/linkedin', {'city': 'Poole'})):
        r = web['alice'].get(path, params=params)
        assert r.status_code == 503 and r.json()['detail'] == airbnb.DISABLED, path
    assert airbnb_net.calls == []
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '1')
    assert airbnb.DISABLED not in web['alice'].get('/app').text and airbnb.DISABLED not in web['alice'].get('/outreach').text


def test_switch_off_leaves_the_image_proxy_alone(web, airbnb_net, monkeypatch):
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '0')
    assert web['alice'].get('/img', params={'u': PHOTO}).status_code == 200


def test_admin_settings_show_the_switch_state_and_the_last_block(web, db, monkeypatch):
    monkeypatch.delenv('AIRBNB_FETCH_ENABLED', raising=False)
    rows = {s['key']: s for s in server.settings_view()}
    assert rows['AIRBNB_FETCH_ENABLED']['state'] == 'On' and rows['AIRBNB_BLOCK_COOLDOWN_MIN']['value'] == '30 (default)'
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '0')
    config = web['admin'].get('/settings').text.split('id="config-card"', 1)[1]
    assert re.search(r'<code>AIRBNB_FETCH_ENABLED</code>\s*<span class="pill pill-sm pill-neg">Off</span>', config)
    assert 'No Airbnb block recorded' in config
    with db.connect() as c:
        c.execute("INSERT INTO airbnb_blocks(id,ts,status,host,reason) VALUES(1,%s,429,'www.airbnb.co.uk','status')", (time.time(),))
    config = web['admin'].get('/settings').text.split('id="config-card"', 1)[1]
    assert 'Last Airbnb block' in config and '429' in config and 'paused until' in config
    assert web['alice'].get('/settings', follow_redirects=False).status_code == 303


# ---------------- 4. reviews: the base page carries none, so the reviews page stays (see report) ----------------

def test_reviews_still_come_from_the_reviews_page_through_the_limiter(db, monkeypatch):
    text = '\n'.join(['Namey', 'Leeds, UK', 'Rating, 5 stars', '·', 'August 2026', 'Spotless flat with a lovely view of the harbour.', ''])
    visits = fake_browser(monkeypatch, text=text)
    assert pipeline.scrape_reviews(ROOM) == [{'stars': 5, 'date': 'August 2026', 'text': 'Spotless flat with a lovely view of the harbour.'}]
    assert visits == [ROOM + '/reviews'] and budgets(db)['page'] > 0


@pytest.mark.parametrize('status,content', [(403, '<html></html>'), (200, CHALLENGE)])
def test_a_blocked_reviews_page_stops_the_job(db, monkeypatch, status, content):
    fake_browser(monkeypatch, status=status, content=content)
    with pytest.raises(airbnb.Unavailable, match='not serving'):
        pipeline.scrape_reviews(ROOM)
    assert blocks(db)[0]['host'] == 'www.airbnb.co.uk'


# ---------------- 5. listing takedowns ----------------

def _request(client, **fields):
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get('/privacy/request').text).group(1)
    return client.post('/privacy/request', data={'csrf': token, 'name': 'Pat Host', 'email': 'pat@example.org',
                                                 'type': 'listing_removal', 'details': '', 'airbnb_profile': '', **fields})


def test_takedown_request_blocks_the_listing_at_once_and_is_recorded(web, db):
    assert 'Remove my listing from ReelSieve' in web['anon'].get('/privacy/request').text
    r = _request(web['anon'], listing_url=' https://www.airbnb.com/rooms/4242?adults=2 ')
    assert r.status_code == 200, r.text
    ref = re.search(r'PR-\d{6}-[0-9A-F]{6}', r.text).group(0)
    assert '4242' in r.text
    with db.connect() as c:
        req = c.execute('SELECT * FROM privacy_requests').fetchone()
        blocked = c.execute('SELECT * FROM blocked_listings').fetchall()
    assert (req['type'], req['listing_id'], req['status'], req['email']) == ('listing_removal', '4242', 'open', 'pat@example.org')
    assert [(b['listing_id'], b['reason']) for b in blocked] == [('4242', 'Removal request ' + ref)]
    assert abs(blocked[0]['ts'] - time.time()) < 60
    settings = web['admin'].get('/settings').text
    assert ref in settings and 'Listing 4242' in settings
    for bad in ('', 'https://www.airbnb.co.uk/users/show/1', 'https://evil.example.org/rooms/1', 'https://www.airbnb.co.uk.evil.test/rooms/5',
                'not a link'):
        assert _request(client_for(), listing_url=bad).status_code == 400, bad
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM blocked_listings').fetchone()['n'] == 1


def test_a_listing_link_on_another_request_type_blocks_nothing(web, db):
    assert _request(web['anon'], type='other', details='Question', listing_url=ROOM).status_code == 200
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM blocked_listings').fetchone()['n'] == 0


def test_admission_refuses_a_blocked_listing_without_charge(drive, db):
    store.block_listing('4242', 'test')
    with pytest.raises(jobs.AdmissionError) as err:
        jobs.admit(ALICE, 'https://www.airbnb.com/rooms/4242?adults=2', {})
    assert err.value.status == 403 and str(err.value) == "This listing has been removed from ReelSieve, so we can't make a reel of it."
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM usage').fetchone()['n'] == 0
    assert jobs.admit(ALICE, 'https://www.airbnb.co.uk/rooms/4243', {})['status'] == 'queued'


def test_admins_add_and_remove_blocked_listings(web, db):
    assert post(web['alice'], '/api/blocked-listings', {'listing': ROOM}).status_code == 403
    assert web['admin'].post('/api/blocked-listings', json={'listing': ROOM}).status_code == 403  # CSRF
    for bad in ('https://evil.example.org/rooms/1', 'abc', ''):
        assert post(web['admin'], '/api/blocked-listings', {'listing': bad}).status_code == 400, bad
    assert post(web['admin'], '/api/blocked-listings', {'listing': ROOM, 'reason': 'Airbnb takedown'}).status_code == 200
    assert post(web['admin'], '/api/blocked-listings', {'listing': '4243'}).status_code == 200
    card = web['admin'].get('/settings').text.split('id="blocked-card"', 1)[1].split('</section>', 1)[0]
    assert '4242' in card and 'Airbnb takedown' in card and '4243' in card and 'Added by an admin' in card
    assert post(web['alice'], '/api/blocked-listings/remove', {'listing_id': '4242'}).status_code == 403
    assert post(web['admin'], '/api/blocked-listings/remove', {'listing_id': '4242'}).status_code == 200
    assert post(web['admin'], '/api/blocked-listings/remove', {'listing_id': '4242'}).status_code == 404
    assert store.blocked_ids(['4242', '4243']) == {'4243'}
    activity = web['admin'].get('/settings').text.split('id="events-card"', 1)[1].split('</section>', 1)[0]
    assert 'Listing blocked' in activity and 'Listing unblocked' in activity and '4242' in activity


PROSPECTS = [{'id': str(n), 'name': name, 'url': f'https://www.airbnb.co.uk/contact_host/{n}/send_message',
              'listing_url': f'https://www.airbnb.co.uk/rooms/{n}', 'city': 'Leeds', 'listing_title': 'Flat'}
             for n, name in ((111, 'Jo'), (222, 'Sam'), (333, 'Kim'))]


def test_blocked_listings_never_appear_in_co_host_or_outreach_results(web, db, monkeypatch):
    store.block_listing('222', 'test')
    monkeypatch.setattr(cohost, 'discover', lambda city, limit=12: {'city': city, 'items': [dict(p) for p in PROSPECTS],
                                                                    'source': 'operators', 'note': ''})
    assert [p['name'] for p in web['alice'].get('/api/outreach/cohosts?city=Leeds').json()['items']] == ['Jo', 'Kim']
    assert [p['name'] for p in web['alice'].get('/api/outreach/linkedin?city=Leeds').json()['items']] == ['Jo', 'Kim']
    post(web['alice'], '/api/outreach/queue', {'channel': 'cohost', 'items': [dict(p, message='Hi') for p in PROSPECTS]})
    assert sorted(r['name'] for r in store.outreach_rows(ALICE)) == ['Jo', 'Kim']


def test_co_host_discovery_does_not_fetch_a_blocked_listing(airbnb_net, db, monkeypatch):
    store.block_listing('2', 'test')
    airbnb_net.status['/host/'] = 404
    monkeypatch.setattr(cohost.listing_search, 'search', lambda *a, **k: {'items': [
        {'id': str(n), 'url': f'https://www.airbnb.co.uk/rooms/{n}', 'reviews': 10 - n, 'rating': 5.0} for n in (1, 2, 3)]})
    res = cohost.discover('Poole')
    assert [c for c in airbnb_net.calls if '/rooms/' in c] == ['www.airbnb.co.uk/rooms/1', 'www.airbnb.co.uk/rooms/3']
    assert '2' not in [i['id'] for i in res['items']]
