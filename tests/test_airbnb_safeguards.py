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

from app import airbnb, auth, cohost, fetch, jobs, pipeline, retention, search, server, store, worker
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


class _Visits(list):
    def __init__(self):
        super().__init__()
        self.routed = []


class _Route:
    def __init__(self, url, resource_type, navigation):
        self.request = types.SimpleNamespace(url=url, resource_type=resource_type, method='GET',
                                             is_navigation_request=lambda: navigation)
        self.outcome = None

    def abort(self):
        self.outcome = 'abort'

    def continue_(self):
        self.outcome = 'continue'


def fake_browser(monkeypatch, status=200, content='<html><body>Reviews</body></html>', text='', subrequests=()):
    """Headless Chromium stand-in: records where it navigates, answers with the given status and page. Like the real page
    it then loads subrequests, (url, resource type, status) each, through the page's route handler and response listeners.
    Returns the navigations; .routed holds (url, resource type, 'continue' or 'abort') for every request."""
    visits, handlers = _Visits(), {'response': []}

    def goto(url, **k):
        visits.append(url)
        for u, rtype, st in [(url, 'document', status), *subrequests]:
            route = _Route(u, rtype, rtype == 'document')
            handlers['route'](route) if 'route' in handlers else route.continue_()
            visits.routed.append((u, rtype, route.outcome))
            if route.outcome == 'continue':
                for h in handlers['response']:
                    h(types.SimpleNamespace(url=u, status=st))
        return types.SimpleNamespace(status=status)
    page =types.SimpleNamespace(goto=goto, route=lambda pattern, h: handlers.__setitem__('route', h),
                                 on=lambda event, h: handlers[event].append(h),
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


def test_a_refused_photo_in_a_render_job_is_a_block(airbnb_net, db, tmp_path):
    airbnb_net.status['/im/'] = 403
    with pytest.raises(airbnb.Unavailable, match='not serving'):
        pipeline.download_photos({'photos': [{'label': '', 'url': PHOTO}]}, tmp_path / 'img')
    assert blocks(db)[0]['host'] == 'a0.muscache.com'


def test_a_refused_photo_through_the_image_proxy_pauses_nothing(web, airbnb_net, db):
    """/img takes any muscache URL a signed-in user sends, so its answer is never a reason to stop the service."""
    airbnb_net.status['/im/'] = 403
    assert web['alice'].get('/img', params={'u': PHOTO}).status_code == 400
    assert blocks(db) == [] and pipeline.scrape_listing(ROOM)['city'] == 'Poole'


@pytest.mark.parametrize('status', [401, 407])
def test_an_authentication_refusal_is_a_block(airbnb_net, db, status):
    airbnb_net.status['/rooms/'] = status
    with pytest.raises(airbnb.Unavailable, match='not serving'):
        pipeline.scrape_listing(ROOM)
    assert blocks(db)[0]['status'] == status


def test_co_host_lookup_never_tries_airbnb_com_after_an_answer_from_airbnb_co_uk(airbnb_net, db, monkeypatch):
    airbnb_net.status['/host/'] = 404  # no Co-Host Network page for the city: the operators fallback, not another site
    monkeypatch.setattr(cohost.listing_search, 'search', lambda *a, **k: {'items': []})
    cohost.discover('Poole')
    assert airbnb_net.calls == ['www.airbnb.co.uk/host/poole/co-hosts']


def resume(web):
    return post(web['admin'], '/api/airbnb/resume')


def test_blocks_are_kept_and_a_repeat_after_the_cool_down_stops_fetching_until_an_admin_resumes(web, airbnb_net, db):
    airbnb_net.status['/rooms/'] = 403
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    age_block(db, 31 * 60)
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)  # refused again once the pause ended
    assert [(b['status'], b['hard']) for b in blocks(db)] == [(403, False), (403, True)]
    airbnb_net.status.clear()
    age_block(db, 5 * 3600)
    sent = len(airbnb_net.calls)
    with pytest.raises(airbnb.Unavailable, match='not serving'):
        pipeline.scrape_listing(ROOM)  # no timer restarts it
    assert len(airbnb_net.calls) == sent
    config = web['admin'].get('/settings').text.split('id="config-card"', 1)[1]
    assert 'stopped until an admin resumes it' in config and 'id="airbnb-resume"' in config
    assert post(web['alice'], '/api/airbnb/resume').status_code == 403
    assert resume(web).status_code == 200
    assert pipeline.scrape_listing(ROOM)['city'] == 'Poole'
    assert len(blocks(db)) == 2 and all(b['cleared_at'] for b in blocks(db))  # the history stays
    activity = web['admin'].get('/settings').text.split('id="events-card"', 1)[1].split('</section>', 1)[0]
    assert 'Airbnb fetching resumed' in activity


def test_unavailable_for_legal_reasons_stops_fetching_at_once_until_an_admin_resumes(web, airbnb_net, db):
    airbnb_net.status['/rooms/'] = 451
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    airbnb_net.status.clear()
    age_block(db, 3 * 3600)
    with pytest.raises(airbnb.Unavailable):
        pipeline.scrape_listing(ROOM)
    assert resume(web).status_code == 200 and pipeline.scrape_listing(ROOM)['city'] == 'Poole'


def test_refusals_of_requests_already_in_flight_are_one_block(airbnb_net, db, tmp_path):
    airbnb_net.status['/im/'] = 403
    d = {'photos': [{'label': '', 'url': f'https://a0.muscache.com/im/pictures/hosting/{n}.jpeg'} for n in range(6)]}
    with pytest.raises(airbnb.Unavailable):
        pipeline.download_photos(d, tmp_path / 'img')  # six threads, several may be refused at once
    assert blocks(db) and not any(b['hard'] for b in blocks(db))
    airbnb_net.status.clear()
    age_block(db, 31 * 60)
    assert pipeline.scrape_listing(ROOM)['city'] == 'Poole'


def test_settings_say_whether_fetching_is_paused_now(web, db):
    with db.connect() as c:
        c.execute("INSERT INTO airbnb_blocks(ts,status,host,reason) VALUES(%s,429,'www.airbnb.co.uk','status')", (time.time() - 7200,))
    config = web['admin'].get('/settings').text.split('id="config-card"', 1)[1]
    assert 'Last Airbnb block' in config and 'resumed at' in config and 'paused until' not in config
    assert 'id="airbnb-resume"' not in config


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
    monkeypatch.setattr(airbnb, 'gate', lambda url, bucket=None: (kinds.append(airbnb.kind(url)), real(url, bucket))[1])
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
    assert budgets(db) == {'page': 0, 'image': 0, 'browser': 0}


def book_ahead(db, bucket, seconds):
    """Someone has already booked this many seconds of the budget."""
    with db.connect() as c:
        c.execute('UPDATE airbnb_rate SET next_at=extract(epoch FROM clock_timestamp())+%s WHERE bucket=%s', (seconds, bucket))


def test_a_long_queue_refuses_web_lookups_at_once_and_books_nothing(web, airbnb_net, db):
    for bucket, path, params in (('page', '/api/search', {'location': 'Poole'}), ('page', '/api/outreach/cohosts', {'city': 'Poole'}),
                                 ('image', '/img', {'u': PHOTO})):
        book_ahead(db, bucket, 60)  # e.g. 12 searches of 5 pages: more than a web lookup may wait, less than a job may
        before, started = budgets(db)[bucket], time.time()
        r = web['alice'].get(path, params=params)
        assert r.status_code == 503 and r.json()['detail'] == airbnb.BUSY, path
        assert time.time() - started < 3 and budgets(db)[bucket] == before, path  # no web thread asleep, no slot taken
    assert airbnb_net.calls == []


def test_web_lookups_wait_behind_a_short_queue(web, airbnb_net, db):
    book_ahead(db, 'page', 1.5)
    started = time.time()
    assert web['alice'].get('/api/search', params={'location': 'Poole'}).status_code == 502  # the fake has no search page
    assert time.time() - started >= 1.3 and airbnb_net.calls == ['www.airbnb.co.uk/s/Poole/homes']


def test_render_jobs_keep_their_place_behind_web_lookups(airbnb_net, db):
    """Web lookups can fill the queue only WEB_MAX_WAIT (10 s) ahead; a render job still queues behind that."""
    assert airbnb.WEB_MAX_WAIT == 10 and airbnb.JOB_MAX_WAIT == 120
    book_ahead(db, 'page', 1.5)
    started = time.time()
    assert pipeline.scrape_listing(ROOM)['city'] == 'Poole' and time.time() - started >= 1.3
    book_ahead(db, 'page', 30)
    token = airbnb.max_wait.set(0.5)  # a job would wait up to 120 s here; keep the test short
    try:
        with pytest.raises(airbnb.Unavailable, match='busy'):
            pipeline.scrape_listing(ROOM)
    finally:
        airbnb.max_wait.reset(token)


def test_one_account_runs_at_most_two_airbnb_lookups_at_once(web, db, monkeypatch):
    import threading
    release, running = threading.Event(), []

    def search(location, *a, **k):
        if location == 'Slow':
            running.append(location)
            release.wait(20)
        return {'items': [], 'count': 0}
    monkeypatch.setattr(server.listing_search, 'search', search)
    slow = [threading.Thread(target=web['alice'].get, args=('/api/search',), kwargs={'params': {'location': 'Slow'}}) for _ in range(2)]
    for t in slow:
        t.start()
    try:
        for _ in range(200):
            if len(running) == 2:
                break
            time.sleep(0.05)
        assert len(running) == 2
        r = web['alice'].get('/api/search', params={'location': 'Poole'})
        assert r.status_code == 429 and 'already' in r.json()['detail']
        assert web['alice'].get('/api/outreach/cohosts', params={'city': 'Poole'}).status_code == 429
        assert web['bob'].get('/api/search', params={'location': 'Poole'}).status_code == 200  # other accounts are not held up
    finally:
        release.set()
        for t in slow:
            t.join(20)
    assert web['alice'].get('/api/search', params={'location': 'Poole'}).status_code == 200


def test_one_account_has_a_limit_on_photos_in_flight(web, airbnb_net, db, monkeypatch):
    monkeypatch.setitem(server.AT_ONCE, 'img', 0)
    r = web['alice'].get('/img', params={'u': PHOTO})
    assert r.status_code == 429 and airbnb_net.calls == []
    monkeypatch.setitem(server.AT_ONCE, 'img', 24)
    assert web['alice'].get('/img', params={'u': PHOTO}).status_code == 200


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


def test_switch_off_stops_photo_fetches_too(web, airbnb_net, monkeypatch, tmp_path):
    """A clean stop after a cease-and-desist: nothing at all goes to Airbnb or its photo CDN."""
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', '0')
    r = web['alice'].get('/img', params={'u': PHOTO})
    assert r.status_code == 503 and r.json()['detail'] == airbnb.DISABLED
    with pytest.raises(airbnb.Unavailable, match='not fetching'):
        pipeline.download_photos({'photos': [{'label': '', 'url': PHOTO}]}, tmp_path / 'img')
    assert airbnb_net.calls == []
    hint = {s['key']: s['hint'] for s in server.settings_view()}['AIRBNB_FETCH_ENABLED']
    assert 'photos' in hint


@pytest.mark.parametrize('value', ['0', 'false', 'OFF', ' no '])
def test_the_switch_understands_the_usual_words_for_off(monkeypatch, value):
    monkeypatch.setenv('AIRBNB_FETCH_ENABLED', value)
    assert not airbnb.enabled()


@pytest.mark.parametrize('key,value', [('AIRBNB_FETCH_ENABLED', 'maybe'), ('AIRBNB_PAGE_RPS', '0'), ('AIRBNB_IMAGE_RPS', '-1'),
                                       ('AIRBNB_BROWSER_RPS', 'fast'), ('AIRBNB_PAGE_RPS', 'inf'), ('AIRBNB_BLOCK_COOLDOWN_MIN', 'nan')])
def test_a_bad_airbnb_setting_refuses_to_start(owners, monkeypatch, key, value):
    server.validate_config()  # the defaults are fine
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match=key):
        server.validate_config()


def test_admin_settings_show_the_switch_state_and_the_last_block(web, db, monkeypatch):
    monkeypatch.delenv('AIRBNB_FETCH_ENABLED', raising=False)
    rows = {s['key']: s for s in server.settings_view()}
    assert rows['AIRBNB_FETCH_ENABLED']['state'] == 'On' and rows['AIRBNB_BLOCK_COOLDOWN_MIN']['value'] == '30 (default)'
    assert rows['AIRBNB_BLOCK_COOLDOWN_MIN']['state'] == 'Default' and rows['HF_KEY']['state'] is None
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


REVIEW_TEXT = '\n'.join(['Namey', 'Leeds, UK', 'Rating, 5 stars', '·', 'August 2026', 'Spotless flat with a lovely view of the harbour.', ''])
DATA_CALL = 'https://www.airbnb.co.uk/api/v3/StaysPdpReviewsQuery/abc?operationName=StaysPdpReviewsQuery'
# What the live reviews page loads (27 Sep 2026: 144 muscache scripts, 45 images, 6 media, 1 font, ~38 airbnb.co.uk
# data and tracking calls, a few third-party scripts), one of each.
PAGE_LOADS = [('https://a0.muscache.com/airbnb/static/packages/web/common/a.js', 'script', 200),
              ('https://a0.muscache.com/airbnb/static/packages/web/common/b.css', 'stylesheet', 200),
              (DATA_CALL, 'fetch', 200),
              ('https://www.airbnb.co.uk/tracking/jitney/logging/messages', 'xhr', 204),
              ('https://www.airbnb.co.uk/gtg/gtm-46mk/gtm.js', 'script', 200),
              ('https://a0.muscache.com/im/pictures/hosting/a.jpeg', 'image', 200),
              ('https://a0.muscache.com/v/a.mp4', 'media', 206),
              ('https://a0.muscache.com/airbnb/static/fonts/c.woff2', 'font', 200),
              ('https://www.googletagmanager.com/gtag/js', 'script', 200)]


def test_every_airbnb_request_of_the_reviews_browser_takes_a_slot_and_pictures_are_not_loaded(db, monkeypatch):
    gated = []
    real = airbnb.gate
    monkeypatch.setattr(airbnb, 'gate', lambda url, bucket=None: (gated.append((url, bucket)), real(url, bucket))[1])
    visits = fake_browser(monkeypatch, text=REVIEW_TEXT, subrequests=PAGE_LOADS)
    assert [r['text'] for r in pipeline.scrape_reviews(ROOM)] == ['Spotless flat with a lovely view of the harbour.']
    outcome = {u: o for u, _, o in visits.routed}
    assert [u for u, _, o in visits.routed if o == 'abort'] == [u for u, t, _ in PAGE_LOADS if t in ('image', 'media', 'font')]
    airbnb_loads = [u for u, t, _ in PAGE_LOADS if t not in ('image', 'media', 'font') and airbnb.kind(u)]
    # the page itself takes a page slot (once, before Chromium starts); each script, style and data call a browser slot
    assert [(u, b) for u, b in gated if airbnb.kind(u)] == [(ROOM + '/reviews', None)] + [(u, 'browser') for u in airbnb_loads]
    assert all(outcome[u] == 'continue' for u in airbnb_loads) and budgets(db)['browser'] > 0


@pytest.mark.parametrize('status', [429, 403])
def test_a_block_on_the_reviews_data_call_stops_the_job(db, monkeypatch, status):
    fake_browser(monkeypatch, text=REVIEW_TEXT, subrequests=[PAGE_LOADS[0], (DATA_CALL, 'fetch', status)])
    with pytest.raises(airbnb.Unavailable, match='not serving'):
        pipeline.scrape_reviews(ROOM)
    [row] = blocks(db)
    assert (row['status'], row['host']) == (status, 'www.airbnb.co.uk')


def test_a_pause_during_the_reviews_page_stops_its_requests_and_the_job(db, monkeypatch):
    real = airbnb.gate

    def gate(url, bucket=None):
        if url == DATA_CALL:  # another process recorded a block while the page was loading
            with db.connect() as c:
                c.execute("INSERT INTO airbnb_blocks(ts,status,host,reason) VALUES(%s,429,'www.airbnb.co.uk','status')", (time.time(),))
        return real(url, bucket)
    monkeypatch.setattr(airbnb, 'gate', gate)
    visits = fake_browser(monkeypatch, text=REVIEW_TEXT, subrequests=PAGE_LOADS[:4])
    with pytest.raises(airbnb.Unavailable, match='not serving'):
        pipeline.scrape_reviews(ROOM)
    assert [o for _, _, o in visits.routed] == ['continue', 'continue', 'continue', 'abort', 'abort']  # from the data call on


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


def refunded(db, job_id):
    with db.connect() as c:
        return bool(c.execute('SELECT refunded_at FROM usage WHERE job_id=%s', (job_id,)).fetchone()['refunded_at'])


def test_a_takedown_stops_a_reel_already_queued_and_refunds_it(drive, db):
    job = jobs.admit(ALICE, ROOM, {})
    store.block_listing('4242', 'Removal request PR-test')

    def command(job, d):
        raise AssertionError('the render started')
    worker.process(jobs.claim('w', 30), command)
    failed = jobs.get(ALICE, job['id'])
    assert (failed['status'], failed['error']) == ('failed', jobs.REMOVED) and refunded(db, job['id'])


TAKEDOWN_DURING_RENDER = r'''
import json, pathlib, sys
from app import store
store.block_listing('4242', 'Removal request PR-test')  # the host's request lands while the reel renders
(pathlib.Path(sys.argv[1]) / 'v.mp4').write_bytes(b'video')
print(json.dumps({'result': {'video': sys.argv[1] + '/v.mp4', 'listing': {'url': 'https://www.airbnb.co.uk/rooms/4242'}}}), flush=True)
'''


def test_a_takedown_while_a_reel_renders_stops_its_delivery(drive, google, db):
    job = jobs.admit(ALICE, ROOM, {})
    worker.process(jobs.claim('w', 30), lambda job, d: [sys.executable, '-c', TAKEDOWN_DURING_RENDER, str(d)])
    failed = jobs.get(ALICE, job['id'])
    assert (failed['status'], failed['error']) == ('failed', jobs.REMOVED) and refunded(db, job['id'])
    assert google.files == {}  # nothing reached the customer's Drive


def test_find_a_listing_leaves_out_taken_down_listings(web, db, monkeypatch):
    store.block_listing('4242', 'test')
    found = [{'id': '4242', 'url': ROOM, 'photo': PHOTO}, {'id': '4243', 'url': 'https://www.airbnb.co.uk/rooms/4243', 'photo': PHOTO}]
    monkeypatch.setattr(server.listing_search, 'search', lambda *a, **k: {'items': [dict(i) for i in found], 'count': 2})
    monkeypatch.setattr(server.listing_search, 'search_page', lambda *a, **k: {'items': [dict(i) for i in found], 'page': 1})
    res = web['alice'].get('/api/search', params={'location': 'Poole'}).json()
    assert [i['id'] for i in res['items']] == ['4243'] and res['count'] == 1
    more = web['alice'].get('/api/search/more', params={'location': 'Poole', 'page': 1}).json()
    assert [i['id'] for i in more['items']] == ['4243']


def test_outreach_rows_of_a_taken_down_listing_leave_the_page_and_the_export(web, db):
    post(web['alice'], '/api/outreach/queue', {'channel': 'cohost', 'items': [dict(p, message='Hi') for p in PROSPECTS]})
    row_ids = [r['id'] for r in store.outreach_rows(ALICE)]
    store.block_listing('222', 'test')
    store.block_listing(str(min(row_ids)), 'test')  # a listing number that equals an outreach row's own id hides nothing
    page, export = web['alice'].get('/outreach').text, web['alice'].get('/api/outreach/export.csv').text
    assert 'or-name">Sam<' not in page and 'contact_host/222' not in export
    assert 'or-name">Jo<' in page and 'or-name">Kim<' in page and 'contact_host/111' in export
    assert sorted(r['name'] for r in store.outreach_rows(ALICE)) == ['Jo', 'Kim']


def blocked_rows(db):
    with db.connect() as c:
        return c.execute('SELECT * FROM blocked_listings ORDER BY listing_id').fetchall()


def test_a_public_removal_blocks_at_once_and_lapses_unless_an_admin_confirms_it(web, drive, db):
    """Anyone can send the form, with no proof the listing is theirs: the block is immediate but provisional."""
    r = _request(web['anon'], listing_url=ROOM)
    assert 'while we check your request' in r.text
    [row] = blocked_rows(db)
    assert 29 * 86400 < row['expires_at'] - time.time() < 32 * 86400  # the one-month reply deadline
    with pytest.raises(jobs.AdmissionError):
        jobs.admit(ALICE, ROOM, {})
    card = web['admin'].get('/settings').text.split('id="blocked-card"', 1)[1].split('</section>', 1)[0]
    assert 'Waiting for a check' in card and 'blocked-confirm' in card
    with db.connect() as c:
        c.execute('UPDATE blocked_listings SET expires_at=%s', (time.time() - 1,))
    assert store.blocked_ids(['4242']) == set() and jobs.admit(ALICE, ROOM, {})['status'] == 'queued'
    assert _request(client_for(), listing_url=ROOM).status_code == 200  # a new request blocks it again
    assert store.blocked_ids(['4242']) == {'4242'}
    assert post(web['alice'], '/api/blocked-listings/confirm', {'listing_id': '4242'}).status_code == 403
    assert post(web['admin'], '/api/blocked-listings/confirm', {'listing_id': '4242'}).status_code == 200
    assert blocked_rows(db)[0]['expires_at'] is None
    activity = web['admin'].get('/settings').text.split('id="events-card"', 1)[1].split('</section>', 1)[0]
    assert 'Listing removal confirmed' in activity


def test_lapsed_removals_are_deleted_by_retention(db):
    store.block_listing('4242', 'Removal request PR-test', expires_at=time.time() - 1)
    store.block_listing('4243', 'Added by an admin')
    retention.run()
    assert [b['listing_id'] for b in blocked_rows(db)] == ['4243']


def test_public_removals_are_capped_per_email_and_per_day(web, db, monkeypatch):
    for n in (1, 2, 3):
        assert 'while we check' in _request(client_for(), listing_url=f'https://www.airbnb.co.uk/rooms/{n}').text
    r = _request(client_for(), listing_url='https://www.airbnb.co.uk/rooms/4')  # a fourth from the same address today
    assert r.status_code == 200 and 'once we have checked' in r.text
    monkeypatch.setattr(server, 'REMOVALS_PER_DAY', 4)
    r = _request(client_for(), email='someone@example.org', listing_url='https://www.airbnb.co.uk/rooms/5')
    assert 'once we have checked' in r.text  # the fifth request today, from anyone
    assert store.blocked_ids(['1', '2', '3', '4', '5']) == {'1', '2', '3'}
    with db.connect() as c:
        assert [r['listing_id'] for r in c.execute('SELECT listing_id FROM privacy_requests ORDER BY ts').fetchall()] == ['1', '2', '3', '4', '5']


# ---------------- 6. privacy notice ----------------

def test_privacy_notice_says_how_we_fetch_and_how_hosts_stop_reels(web):
    page = web['anon'].get('/privacy').text
    listing = page.split('<h2>Listing content and your videos</h2>', 1)[1].split('<h2>', 1)[0]
    hosts = page.split('<h2>If you are an Airbnb host or guest</h2>', 1)[1].split('<h2>', 1)[0]
    for phrase in ('logged out', 'limited rate', 'we pause at once', 'we stop until a person has checked', 'Remove my listing from ReelSieve'):
        assert phrase in listing, phrase
    assert 'Remove my listing from ReelSieve' in hosts and 'href="/privacy/request"' in hosts
    assert "Videos show the text, star rating and month of a guest review, but not the reviewer's name." in listing
    retention_table = page.split('How long we keep it', 1)[1].split('</table>', 1)[0]
    how_long = hosts.split('<strong>How long:</strong>', 1)[1].split('</li>', 1)[0]
    for text in (retention_table, how_long):  # the blocklist is kept until lifted, so the notice says so
        assert 'Listings a host asks us to remove' in text and 'until the host asks us to lift it' in text.lower()
