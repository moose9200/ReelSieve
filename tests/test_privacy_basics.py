"""Privacy basics that must never regress: no visitor data sent to third-party asset hosts."""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'app'


def test_no_page_loads_fonts_or_assets_from_google():
    offenders = [str(p.relative_to(APP)) for p in list((APP / 'templates').glob('*.html')) + list((APP / 'static').glob('*.css'))
                 if 'fonts.googleapis.com' in p.read_text() or 'fonts.gstatic.com' in p.read_text()]
    assert offenders == []
    assert (APP / 'static' / 'fonts' / 'assistant-latin.woff2').stat().st_size > 10_000
    assert 'assistant-latin.woff2' in (APP / 'static' / 'fonts.css').read_text()


def test_privacy_notice_keeps_the_items_the_law_requires(db):
    from fastapi.testclient import TestClient
    from app import server
    text = TestClient(server.app).get('/privacy').text
    for required in ['Hemant Kumar Sain', 'Alwar, Rajasthan', 'hello@braivex.com', '/privacy/request',
                     'Lawful basis', 'How long we keep it', 'Railway', 'Higgsfield', 'Stripe',
                     'International Data Transfer Addendum', 'Standard Contractual Clauses',
                     'ico.org.uk/make-a-complaint', 'within one month', 'Object to outreach',
                     'strictly necessary', 'Limited Use requirements', 'drive.file', 'under 18']:
        assert required in text, required


def test_https_responses_carry_hsts_and_plain_http_does_not(db):
    from fastapi.testclient import TestClient
    from app import server
    c = TestClient(server.app)
    assert c.get('/healthz', headers={'x-forwarded-proto': 'https'}).headers.get('strict-transport-security') == 'max-age=31536000'
    assert 'strict-transport-security' not in c.get('/healthz').headers
