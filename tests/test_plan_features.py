"""Every feature a plan advertises is something the product enforces or does; nothing else is listed."""
import sys

import pytest
from fastapi.testclient import TestClient

from app import auth, billing, jobs, plans, server, store, worker
from fakes import connect

URL = 'https://www.airbnb.co.uk/rooms/'
NOT_BUILT = ['api', 'white-label', 'white label', 'bulk', 'priority', 'email', 'brand', 'outro', 'dedicated', 'support',
             'spreadsheet', 'most popular']


def test_advertised_features_contain_no_unbuilt_promises():
    for p in plans.PLANS.values():
        text = ' '.join(p['features'] + [p['period']]).lower()
        assert not [w for w in NOT_BUILT if w in text], (p['key'], text)
        assert f"up to {p['max_seconds']} seconds" in text
        assert ('ai camera motion' in text) == p['ai_motion']
        assert ('unlimited videos' in text) if p['videos'] is None else (f"{p['videos']} videos" in text)


@pytest.fixture
def paying(owners, google, monkeypatch, tmp_path):
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    connect(owners, google)
    return owners


def test_paid_plan_limits_are_enforced_at_admission(paying, db):
    store.set_plan('alice@example.test', 'starter', 3)
    job = jobs.admit('alice@example.test', URL + '1', {'attested': True, 'ai_motion': True, 'style': 'tutorial'}, 'a')
    assert job['params']['max_seconds'] == 90 and job['params']['ai_motion'] is True and job['params']['style'] == 'v3'
    assert plans.account_view('alice@example.test')['credits'] == 2
    cmd = worker.render_command(jobs.claim('w', 30), '/tmp/x')
    assert '"max_seconds": 90' in cmd[4] and '"ai_motion": true' in cmd[4] and '"renderer": "v3"' in cmd[4]


def test_free_plan_gets_both_styles_but_not_ai_motion(paying, db):
    job = jobs.admit('alice@example.test', URL + '2', {'attested': True, 'ai_motion': True, 'style': 'tutorial'}, 'b')
    assert job['params']['style'] == 'v3' and job['params']['ai_motion'] is False and job['params']['max_seconds'] == 60


def test_paid_pack_grants_its_videos_and_remaking_a_listing_is_free(paying, db):
    order = billing.create_order('alice@example.test', 'starter', 'invoice')
    billing.settle(order['ref'], by='test')
    assert plans.account_view('alice@example.test')['credits'] == plans.PLANS['starter']['videos']
    jobs.admit('alice@example.test', URL + '3', {'attested': True}, 'c1')
    jobs.admit('alice@example.test', URL + '3', {'attested': True}, 'c2')
    assert plans.account_view('alice@example.test')['credits'] == plans.PLANS['starter']['videos'] - 1


def test_enterprise_is_unlimited(paying, db):
    store.set_plan('alice@example.test', 'enterprise', 0)
    for i in range(5):
        jobs.admit('alice@example.test', URL + str(100 + i), {'attested': True}, f'e{i}')
    assert plans.account_view('alice@example.test')['remaining'] is None


def test_upgrade_and_account_pages_show_only_real_features(paying):
    with TestClient(server.app) as client:
        client.cookies.set(auth.COOKIE, paying['alice'])
        for page in ['/upgrade?plan=starter', '/upgrade?plan=commercial', '/upgrade?plan=enterprise', '/account']:
            text = client.get(page).text.lower()
            assert not [w for w in ['api access', 'white-label', 'bulk', 'priority rendering', 'email + drive',
                                    'your brand', 'outro', 'dedicated support', '9:16 vertical cut included'] if w in text], page
