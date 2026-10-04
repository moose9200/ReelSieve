"""One footer partial (templates/_site_footer.html) on every page, the Braivex one (Loculens reference).

The landing page and base.html both include it, so the pages cannot drift apart.
"""
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, server

PRODUCT = ['/#how', '/#sample', '/#pricing', '/#faq', '/signup', '/login']
COMPANY = ['https://braivex.com', 'https://braivex.com/pages/about-us', 'https://braivex.com/pages/contact']
LEGAL = ['/privacy', '/terms', '/privacy/request', 'https://braivex.com/policies/refund-policy',
         'https://braivex.com/policies/contact-information', 'https://braivex.com/policies/legal-notice']
AIRBNB = ('ReelSieve is an independent third party and is not endorsed by or associated with Airbnb, Inc. '
          'or its affiliates.')


@pytest.fixture
def pages(owners, monkeypatch):
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    anon, alice = TestClient(server.app), TestClient(server.app)
    alice.cookies.set(auth.COOKIE, owners['alice'])
    return lambda path, who='anon': (alice if who == 'alice' else anon).get(path).text


def footer_of(page):
    assert page.count('<footer class="lp-footer">') == 1, 'exactly one footer per page'
    return page.split('<footer class="lp-footer">', 1)[1].split('</footer>', 1)[0]


ANON_PAGES = ['/', '/login', '/signup', '/privacy', '/terms', '/privacy/request']


@pytest.mark.parametrize('path', ANON_PAGES)
def test_every_public_page_carries_the_whole_footer(pages, path):
    foot = footer_of(pages(path))
    for href in PRODUCT + COMPANY + LEGAL:
        assert f'href="{href}"' in foot, (path, href)
    for heading in ('<h3>Product</h3>', '<h3>Company</h3>', '<h3>Legal</h3>'):
        assert heading in foot, (path, heading)
    assert 'Let&rsquo;s build something that matters.' in foot or 'Let’s build something that matters.' in foot
    assert 'Request a tailored briefing for your enterprise AI project.' in foot
    assert 'Initiate RFP' in foot and 'href="https://braivex.com/pages/contact"' in foot
    assert f'&copy; {time.strftime("%Y", time.gmtime())} Braivex.' in foot
    assert 'All trademarks are property of their respective owners.' in foot
    assert AIRBNB in foot
    assert re.search(r'<a class="lp-brand" href="/"[^>]*>.*?<span>ReelSieve</span>', foot, re.S), path
    assert 'More from Braivex' not in foot  # the product list is gone (28 Sep 2026 consistency spec)


@pytest.mark.parametrize('path', ['/app', '/account', '/reels', '/upgrade', '/outreach'])
def test_the_signed_in_app_has_the_same_footer(pages, path):
    landing, app_page = footer_of(pages('/')), footer_of(pages(path, 'alice'))
    assert app_page == landing, path


def test_the_footer_is_one_partial_and_one_stylesheet_both_templates_load(pages):
    from pathlib import Path
    app_dir = Path(server.__file__).resolve().parent
    assert (app_dir / 'templates' / '_site_footer.html').exists()
    for name in ('base.html', 'landing.html'):
        text = (app_dir / name.replace(name, 'templates/' + name)).read_text()
        assert '{% include "_site_footer.html" %}' in text, name
        assert '<footer' not in text, name  # no page keeps a footer of its own
        assert '/static/footer.css' in text, name  # the landing page does not load style.css
    css = (app_dir / 'static' / 'footer.css').read_text()
    for value in ('padding: 64px 40px 28px', '1.7fr 1fr 1fr 1.1fr', 'gap: 40px', 'letter-spacing: .18em',
                  '#00f0ff', 'max-width: 34ch', 'max-width: 650px', 'padding: 40px 16px 32px'):
        assert value in css, value  # the Loculens values
    for path in ANON_PAGES:
        assert 'fonts.googleapis.com' not in pages(path) and 'fonts.gstatic.com' not in pages(path)
