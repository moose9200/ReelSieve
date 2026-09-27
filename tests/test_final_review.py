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
