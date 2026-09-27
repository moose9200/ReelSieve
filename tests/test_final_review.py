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
