"""Final pre-deploy review findings (27 Sep 2026): each test failed on 3105f8e and passes after its fix.

Real isolated PostgreSQL, the synthetic Google fake and stub render children, like test_own_photos.py.
"""
import json
import re

from app import jobs, photos
from fakes import connect  # noqa: F401
from test_own_photos import FIELDS, URL, batch, drive, multipart, photo_job, usage  # noqa: F401 (drive: fixture)
from test_saas_routes import client_for, csrf

ALICE, BOB = 'alice@example.test', 'bob@example.test'
N = photos.MIN_PHOTOS


def paid(user=ALICE, credits=20):
    from app import store
    store.set_plan(user, 'commercial', credits)


# ---------------- N1: photos read back from a customer's Drive never go to Higgsfield ----------------

def test_own_photo_reels_never_get_ai_motion_whatever_the_client_sends(drive, db, owners, monkeypatch):
    monkeypatch.setenv('HF_KEY', 'synthetic-hf-key')
    paid()
    assert photo_job(ai_motion=True)['params']['ai_motion'] is False
    data, files = multipart(N + 1, ai_motion='true')
    r = client_for(owners['alice']).post('/api/jobs/photos', data=data, files=files, headers=csrf(owners['alice']))
    assert r.status_code == 200, r.text
    assert jobs.get(ALICE, r.json()['id'])['params']['ai_motion'] is False
    assert jobs.admit(ALICE, URL, {'ai_motion': True})['params']['ai_motion'] is True  # listing-link reels keep it


def test_the_worker_never_asks_for_ai_motion_on_an_own_photo_reel_admitted_before_the_fix(drive, db, tmp_path):
    from app import worker
    paid()
    job = photo_job()
    with db.connect() as c:  # a photo reel admitted by the old code with AI motion on
        c.execute("UPDATE jobs SET params=params || '{\"ai_motion\": true}'::jsonb WHERE id=%s", (job['id'],))
    spec = json.loads(worker.render_command(jobs.claim('w', 30), tmp_path)[4])
    assert spec['photos'] and spec['ai_motion'] is False


def test_photos_mode_offers_no_ai_motion_and_says_why(drive, db, owners, monkeypatch):
    monkeypatch.setenv('HF_KEY', 'synthetic-hf-key')
    paid()
    page = client_for(owners['alice']).get('/app?mode=photos').text
    form = page.split('id="photos-form"', 1)[1].split('</form>', 1)[0]
    assert re.search(r'<input type="checkbox" id="p-ai_motion"[^>]*disabled', form)
    assert 'AI camera motion is only for listing-link reels' in form and 'Higgsfield' not in form
    link_form = page.split('id="reel-form"', 1)[1].split('</form>', 1)[0]
    assert not re.search(r'<input type="checkbox" id="ai_motion"[^>]*disabled', link_form)  # paid plan, HF configured
    notice = client_for().get('/privacy').text
    assert 'read back from your Drive are sent to Higgsfield' not in notice
    assert 'Photos you upload for a reel are never sent to Higgsfield' in notice


# ---------------- N21: rotating SESSION_SECRET never voids an objection ----------------

def _legacy_key(secret):
    import hashlib
    import hmac
    return hmac.new(secret.encode(), b'reelsieve:outreach-suppression:v1', hashlib.sha256).digest()


def test_the_do_not_contact_key_is_made_once_as_before_stored_encrypted_and_survives_a_new_session_secret(owners, db, monkeypatch):
    import hashlib
    import hmac
    import os
    from app import gdrive, store
    store._suppression_keys.clear()
    old = _legacy_key(os.environ['SESSION_SECRET'])
    with db.connect() as c:  # an objection recorded by the code before this fix
        c.execute('INSERT INTO outreach_suppressions(key,ts) VALUES(%s,1)',
                  (hmac.new(old, b'company:00000001', hashlib.sha256).hexdigest(),))
    with client_for():  # start-up freezes the key
        pass
    with db.connect() as c:
        row = c.execute("SELECT value FROM app_meta WHERE key='outreach_suppression_key'").fetchone()
    assert row and old.hex() not in json.dumps(row['value']) and gdrive._fernet().decrypt(row['value']['enc'].encode()) == old
    monkeypatch.setenv('SESSION_SECRET', 'a-rotated-synthetic-session-secret-only')
    store._suppression_keys.clear()  # a new process after the rotation
    assert store._suppression_key() == old
    assert store.unsuppressed([{'company_number': '00000001'}, {'company_number': '00000002'}]) == [{'company_number': '00000002'}]
    store.suppress({'airbnb_profile': 'https://www.airbnb.co.uk/users/show/55'})
    assert store.unsuppressed([{'airbnb_profile': 'https://www.airbnb.co.uk/users/show/55'}]) == []


def test_an_undecryptable_stored_key_stops_the_app_instead_of_making_a_new_one(owners, db, monkeypatch):
    import pytest
    from cryptography.fernet import Fernet
    from app import store
    store._suppression_keys.clear()
    store._suppression_key()
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())  # rotated without keeping the old key
    store._suppression_keys.clear()
    with pytest.raises(RuntimeError, match='TOKEN_ENCRYPTION_OLD_KEYS'):
        store._suppression_key()


# ---------------- S1: privacy-form objections are limited per network, tagged, visible and undoable ----------------

from test_privacy_rights import _request, post, web  # noqa: E402,F401 (web: fixture)

PROFILE = 'https://www.airbnb.co.uk/users/show/31337'


def test_the_privacy_form_limit_counts_a_whole_network_not_one_address(web):
    ipv6 = lambda n: {'X-Forwarded-For': f'2001:db8:1:2::{n:x}'}  # noqa: E731  one /64, a new address each time
    for n in range(1, 6):
        r = web['anon'].post('/privacy/request', data=_form(web['anon'], type='access'), headers=ipv6(n))
        assert r.status_code == 200, n
    assert web['anon'].post('/privacy/request', data=_form(web['anon'], type='access'), headers=ipv6(0xffff)).status_code == 429
    other = {'X-Forwarded-For': '2001:db8:1:3::1'}  # the next /64 is another network
    assert web['anon'].post('/privacy/request', data=_form(web['anon'], type='access'), headers=other).status_code == 200


def _form(client, **fields):
    page = client.get('/privacy/request').text
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    return {'csrf': token, 'name': 'Pat Host', 'email': 'pat@example.org', 'type': 'objection',
            'details': 'Please stop.', 'airbnb_profile': '', **fields}


def _marks(db):
    with db.connect() as c:
        return c.execute('SELECT * FROM outreach_suppressions ORDER BY ts').fetchall()


def test_form_objections_carry_the_request_ref_show_in_settings_and_an_admin_can_undo_them(web, db):
    from app import store
    store.suppress({'company_number': '00000009'}, ALICE)  # a user's own mark, made earlier: never undone by a request
    r = _request(web['anon'], airbnb_profile=PROFILE, company_number='00000009')
    ref = re.search(r'PR-\d{6}-[0-9A-F]{6}', r.text).group(0)
    other = re.search(r'PR-\d{6}-[0-9A-F]{6}', _request(client_for(), airbnb_profile=PROFILE.replace('31337', '42')).text).group(0)
    tagged = [m for m in _marks(db) if m['request_ref'] == ref]
    assert len(tagged) == 1 and tagged[0]['owner_id'] is None  # the profile; the company was already on the list
    assert store.unsuppressed([{'airbnb_profile': PROFILE}]) == []  # honoured at once
    settings = web['admin'].get('/settings').text.split('id="requests-card"', 1)[1].split('</section>', 1)[0]
    row = settings.split(f'data-request="{ref}"', 1)[1].split('</li>', 1)[0]
    assert '1 do-not-contact entry' in row and f'class="btn btn-secondary btn-sm req-unsuppress" data-ref="{ref}"' in row
    assert post(web['alice'], '/api/privacy-requests/unsuppress', {'ref': ref}).status_code == 403
    assert post(web['admin'], '/api/privacy-requests/handled', {'ref': ref}).status_code == 200
    settings = web['admin'].get('/settings').text.split('id="requests-card"', 1)[1].split('</section>', 1)[0]
    assert f'data-ref="{ref}"' in settings  # still visible and undoable once the request is closed
    r = post(web['admin'], '/api/privacy-requests/unsuppress', {'ref': ref})
    assert r.status_code == 200 and r.json()['removed'] == 1
    assert store.unsuppressed([{'airbnb_profile': PROFILE}]) == [{'airbnb_profile': PROFILE}]
    assert store.unsuppressed([{'company_number': '00000009'}]) == []  # the user's mark stays
    assert [m['request_ref'] for m in _marks(db) if m['request_ref']] == [other]  # the other request is untouched
    events = [(e['action'], e['actor'], e['detail']) for e in store.admin_events()]
    assert ('request_unsuppress', 'operator@example.test', {'ref': ref, 'rows': 1}) in events
    assert post(web['admin'], '/api/privacy-requests/unsuppress', {'ref': ref}).status_code == 404
    assert 'Do-not-contact from a privacy request undone' in web['admin'].get('/settings').text


# ---------------- S3: erasure keeps a keyed hash of the email so unsuppress still works ----------------

def test_marks_of_an_erased_account_can_still_be_undone_by_its_email(owners, db):
    import time
    from app import admin, retention, store
    store.suppress({'company_number': '00000001'}, ALICE)
    store.suppress({'company_number': '00000002'})  # an objection through the form: no account
    admin.erase(ALICE)
    marks = _marks(db)
    assert [m['owner_id'] for m in marks] == [None, None] and ALICE not in json.dumps(marks)
    assert marks[0]['owner_hash'] and marks[1]['owner_hash'] is None
    assert admin.main(['unsuppress', ALICE]) == 0
    assert store.unsuppressed([{'company_number': '00000001'}]) == [{'company_number': '00000001'}]
    assert store.unsuppressed([{'company_number': '00000002'}]) == []
    assert ('unsuppress', {'rows': 1}) in [(e['action'], e['detail']) for e in store.admin_events()]
    assert ALICE not in json.dumps([e['detail'] for e in store.admin_events()], default=str)
    store.suppress({'company_number': '00000003'}, BOB)
    admin.erase(BOB)
    retention.run(now=time.time() + 91 * 86400)
    assert all(m['owner_hash'] is None and m['owner_id'] is None for m in _marks(db))  # the link goes after 90 days


# ---------------- N2: a host who objected is not offered as someone to contact on a reel's page ----------------

HOSTED = r'''
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / 'a.mp4').write_bytes(b'primary'); (d / 'b.mp4').write_bytes(b'small')
print(json.dumps({'result': {'video': str(d / 'a.mp4'), 'video_720': str(d / 'b.mp4'), 'duration': 31,
      'listing': {'url': 'https://www.airbnb.co.uk/rooms/4242', 'title': 'Sea view flat', 'city': 'Brighton', 'host': 'Sam',
                  'host_id': '777'}}}), flush=True)
'''


def _hosted_reel(owners, google):
    import sys
    from app import worker
    connect(owners, google)
    job = jobs.admit(ALICE, URL, {'message': 'Hi {host_name}, I made a reel of {listing_title}.'})
    worker.process(jobs.claim('w', 30), lambda j, d: [sys.executable, '-c', HOSTED, str(d)])
    return job['id']


def test_the_listing_page_scrape_keeps_the_hosts_airbnb_id_for_the_reel(airbnb_net, db):
    from app import pipeline
    assert pipeline.scrape_listing(URL)['host_id'] == '987654321'  # pdpContext.hostId, as cohost.py reads it
    assert 'host_id' in pipeline.LISTING_KEYS and 'host' in pipeline.LISTING_KEYS


def test_a_host_who_objected_is_not_shown_as_someone_to_contact_on_the_reel_page(owners, google, db):
    jid = _hosted_reel(owners, google)
    alice = client_for(owners['alice'])
    assert jobs.get(ALICE, jid)['meta']['listing']['host_id'] == '777'
    page = alice.get(f'/jobs/{jid}').text
    assert 'id="host-card"' in page and 'Hi Sam, I made a reel' in page and '/contact_host/4242/' in page
    _request(client_for(), airbnb_profile='https://www.airbnb.co.uk/users/show/777')  # the host objects
    page = alice.get(f'/jobs/{jid}').text
    assert 'id="host-card"' not in page and 'Hi Sam' not in page and '/contact_host/' not in page and 'Host message' not in page
    view = alice.get(f'/api/jobs/{jid}').json()
    assert view['host_suppressed'] is True and view['contact_url'] is None and not view['message_final'] and not view['message']
    js = alice.get('/static/app.js').text
    assert 'host_suppressed' in js  # the live page hides the card too when the job finishes after the objection


def test_the_hosts_id_goes_with_the_host_name_after_30_days(owners, google, db):
    import time
    from app import retention
    jid = _hosted_reel(owners, google)
    with db.connect() as c:
        c.execute('UPDATE jobs SET finished_at=%s WHERE id=%s', (time.time() - 31 * 86400, jid))
    retention.run()
    listing = jobs.get(ALICE, jid)['meta']['listing']
    assert 'host' not in listing and 'host_id' not in listing and listing['title'] == 'Sea view flat'
