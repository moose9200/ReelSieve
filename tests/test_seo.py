"""Crawler surface: robots.txt, sitemap.xml, llms.txt, indexing tags and response headers, all seen signed out."""
import re
import time
import xml.etree.ElementTree as ET
from urllib.robotparser import RobotFileParser

import pytest
from fastapi.testclient import TestClient

from app import auth, plans, server

SITE = 'https://www.reelsieve.braivex.com'
NOINDEX = '<meta name="robots" content="noindex, nofollow">'


@pytest.fixture
def web(owners, monkeypatch):
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    monkeypatch.delenv('RAILWAY_PUBLIC_DOMAIN', raising=False)
    with TestClient(server.app) as anon, TestClient(server.app) as alice:
        alice.cookies.set(auth.COOKIE, owners['alice'])
        yield anon, alice


def get(client, path, **kw):
    r = client.get(path, follow_redirects=False, **kw)
    assert r.status_code == 200, (path, r.status_code, r.headers.get('location'))
    return r


def test_crawler_files_are_public_with_their_content_types(web):
    anon, _ = web
    for path, ctype in [('/robots.txt', 'text/plain'), ('/sitemap.xml', 'application/xml'), ('/llms.txt', 'text/plain')]:
        assert get(anon, path).headers['content-type'].startswith(ctype), path


def test_robots_blocks_private_areas_and_names_the_sitemap(web):
    anon, _ = web
    rp = RobotFileParser()
    rp.parse(get(anon, '/robots.txt').text.splitlines())
    for path in ['/app', '/api/jobs', '/jobs/abc', '/reels', '/outreach', '/settings', '/account', '/upgrade/paid',
                 '/oauth/google/start', '/logout']:
        assert not rp.can_fetch('*', SITE + path), path
    for path in ['/', '/signup', '/privacy', '/terms', '/llms.txt', '/static/style.css']:
        assert rp.can_fetch('*', SITE + path), path
    assert rp.site_maps() == [SITE + '/sitemap.xml']


def test_canonical_origin_is_configured_never_the_host_header(web, monkeypatch):
    anon, _ = web
    assert 'Sitemap: ' + SITE + '/sitemap.xml' in get(anon, '/robots.txt', headers={'Host': 'evil.example'}).text
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://staging.example.test/')
    assert 'Sitemap: https://staging.example.test/sitemap.xml' in get(anon, '/robots.txt').text


def test_sitemap_lists_only_indexable_pages_with_template_dates(web):
    anon, _ = web
    ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    urls = ET.fromstring(get(anon, '/sitemap.xml').content).findall('s:url', ns)
    got = {u.find('s:loc', ns).text: u.find('s:lastmod', ns).text for u in urls}
    assert set(got) == {SITE + p for p in ('/', '/signup', '/privacy', '/terms')}
    legal = (server.HERE / 'templates' / 'legal.html').stat().st_mtime
    assert got[SITE + '/privacy'] == time.strftime('%Y-%m-%d', time.gmtime(legal))


def test_llms_txt_reads_live_prices_and_carries_the_disclaimer(web, monkeypatch):
    anon, _ = web
    monkeypatch.setitem(plans.PLANS['starter'], 'price_label', '$123')
    text = get(anon, '/llms.txt').text
    assert text.startswith('# ReelSieve\n\n> ')
    assert '- Starter: $123 for 3 videos' in text
    for p in plans.PLANS.values():
        assert p['name'] in text and (p['price_usd'] is None or p['price_label'] in text), p['key']
    assert 'not endorsed by or associated with Airbnb, Inc.' in text
    assert not re.search(r'\.[ \t]+\S', text), 'owner rule: a new line after every full stop'


def test_only_public_pages_are_indexable(web):
    anon, alice = web
    assert NOINDEX in get(alice, '/account').text
    assert NOINDEX in get(anon, '/login').text
    for path, canonical in [('/privacy', '/privacy'), ('/terms', '/terms'), ('/signup?plan=starter', '/signup')]:
        page = get(anon, path).text
        assert 'noindex' not in page, path
        assert f'<link rel="canonical" href="{SITE}{canonical}">' in page, path


def test_security_and_static_cache_headers(web):
    anon, _ = web
    for r in [get(anon, '/'), get(anon, '/healthz'), anon.get('/app', follow_redirects=False)]:
        assert r.headers['x-content-type-options'] == 'nosniff'
        assert r.headers['referrer-policy'] == 'strict-origin-when-cross-origin'
        assert r.headers['x-frame-options'] == 'DENY'
    assert get(anon, '/static/style.css').headers['cache-control'] == 'public, max-age=86400'
    assert 'cache-control' not in anon.get('/static/missing.css').headers


def test_asset_urls_carry_the_build_so_deploys_bust_the_cache(web):
    from app import server
    anon, _ = web
    assert f'/static/landing.css?v={server.BUILD}' in anon.get('/').text
    login = anon.get('/login').text
    assert f'/static/style.css?v={server.BUILD}' in login and f'/static/app.js?v={server.BUILD}' in login
