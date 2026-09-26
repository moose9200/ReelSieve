"""Public landing page: one H1, canonical, valid JSON-LD that matches the visible FAQ, live prices, disclaimer."""
import html
import json
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from app import auth, plans, server

SITE = 'https://www.reelsieve.braivex.com/'


@pytest.fixture
def web(owners):
    with TestClient(server.app) as anon, TestClient(server.app) as alice:
        alice.cookies.set(auth.COOKIE, owners['alice'])
        yield anon, alice


def landing(client):
    r = client.get('/', follow_redirects=False)
    assert r.status_code == 200
    return r.text


def text(fragment):
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', fragment)).split())


def json_ld(page):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    assert blocks
    return [json.loads(b) for b in blocks]


def graph(page, kind):
    return [n for block in json_ld(page) for n in block.get('@graph', [block]) if n['@type'] == kind]


class BodyText(HTMLParser):
    """Text nodes inside <body>, skipping scripts, styles and inline SVG."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside, self.skip, self.nodes = False, 0, []

    def handle_starttag(self, tag, attrs):
        self.inside |= tag == 'body'
        self.skip += tag in ('script', 'style', 'svg')

    def handle_endtag(self, tag):
        self.skip -= tag in ('script', 'style', 'svg')

    def handle_data(self, data):
        if self.inside and not self.skip and data.strip():
            self.nodes.append(' '.join(data.split()))


def test_anonymous_landing_has_one_h1_canonical_and_absolute_share_image(web):
    page = landing(web[0])
    assert len(re.findall(r'<h1\b', page)) == 1
    assert f'<link rel="canonical" href="{SITE}">' in page
    assert f'<meta property="og:image" content="{SITE}static/sample/poster.jpg">' in page
    assert 'preload="none"' in page
    assert 'https://reelsieve.com' not in page


def test_json_ld_parses_and_faq_matches_the_visible_questions_and_answers(web):
    page = landing(web[0])
    assert {n['@type'] for b in json_ld(page) for n in b['@graph']} >= {'Organization', 'SoftwareApplication', 'FAQPage'}
    (faq,) = graph(page, 'FAQPage')
    questions = [text(q) for q in re.findall(r'<summary>(.*?)</summary>', page, re.S)]
    answers = [text(a) for a in re.findall(r'<p class="lp-lines lp-answer">(.*?)</p>', page, re.S)]
    assert 8 <= len(questions) <= 12
    assert [q['name'] for q in faq['mainEntity']] == questions
    assert [q['acceptedAnswer']['text'] for q in faq['mainEntity']] == answers


def test_prices_come_from_plans(web, monkeypatch):
    monkeypatch.setitem(plans.PLANS['starter'], 'price_label', '$123')
    monkeypatch.setitem(plans.PLANS['commercial'], 'price_usd', 777)
    page = landing(web[0])
    assert '<p class="lp-price">$123</p>' in page
    assert 'Starter costs $123 for 3 videos' in page
    (app,) = graph(page, 'SoftwareApplication')
    offers = {o['name']: o['price'] for o in app['offers']}
    assert offers == {p['name']: p['price_usd'] for p in plans.PLANS.values() if p['price_usd'] is not None}
    for p in plans.PLANS.values():
        assert f'<p class="lp-price">{escape(p["price_label"])}</p>' in page, p['key']


def test_disclaimer_and_one_sentence_per_line(web):
    page = landing(web[0])
    assert 'ReelSieve is independent and is not endorsed by or associated with Airbnb, Inc.' in page
    parser = BodyText()
    parser.feed(page)
    joined = [t for t in parser.nodes if re.search(r'[.!?]\s+["A-Z0-9$]', t)]
    assert not joined, 'owner rule: each sentence on its own line'


def test_signed_in_visitors_get_app_and_upgrade_links(web):
    page = landing(web[1])
    assert 'href="/app">Open the app</a>' in page
    assert 'href="/upgrade?plan=starter"' in page
    assert 'action="/app"' in page
