"""'Your own photos' reels and the ownership confirmation for listing links.

Real isolated PostgreSQL, the synthetic Google fake (tests/fakes.py) and stub render children:
nothing here fetches Airbnb, calls a paid provider or sends a message.
"""
import io
import time

from PIL import Image, PngImagePlugin
import pytest

from app import gdrive, jobs, photos
from fakes import connect
from test_saas_routes import client_for, csrf

URL = 'https://www.airbnb.co.uk/rooms/4242'


@pytest.fixture
def drive(owners, google, monkeypatch, tmp_path):
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    connect(owners, google)
    return google


def usage(db):
    with db.connect() as c:
        return c.execute('SELECT job_id,kind,credits,debited,refunded_at FROM usage ORDER BY ts').fetchall()


# ---------------- listing links: ownership confirmation ----------------

def test_listing_link_job_needs_the_ownership_confirmation(drive, db):
    with pytest.raises(jobs.AdmissionError) as err:
        jobs.admit('alice@example.test', URL, {})
    assert err.value.status == 400 and 'my listing' in str(err.value) and "owner's permission" in str(err.value)
    assert usage(db) == []


def test_confirmed_listing_link_job_stores_the_confirmation_and_its_time(drive, db):
    before = time.time()
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    assert job['params']['attested'] is True and before <= job['params']['attested_at'] <= time.time()
    # the time is not part of the request: a retry with the same key is still the same reel
    again = jobs.admit('alice@example.test', URL, {'attested': True}, job['idempotency_key'])
    assert again['id'] == job['id']


def test_listing_link_route_refuses_without_confirmation_and_the_page_asks_for_it(drive, db, owners):
    alice = client_for(owners['alice'])
    r = alice.post('/api/jobs', json={'url': URL}, headers=csrf(owners['alice']))
    assert r.status_code == 400 and "owner's permission" in r.json()['detail'] and usage(db) == []
    r = alice.post('/api/jobs', json={'url': URL, 'attested': True}, headers=csrf(owners['alice']))
    assert r.status_code == 200 and jobs.get('alice@example.test', r.json()['id'])['params']['attested'] is True
    page = alice.get('/app').text
    assert 'id="attested"' in page and 'This is my listing, or I have the owner\'s permission to use its photos' in page
    assert 'attested: ' in alice.get('/static/app.js').text


# ---------------- uploaded photos: type by content, sizes, count, metadata stripped ----------------

def image(fmt='JPEG', size=(64, 40), colour=(180, 120, 60), exif=True):
    """A synthetic photo carrying the metadata a phone adds: camera make, GPS position, rotation, a comment."""
    img = Image.new('RGBA' if fmt == 'PNG' else 'RGB', size, colour)
    opts = {}
    if exif:
        e = Image.Exif()
        e[0x0112] = 6  # rotate 90 degrees clockwise when shown
        e[0x010F] = 'SecretCam'
        gps = e.get_ifd(0x8825)
        gps[1], gps[2] = 'N', (51.0, 30.0, 12.0)
        opts['exif'] = e
        if fmt == 'JPEG':
            opts['comment'] = b'secret-comment'
        if fmt == 'PNG':
            meta = PngImagePlugin.PngInfo()
            meta.add_text('Author', 'Secret Person')
            opts['pnginfo'] = meta
    buf = io.BytesIO()
    img.save(buf, fmt, **opts)
    return buf.getvalue()


def batch(n=6, fmt='JPEG'):
    return [(f'room-{i}.jpg', image(fmt)) for i in range(n)]


@pytest.mark.parametrize('fmt', ['JPEG', 'PNG', 'WEBP'])
def test_real_image_type_comes_from_the_bytes_not_the_name(fmt):
    out = photos.clean(image(fmt), 'holiday.gif')  # misleading extension, real JPEG/PNG/WebP inside
    assert out[:3] == b'\xff\xd8\xff'  # always stored as a clean JPEG


@pytest.mark.parametrize('data', [b'GIF89a' + b'\0' * 64, b'<svg xmlns="http://www.w3.org/2000/svg"/>', b'%PDF-1.7',
                                  b'\0\0\0\x18ftypheic' + b'\0' * 64, b'just some text'])
def test_other_file_types_are_refused_whatever_they_are_called(data):
    with pytest.raises(photos.PhotoError, match='not a JPEG, PNG or WebP'):
        photos.clean(data, 'kitchen.jpg')


def test_a_file_that_only_starts_like_a_jpeg_is_refused():
    with pytest.raises(photos.PhotoError, match='could not be read'):
        photos.clean(b'\xff\xd8\xff\xe0' + b'not really an image' * 50, 'kitchen.jpg')


@pytest.mark.parametrize('fmt', ['JPEG', 'PNG', 'WEBP'])
def test_exif_gps_rotation_and_comments_are_stripped_and_rotation_applied(fmt):
    raw = image(fmt, size=(64, 40))
    assert b'SecretCam' in raw
    out = photos.clean(raw, 'x.jpg')
    for secret in (b'SecretCam', b'secret-comment', b'Secret Person', b'Exif'):
        assert secret not in out, secret
    im = Image.open(io.BytesIO(out))
    assert im.format == 'JPEG' and not im.getexif() and not im.getexif().get_ifd(0x8825)
    assert not {'comment', 'icc_profile', 'exif', 'xmp'} & set(im.info)
    assert im.size == (40, 64)  # the rotation the phone asked for is applied before the tag goes


def test_very_large_photos_are_scaled_down_and_pixel_bombs_refused(monkeypatch):
    out = photos.clean(image(size=(3000, 1000), exif=False), 'wide.jpg')
    assert Image.open(io.BytesIO(out)).size == (photos.MAX_EDGE, 853)
    monkeypatch.setattr(photos, 'MAX_PIXELS', 1000)
    with pytest.raises(photos.PhotoError, match='too many pixels'):
        photos.clean(image(size=(64, 40), exif=False), 'bomb.png')


def test_upload_count_and_size_limits(monkeypatch):
    photos.check_batch(batch(6))
    photos.check_batch(batch(40))
    for n in (0, 5, 41):
        with pytest.raises(photos.PhotoError, match='6 to 40 photos'):
            photos.check_batch(batch(n))
    big = [('huge.jpg', b'\xff\xd8\xff' + b'0' * photos.MAX_BYTES)] + batch(5)
    with pytest.raises(photos.PhotoError, match='huge.jpg is larger than 15 MB'):
        photos.check_batch(big)
    monkeypatch.setattr(photos, 'MAX_TOTAL', 6 * len(image()) - 1)
    with pytest.raises(photos.PhotoError, match='250 MB'):
        photos.check_batch(batch(6))


def test_typed_details_are_validated_and_quotes_carry_no_name():
    good = photos.details({'title': '  Sea View Cottage ', 'location': 'Whitby, UK', 'highlights': 'Hot tub, Sea view\nFree parking, , ',
                           'quotes': [{'text': 'Spotless cottage with a lovely view of the harbour.', 'stars': '5', 'name': 'Jane Guest'},
                                      {'text': '', 'stars': 5}]})
    assert good == {'title': 'Sea View Cottage', 'location': 'Whitby, UK', 'highlights': ['Hot tub', 'Sea view', 'Free parking'],
                    'quotes': [{'text': 'Spotless cottage with a lovely view of the harbour.', 'stars': 5}]}
    fine = {'text': 'A perfectly fine quote here.', 'stars': 5}
    for bad, msg in [({'location': 'Whitby'}, 'property title'), ({'title': 'Cottage'}, 'location'),
                     ({'title': 'x' * 81, 'location': 'Whitby'}, 'property title'),
                     ({'title': 'C', 'location': 'W', 'quotes': [{'text': 'Too short', 'stars': 5}]}, '20'),
                     ({'title': 'C', 'location': 'W', 'quotes': [{**fine, 'stars': 9}]}, 'stars'),
                     ({'title': 'C', 'location': 'W', 'quotes': [fine] * 4}, '3 guest quotes')]:
        with pytest.raises(photos.PhotoError, match=msg):
            photos.details(bad)


def test_room_comes_from_the_choice_or_the_file_name():
    assert photos.room_of('kitchen', 'IMG_1.jpg') == 'kitchen'
    assert photos.room_of('auto', 'Living_Room-02.JPG') == 'living'
    assert photos.room_of('auto', 'master-bedroom.webp') == 'bedroom'
    assert photos.room_of('nonsense', 'IMG_1234.jpg') == 'other'


# ---------------- the customer's Drive holds the inputs; we keep ids only ----------------

def generation(user='alice@example.test'):
    from app import database
    with database.connect() as c:
        return gdrive.usable_generation(c, database.user_id(user, c))


def test_inputs_go_to_a_job_folder_in_the_customers_drive_and_come_back_intact(drive, tmp_path):
    shots = [photos.clean(image(colour=(i * 40, 10, 10)), f'{i}.jpg') for i in range(3)]
    folder, ids = gdrive.upload_inputs('alice@example.test', 'abc123def456', shots, generation())
    job_folder = drive.folders[folder]
    inputs = drive.folders[job_folder['parents'][0]]
    assert job_folder['name'] == 'abc123def456' and inputs['name'] == 'Inputs'
    assert inputs['parents'] == [drive.app_folder()]
    for i, fid in enumerate(ids):
        meta = drive.metas[fid]
        assert meta['parents'] == [folder] and meta['mimeType'] == 'image/jpeg' and meta['name'] == f'photo-{i + 1:02d}.jpg'
        assert meta['appProperties']['job'] == 'abc123def456'
    # a second reel reuses the one Inputs folder
    folder2, _ = gdrive.upload_inputs('alice@example.test', 'fff000fff000', shots[:1], generation())
    assert drive.folders[folder2]['parents'] == job_folder['parents']
    got = gdrive.download_inputs('alice@example.test', ids, tmp_path / 'in', generation())
    assert [p.name for p in got] == ['p01.jpg', 'p02.jpg', 'p03.jpg'] and [p.read_bytes() for p in got] == shots
    gdrive.delete_inputs('alice@example.test', folder, generation())
    assert folder not in drive.folders and not set(ids) & set(drive.blobs)
    gdrive.delete_inputs('alice@example.test', folder, generation())  # already gone: nothing to do


def test_a_failed_input_upload_leaves_nothing_behind(drive):
    shots = [photos.clean(image(), 'a.jpg')] * 3
    drive.fail_upload_after = 1
    with pytest.raises(RuntimeError, match='Google Drive'):
        gdrive.upload_inputs('alice@example.test', 'abc123def456', shots, generation())
    assert not [f for f in drive.folders.values() if f['name'] == 'abc123def456'] and not drive.blobs


def test_inputs_stay_with_the_google_account_the_job_was_admitted_on(drive, tmp_path):
    with pytest.raises(RuntimeError, match='reconnected'):
        gdrive.upload_inputs('alice@example.test', 'abc123def456', [b'x'], generation() + 1)
    with pytest.raises(RuntimeError, match='reconnected'):
        gdrive.download_inputs('alice@example.test', ['some-id'], tmp_path, generation() + 1)


# ---------------- admission: photos go to Drive first, the job keeps ids and typed facts ----------------

FIELDS = {'title': 'Harbour Cottage', 'location': 'Whitby, UK', 'highlights': 'Hot tub, Sea view',
          'quotes': [{'text': 'Spotless cottage with a lovely view of the harbour.', 'stars': 5}],
          'rooms': ['exterior', 'living', 'kitchen', 'bedroom', 'bathroom', 'auto'], 'style': 'tutorial'}


def photo_job(user='alice@example.test', files=None, key=None, **fields):
    return jobs.admit_photos(user, {**FIELDS, **fields}, files or batch(6), key)


def job_folders(google):
    return [f for f in google.folders.values() if 'job' in f['appProperties']]


def test_photo_admission_uploads_to_the_owners_drive_and_the_job_keeps_ids_only(drive, db):
    job = photo_job()
    p = job['params']
    assert p['source'] == 'photos' and job['url'].startswith('photos:') and job['status'] == 'queued'
    assert drive.folders[p['photos']['folder']]['name'] == job['id']
    assert len(p['photos']['ids']) == 6 and all(drive.metas[i]['parents'] == [p['photos']['folder']] for i in p['photos']['ids'])
    assert (p['title'], p['location'], p['highlights']) == ('Harbour Cottage', 'Whitby, UK', ['Hot tub', 'Sea view'])
    assert p['quotes'] == [{'text': 'Spotless cottage with a lovely view of the harbour.', 'stars': 5}]
    assert p['rooms'] == ['exterior', 'living', 'kitchen', 'bedroom', 'bathroom', 'other'] and p['style'] == 'v3'
    assert p['delete_inputs'] is True and 'attested' not in p
    stored = str(dict(job))
    assert 'room-0.jpg' not in stored and 'SecretCam' not in stored and len(stored) < 4000  # ids and facts, never photo bytes
    assert not any(b'SecretCam' in b or b'Exif' in b for b in drive.blobs.values())  # Drive only ever gets clean photos
    assert [(u['kind'], u['credits']) for u in usage(db)] == [('video', 1)]


def test_a_photo_reel_costs_one_video_exactly_like_a_link_reel(drive, db):
    from app import plans, store
    store.set_plan('alice@example.test', 'starter', 3)
    credits = lambda: plans.account_view('alice@example.test')['remaining']  # noqa: E731
    jobs.admit('alice@example.test', URL, {'attested': True})
    assert credits() == 2
    job = photo_job(key='p1')
    assert credits() == 1 and [(u['kind'], u['credits'], u['debited']) for u in usage(db)] == [('video', 1, True)] * 2
    # the same request again (a retried submit) is the same reel: no second charge, no second upload
    assert photo_job(key='p1')['id'] == job['id'] and credits() == 1 and len(job_folders(drive)) == 1
    with pytest.raises(jobs.AdmissionError) as err:
        photo_job(key='p1', title='Another Cottage')
    assert err.value.status == 409 and len(job_folders(drive)) == 1
    assert jobs.cancel('alice@example.test', job['id'])['status'] == 'cancelled' and credits() == 2
    photo_job(key='p2', files=batch(7))
    photo_job(key='p3', files=batch(8))
    assert credits() == 0
    with pytest.raises(jobs.AdmissionError) as err:
        photo_job(key='p4', files=batch(9))
    assert err.value.status == 402 and len(job_folders(drive)) == 3  # refused before anything was uploaded


def test_photo_reel_needs_drive_and_valid_photos_before_any_charge_or_upload(drive, db):
    with pytest.raises(jobs.AdmissionError) as err:
        photo_job('bob@example.test')
    assert err.value.status == 412
    for files, msg in [(batch(5), '6 to 40'), (batch(5) + [('notes.jpg', b'plain text')], 'not a JPEG')]:
        with pytest.raises(jobs.AdmissionError, match=msg) as err:
            photo_job(files=files)
        assert err.value.status == 400
    with pytest.raises(jobs.AdmissionError, match='property title'):
        photo_job(title='')
    assert usage(db) == [] and not drive.folders.get('folder-inputs-x') and not drive.blobs


def test_a_failed_drive_upload_charges_nothing(drive, db):
    drive.fail_upload_after = 2
    with pytest.raises(jobs.AdmissionError, match='Google Drive') as err:
        photo_job()
    assert err.value.status == 502 and usage(db) == [] and not job_folders(drive) and not drive.blobs
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'] == 0


def multipart(n=6, **over):
    data = {'title': FIELDS['title'], 'location': FIELDS['location'], 'highlights': FIELDS['highlights'],
            'style': 'cinematic', 'quote_text': [FIELDS['quotes'][0]['text'], ''], 'quote_stars': ['5', '5'],
            'room': ['auto'] * n, 'delete_inputs': 'false', **over}
    return data, [('photos', (f'shot-{i}.png', image('PNG'), 'image/png')) for i in range(n)]


def test_photo_route_admits_a_multipart_upload(drive, db, owners):
    alice = client_for(owners['alice'])
    data, files = multipart()
    assert alice.post('/api/jobs/photos', data=data, files=files).status_code == 403  # CSRF like every mutation
    r = alice.post('/api/jobs/photos', data=data, files=files, headers={**csrf(owners['alice']), 'Idempotency-Key': 'r1'})
    assert r.status_code == 200, r.text
    job = jobs.get('alice@example.test', r.json()['id'])
    assert job['params']['delete_inputs'] is False and len(job['params']['photos']['ids']) == 6
    assert job['params']['quotes'] == FIELDS['quotes'] and job['params']['style'] == 'v2'
    data, files = multipart(5)
    r = alice.post('/api/jobs/photos', data=data, files=files, headers=csrf(owners['alice']))
    assert r.status_code == 400 and '6 to 40 photos' in r.json()['detail']


def test_photo_route_stops_reading_past_the_size_limit(drive, db, owners, monkeypatch):
    from app import server
    monkeypatch.setattr(server, 'PHOTO_BODY_MAX', 2000)
    data, files = multipart()
    r = client_for(owners['alice']).post('/api/jobs/photos', data=data, files=files, headers=csrf(owners['alice']))
    assert r.status_code == 413 and '250 MB' in r.json()['detail'] and usage(db) == [] and not drive.blobs
