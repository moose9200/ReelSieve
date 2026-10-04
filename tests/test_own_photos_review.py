"""Review findings on 'Your own photos' (27 Sep 2026): each test failed on 5e3f0e1 and passes after its fix.

Real isolated PostgreSQL, the synthetic Google fake and stub render children, like test_own_photos.py.
"""
import asyncio
import io
import json
from pathlib import Path
import re
import tempfile
import time

import httpx
from PIL import Image
import pytest

from app import admin, auth, gdrive, jobs, photos, pipeline
from fakes import connect
from test_own_photos import FIELDS, URL, batch, drive, image, multipart, photo_job, run_worker, usage  # noqa: F401 (drive: fixture)
from test_saas_routes import client_for, csrf

N = photos.MIN_PHOTOS

INSPECT = r'''
import json, os, pathlib, sys
from PIL import Image
d = pathlib.Path(sys.argv[1])
seen = []
for p in sorted((d / 'inputs').glob('*.jpg')):
    im = Image.open(p)
    seen.append(f"{p.name}:{im.format}:{im.width}x{im.height}")
print(json.dumps({'log': 'INPUTS ' + ' '.join(seen) + ' | CV_MAX ' + str(os.environ.get('OPENCV_IO_MAX_IMAGE_PIXELS'))}), flush=True)
(d / 'reel.mp4').write_bytes(b'primary')
print(json.dumps({'result': {'video': str(d / 'reel.mp4'), 'duration': 30.0,
      'listing': {'url': None, 'title': 'Harbour Cottage', 'city': 'Whitby, UK', 'photo': None}}}), flush=True)
'''


def paid(credits=20):
    from app import store
    store.set_plan('alice@example.test', 'commercial', credits)  # the free plan allows one reel a day


def _log(job_id):
    return ' '.join(jobs.get('alice@example.test', job_id)['log'])


def _encoded(fmt, size):
    buf = io.BytesIO()
    Image.new('RGB', size, (90, 120, 150)).save(buf, fmt)
    return buf.getvalue()


# ---------------- high: the worker re-checks every photo it downloads ----------------

def test_a_photo_replaced_in_drive_after_upload_is_cleaned_again_before_rendering(drive, db):
    job = photo_job(delete_inputs=False, files=batch(N))
    first = job['params']['photos']['ids'][0]
    drive.blobs[first] = _encoded('PNG', (3000, 2000))  # 'Upload new version' in Drive: a bigger PNG under the same id
    run_worker(INSPECT)
    done = jobs.get('alice@example.test', job['id'])
    assert done['status'] == 'done', done['error']
    assert re.search(rf'p01\.jpg:JPEG:{photos.MAX_EDGE}x17\d\d ', _log(job['id']))  # re-encoded and scaled like at upload
    assert 'PNG' not in _log(job['id'])


def test_a_photo_replaced_by_another_file_type_fails_the_reel_and_refunds(drive, db):
    job = photo_job(delete_inputs=False, files=batch(N))
    drive.blobs[job['params']['photos']['ids'][1]] = _encoded('TIFF', (64, 64))
    run_worker(INSPECT)
    failed = jobs.get('alice@example.test', job['id'])
    assert failed['status'] == 'failed' and 'changed after upload' in failed['error'] and usage(db)[0]['refunded_at']
    assert 'INPUTS' not in _log(job['id'])  # the renderer never saw it


def test_the_render_child_caps_the_pixels_opencv_will_decode(drive, db):
    job = photo_job(files=batch(N))
    run_worker(INSPECT)
    assert f'CV_MAX {photos.MAX_PIXELS}' in _log(job['id'])


# ---------------- medium: the CSRF gate never spools a request body ----------------

def test_the_gate_refuses_api_posts_without_the_header_before_reading_the_body(drive, db, owners, monkeypatch):
    from starlette.requests import Request
    read = []
    original = Request._get_form
    monkeypatch.setattr(Request, '_get_form', lambda self, **kw: (read.append(kw), original(self, **kw))[1])
    data, files = multipart(N)
    data['csrf'] = auth.csrf_token(owners['alice'])
    r = client_for(owners['alice']).post('/api/jobs/photos', data=data, files=files)
    assert r.status_code == 403 and read == [] and usage(db) == [] and not drive.blobs


def test_public_form_posts_never_write_a_file_part_to_disk(owners, monkeypatch):
    import tempfile as tf
    rolled = []
    original = tf.SpooledTemporaryFile.rollover
    monkeypatch.setattr(tf.SpooledTemporaryFile, 'rollover', lambda self: (rolled.append(1), original(self))[1])
    anon = client_for()
    anon.get('/login')
    from app import server
    token = auth.csrf_token('anon:' + anon.cookies.get(server.CSRF_COOKIE))
    r = anon.post('/login', data={'csrf': token, 'user': 'x@example.test', 'password': 'y'},
                  files=[('junk', ('big.bin', b'0' * (3 * 1024 * 1024), 'application/octet-stream'))])
    assert r.status_code in (400, 413) and rolled == []
    small = [('junk', ('small.bin', b'0' * 2000, 'application/octet-stream'))]
    r = anon.post('/login', data={'csrf': token, 'user': 'x@example.test', 'password': 'y'}, files=small)
    assert r.status_code == 400  # a file part is never accepted on an HTML form, whatever its size
    # with the header the gate reads nothing; the route's own form read is bounded the same way
    r = anon.post('/login', data={'user': 'x@example.test', 'password': 'y'}, headers={'X-CSRF-Token': token},
                  files=[('junk', ('big.bin', b'0' * (3 * 1024 * 1024), 'application/octet-stream'))])
    assert r.status_code in (400, 413) and rolled == []
    r = anon.post('/login', data={'csrf': token, 'user': 'x@example.test', 'password': 'y' * (200 * 1024)})
    assert r.status_code == 413
    # an ordinary sign-in form still works
    assert anon.post('/login', data={'csrf': token, 'user': 'x@example.test', 'password': 'wrong-password'}).status_code == 401


# ---------------- medium: upload slots cannot be held by slow or repeated uploads ----------------

def _asgi_post(session, body_chunks, stall=None, headers=None, pause=0):
    """POST /api/jobs/photos through the real ASGI app; the body stops after the first chunk until `stall` is set.
    pause: seconds between later chunks (a client that trickles)."""
    from app import server
    async def gen():
        yield body_chunks[0]
        if stall is not None:
            await stall.wait()
        for c in body_chunks[1:]:
            await asyncio.sleep(pause)
            yield c
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://t') as c:
            return await c.post('/api/jobs/photos', content=gen(), headers={
                **csrf(session), 'Cookie': f'{auth.COOKIE}={session}', 'Content-Length': str(sum(map(len, body_chunks))),
                'Content-Type': 'multipart/form-data; boundary=xyz', **(headers or {})})
    return go()


def _body(n=N):
    data, files = multipart(n)
    req = httpx.Request('POST', 'http://t/', data=data, files=files)
    raw = req.read()
    boundary = req.headers['content-type'].split('boundary=')[1]
    return [raw.replace(boundary.encode(), b'xyz')]


def test_one_owner_cannot_take_a_second_upload_slot_and_others_are_not_blocked(drive, db, owners, monkeypatch):
    from app import server
    monkeypatch.setattr(server, '_photo_slots', asyncio.Semaphore(2))
    monkeypatch.setattr(server, '_uploading', set())
    body = _body()[0]
    chunks = [body[:1000], body[1000:]]

    async def scenario():
        stall = asyncio.Event()
        slow = asyncio.create_task(_asgi_post(owners['alice'], chunks, stall))
        await asyncio.sleep(0.3)
        second = await asyncio.wait_for(_asgi_post(owners['alice'], chunks), 5)
        other = await asyncio.wait_for(_asgi_post(owners['bob'], chunks), 5)  # bob has no Drive: answered at once
        stall.set()
        return second, other, await asyncio.wait_for(slow, 20)
    second, other, slow = asyncio.run(scenario())
    assert second.status_code == 429 and 'already uploading' in second.json()['detail']
    assert other.status_code == 412
    assert slow.status_code == 200, slow.text


def test_a_stalled_upload_is_dropped_and_a_full_house_answers_busy(drive, db, owners, monkeypatch):
    from app import server
    monkeypatch.setattr(server, '_photo_slots', asyncio.Semaphore(1))
    monkeypatch.setattr(server, '_uploading', set())
    monkeypatch.setattr(server, 'PHOTO_SLOT_WAIT', 0.3)
    monkeypatch.setattr(server, 'PHOTO_READ_GRACE', 1.0)
    connect(owners, drive, 'bob')  # bob passes the Drive check (made before a slot is taken) and is on another network
    body = _body()[0]
    chunks = [body[:1000], body[1000:]]

    async def scenario():
        stall = asyncio.Event()  # never set: the client stops sending
        slow = asyncio.create_task(_asgi_post(owners['alice'], chunks, stall))
        await asyncio.sleep(0.2)
        busy = await asyncio.wait_for(_asgi_post(owners['bob'], chunks, headers={'X-Forwarded-For': '198.51.100.9'}), 5)
        return busy, await asyncio.wait_for(slow, 10)
    started = time.time()
    busy, slow = asyncio.run(scenario())
    assert busy.status_code == 503 and 'busy' in busy.json()['detail']
    assert slow.status_code == 408 and time.time() - started < 8
    assert server._photo_slots._value == 1 and not server._uploading  # released


def test_admission_checks_drive_and_credits_before_decoding_any_photo(drive, db, monkeypatch):
    from app import store
    calls = []
    original = photos.clean
    monkeypatch.setattr(photos, 'clean', lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    with pytest.raises(jobs.AdmissionError) as err:
        photo_job('bob@example.test', files=batch(N))
    assert err.value.status == 412 and calls == []
    store.set_plan('alice@example.test', 'starter', 0)
    with pytest.raises(jobs.AdmissionError) as err:
        photo_job(files=batch(N))
    assert err.value.status == 402 and calls == []


# ---------------- medium: 40 photos with the page's full field set are accepted ----------------

def test_the_route_accepts_the_maximum_photos_with_every_field_the_page_sends(drive, db, owners):
    from app import store
    store.set_plan('alice@example.test', 'starter', 3)
    n = photos.MAX_PHOTOS
    data = {'title': 'Harbour Cottage', 'location': 'Whitby, UK', 'highlights': 'Hot tub', 'style': 'cinematic',
            'quote_text': ['Spotless cottage with a lovely view of the harbour.', '', ''], 'quote_stars': ['4', '', ''],
            'quotes_real': 'true', 'ai_motion': 'false', 'ai_resolution': '1080p', 'delete_inputs': 'true', 'room': ['auto'] * n}
    files = [('photos', (f's{i}.jpg', image(colour=(i * 5, 9, 9), exif=False), 'image/jpeg')) for i in range(n)]
    r = client_for(owners['alice']).post('/api/jobs/photos', data=data, files=files, headers=csrf(owners['alice']))
    assert r.status_code == 200, r.text
    assert len(jobs.get('alice@example.test', r.json()['id'])['params']['photos']['ids']) == n


# ---------------- high: the smallest accepted photo set renders, however it is labelled ----------------

LABELS = {
    'unlabelled': lambda n: ['other'] * n,
    'one of each room': lambda n: (['exterior', 'living', 'kitchen', 'bedroom', 'bathroom', 'garden', 'spa', 'view'] * 5)[:n],
    'all one room': lambda n: ['kitchen'] * n,
    'outside then one room': lambda n: ['exterior'] * 2 + ['kitchen'] * (n - 2),
    'two rooms': lambda n: (['bedroom', 'bathroom'] * 20)[:n],
}


@pytest.mark.parametrize('labels', list(LABELS))
@pytest.mark.parametrize('n', [photos.MIN_PHOTOS, photos.MIN_PHOTOS + 3, photos.MAX_PHOTOS])
def test_the_minimum_photo_count_and_up_always_makes_a_reel_that_passes_the_guards(labels, n, tmp_path):
    names = [f'p{i + 1:02d}.jpg' for i in range(n)]
    for x in names:
        (tmp_path / x).write_bytes(b'x')
    facts = {'title': 'Flat', 'location': 'Leeds', 'highlights': [], 'rooms': LABELS[labels](n),
             'quotes': [{'text': 'A lovely place to stay, very clean.', 'stars': 4}]}
    d, revs = pipeline.photo_listing(facts, names)
    m = pipeline.own_photos_manifest(pipeline.build_manifest(d, revs, tmp_path, tmp_path / 'depth', **pipeline.OWN_PHOTOS), d)
    assert pipeline.lint_manifest(m) >= 27


def test_fewer_than_the_minimum_is_refused_before_anything_is_charged(drive, db):
    with pytest.raises(jobs.AdmissionError, match=f'{photos.MIN_PHOTOS} to {photos.MAX_PHOTOS} photos'):
        photo_job(files=batch(photos.MIN_PHOTOS - 1))
    assert usage(db) == []


# ---------------- high/medium: rights statements that fit each flow ----------------

def test_a_listing_link_needs_no_tick_and_the_page_says_what_is_allowed(drive, db, owners):
    alice = client_for(owners['alice'])
    r = alice.post('/api/jobs', json={'url': URL}, headers=csrf(owners['alice']))
    assert r.status_code == 200, r.text
    assert 'attested' not in jobs.get('alice@example.test', r.json()['id'])['params']
    page = alice.get('/app').text
    assert 'id="attested"' not in page and "owner's permission to use its photos" not in page
    assert 'id="rights-note"' in page and 'private sample for its host' in page and 'href="/terms"' in page
    assert 'Not your listing?' not in page and 'took yourself, or are licensed to use' in page
    assert 'attested' not in alice.get('/static/app.js').text


def test_terms_cover_other_peoples_listings_uploaded_photos_and_guest_quotes(owners):
    terms = client_for().get('/terms').text
    assert 'private sample for its host' in terms and 'publish' in terms
    assert 'real reviews from guests who stayed' in terms and 'word for word' in terms


# ---------------- medium: typed guest quotes are real reviews, shown at the rating given ----------------

def test_guest_quotes_need_the_real_review_confirmation_which_is_kept_with_the_job(drive, db):
    paid()
    with pytest.raises(jobs.AdmissionError, match='real reviews'):
        photo_job(files=batch(N), quotes_real=False)
    job = photo_job(files=batch(N), quotes_real=True)
    assert job['params']['quotes'] and time.time() - job['params']['quotes_confirmed_at'] < 60
    none = photo_job(files=batch(N + 1), quotes=[], key='no-quotes')
    assert 'quotes_confirmed_at' not in none['params']  # nothing to confirm without a quote
    from app import store
    exported = [j['params'] for j in store.export('alice@example.test')['jobs']]
    assert any(p.get('quotes_confirmed_at') for p in exported)


def test_the_quote_form_has_no_default_rating_and_asks_for_the_confirmation(drive, owners):
    page = client_for(owners['alice']).get('/app').text
    for sel in re.findall(r'<select name="quote_stars".*?</select>', page, re.S):
        assert re.search(r'<option value=""[^>]*selected', sel) and '<option value="5" selected' not in sel
    assert 'id="quotes_real"' in page and 'real reviews from guests who stayed, quoted word for word' in page
    js = client_for(owners['alice']).get('/static/app.js').text
    assert 'quotes_real' in js


def test_the_reel_shows_the_first_typed_quote_whatever_its_rating(tmp_path):
    names = [f'p{i + 1:02d}.jpg' for i in range(N)]
    for x in names:
        (tmp_path / x).write_bytes(b'x')
    quotes = [{'text': 'Comfortable beds, but the kitchen was small.', 'stars': 3},
              {'text': 'Spotless cottage with a lovely view of the harbour.', 'stars': 5}]
    d, revs = pipeline.photo_listing({'title': 'Flat', 'location': 'Leeds', 'highlights': [], 'rooms': ['other'] * N,
                                      'quotes': quotes}, names)
    m = pipeline.own_photos_manifest(pipeline.build_manifest(d, revs, tmp_path, tmp_path / 'depth', **pipeline.OWN_PHOTOS), d)
    assert m['reviews']['items'][0]['stars'] == 3 and m['reviews']['items'][0]['text'].startswith('Comfortable beds')


# ---------------- high/medium: the privacy notice and the in-form notices match what happens ----------------

def test_privacy_notice_covers_uploaded_photos_typed_details_drive_use_and_higgsfield(owners):
    page = client_for().get('/privacy').text
    assert 'Last updated 04 Oct 2026' in page
    assert 'Photos you upload' in page and 'Inputs folder' in page and 'scratch copies' in page
    assert 'Title, location, highlights and guest quotes you type' in page and '30 days' in page
    assert 'reads them back' in page and 'deletes them' in page  # drive.file: now also inputs, not only finished videos
    assert "receives a listing's photos only if you switch on AI camera motion for a listing-link reel" in page
    assert 'Photos you upload for a reel are never sent to Higgsfield' in page and 'train and improve its AI models' in page
    assert 'If a customer quotes your review' in page  # guests whose words a customer types in


def test_photo_form_says_higgsfield_keeps_the_photos_and_may_train_on_them(drive, owners):
    page = client_for(owners['alice']).get('/app').text
    assert 'Higgsfield keeps them and may use them to train its AI models' in page
    assert 'ReelSieve keeps no copy.' not in page  # untrue once AI camera motion is on


def test_public_copy_does_not_promise_rating_or_real_review_cards_on_photo_reels(owners):
    web = client_for()
    llms, landing = web.get('/llms.txt').text, web.get('/').text
    assert 'Each video has an intro, a rating card' not in llms and 'Photo reels' in llms
    assert f'{photos.MIN_PHOTOS} to {photos.MAX_PHOTOS} photos' in llms and '6 to 40' not in landing + llms
    assert "with the guest's name" not in landing and 'It ends with a real five-star guest review' not in landing


# ---------------- low: erasure and deactivation delete the photos the customer asked us to delete ----------------

def test_erasing_an_account_first_deletes_photos_it_asked_to_have_deleted(drive, db):
    paid()
    photo_job(files=batch(N))
    kept = photo_job(files=batch(N + 1), delete_inputs=False, key='keep')
    assert len(drive.blobs) == 2 * N + 1
    admin.erase('alice@example.test', by='alice@example.test')
    assert len(drive.blobs) == N + 1 and all(i in drive.blobs for i in kept['params']['photos']['ids'])


def test_deactivating_an_account_deletes_them_too_and_records_it(drive, db):
    job = photo_job(files=batch(N))
    admin.deactivate('alice@example.test')
    assert not drive.blobs
    with db.connect() as c:
        assert c.execute('SELECT meta FROM jobs WHERE id=%s', (job['id'],)).fetchone()['meta']['inputs'] == 'deleted'


# ---------------- low/medium: after a reconnect the app folder is never an Inputs folder ----------------

def test_after_reconnecting_reels_and_inputs_still_land_in_the_app_folder(drive, db, owners):
    from app import database
    paid()
    photo_job(files=batch(N), delete_inputs=False)
    root = drive.app_folder()
    with database.connect() as c:
        gdrive.disconnect_owner(database.user_id('alice@example.test', c))
    connect(owners, drive)
    job = photo_job(files=batch(N + 1), delete_inputs=False, key='after')
    inputs = drive.folders[job['params']['photos']['folder']]['parents'][0]
    assert drive.folders[inputs]['parents'] == [root]
    run_worker(INSPECT)  # the older job first
    run_worker(INSPECT)
    rec = gdrive.receipt('alice@example.test', job['id'])
    assert drive.metas[rec['id']]['parents'] == [root]


# ---------------- low: 'Delete these photos' deletes those photos, recoverably for anything else ----------------

def test_deleting_inputs_removes_only_our_photos_and_bins_the_folder(drive, db):
    job = photo_job(files=batch(N))
    folder, ids = job['params']['photos']['folder'], job['params']['photos']['ids']
    drive.metas['users-own-file'] = {'id': 'users-own-file', 'name': 'notes.txt', 'parents': [folder]}
    run_worker(INSPECT)
    assert not set(ids) & set(drive.blobs) and 'users-own-file' in drive.metas
    assert drive.folders[folder].get('trashed') is True
    assert jobs.get('alice@example.test', job['id'])['meta']['inputs'] == 'deleted'


# ---------------- low: small robustness fixes ----------------

def _bad_exif_jpeg():
    img = Image.new('RGB', (64, 40), (180, 120, 60))
    e = Image.Exif()
    e[0x0112], e[0x010F] = 6, 'SecretCam'
    gps = e.get_ifd(0x8825)
    gps[1], gps[2] = 'N', (51.0, 30.0, 12.0)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', exif=e)
    raw = bytearray(buf.getvalue())
    for i, v in [(141, 225), (129, 93), (107, 2)]:  # found by fuzzing the EXIF block; Pillow then raises TypeError
        raw[i] = v
    return bytes(raw)


def test_a_photo_with_malformed_exif_is_accepted_without_its_metadata(drive, db, owners):
    out = photos.clean(_bad_exif_jpeg(), 'odd.jpg')
    assert out[:3] == b'\xff\xd8\xff' and b'SecretCam' not in out
    data, files = multipart(N - 1)
    files.append(('photos', ('odd.jpg', _bad_exif_jpeg(), 'image/jpeg')))
    data.update(room=['auto'] * N, quotes_real='true')
    r = client_for(owners['alice']).post('/api/jobs/photos', data=data, files=files, headers=csrf(owners['alice']))
    assert r.status_code == 200, r.text


def test_unknown_guest_count_never_prints_none_in_a_caption(tmp_path):
    rooms = ['exterior'] * 3 + ['living', 'kitchen', 'bedroom', 'bathroom', 'garden', 'other']
    names = [f'p{i + 1:02d}.jpg' for i in range(len(rooms))]
    for x in names:
        (tmp_path / x).write_bytes(b'x')
    d, revs = pipeline.photo_listing({'title': 'Flat', 'location': 'Whitby, UK', 'highlights': [], 'quotes': [], 'rooms': rooms}, names)
    m = pipeline.own_photos_manifest(pipeline.build_manifest(d, revs, tmp_path, tmp_path / 'depth', **pipeline.OWN_PHOTOS), d)
    subs = [s['subtitle'] for s in m['scenes']]
    assert not [s for s in subs if 'None' in s or s.rstrip().endswith('/')], subs


def test_photo_reel_file_names_keep_hash_and_question_marks_in_the_title():
    assert gdrive.safe_name('Casa #5 Seaview?') == 'Casa #5 Seaview?.mp4'
    assert gdrive.safe_name('https://www.airbnb.co.uk/rooms/1?check_in=2026#x') == 'https://www.airbnb.co.uk/rooms/1.mp4'


def test_job_page_counts_extra_polls_only_after_the_job_has_finished():
    js = (Path(__file__).resolve().parents[1] / 'app' / 'static' / 'app.js').read_text()
    line = next(x for x in js.splitlines() if 'extraPolls++' in x)
    assert re.search(r'terminal\s*&&.*extraPolls\+\+', line), line
