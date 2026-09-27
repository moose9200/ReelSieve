"""'Your own photos' reels and the ownership confirmation for listing links.

Real isolated PostgreSQL, the synthetic Google fake (tests/fakes.py) and stub render children:
nothing here fetches Airbnb, calls a paid provider or sends a message.
"""
import time

import pytest

from app import jobs
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
