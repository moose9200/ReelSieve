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


def test_account_page_shows_changes_our_team_made_without_naming_the_admin(owners, db):
    from fastapi.testclient import TestClient
    from app import auth, server, store
    store.admin_event('password_reset', 'bob@example.test', 'alice@example.test')
    store.admin_event('plan', 'bob@example.test', 'alice@example.test', plan='starter', credits=3)
    with TestClient(server.app) as client:
        client.cookies.set(auth.COOKIE, owners['alice'])
        text = client.get('/account').text
    assert 'Password reset by our team' in text and 'Plan or credits changed by our team' in text
    assert 'bob@example.test' not in text


# ---------------- final pre-deploy review (27 Sep 2026): the notice says what the code does ----------------

import html as _html  # noqa: E402
import re as _re  # noqa: E402

import pytest  # noqa: E402


def _text(page):
    """Visible text, one line: tags dropped, entities decoded, spaces collapsed."""
    return _re.sub(r' ([.,;:])', r'\1', ' '.join(_html.unescape(_re.sub(r'<[^>]+>', ' ', page)).split()))


@pytest.fixture
def pages(owners, monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, server
    for k in ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'CHECKOUT_STARTER', 'CHECKOUT_COMMERCIAL', 'INVOICE_BACKUP_ENDPOINT'):
        monkeypatch.delenv(k, raising=False)
    anon, alice = TestClient(server.app), TestClient(server.app)
    alice.cookies.set(auth.COOKIE, owners['alice'])
    return lambda path, who='anon': _text((alice if who == 'alice' else anon).get(path).text)


def test_notice_pins_google_permissions_tokens_and_in_app_playback(pages):
    notice = pages('/privacy')
    assert 'Last updated 27 Sep 2026' in notice
    ask = "your Google account's email address and ID to show which account is connected"
    assert 'drive.file' in notice and ask in notice                                                          # N4
    assert ask in pages('/account', 'alice') and ask in pages('/')                                           # N4 card, landing
    assert 'encrypted Google access and refresh tokens' in notice                                             # N13
    assert 'play or download a reel inside ReelSieve: the video passes through our server on its way to you and is not stored' in notice  # N12


def test_notice_pins_what_account_deletion_keeps_and_for_how_long(pages):
    notice, account = pages('/privacy'), pages('/account', 'alice')
    for kept in ('paid orders for 8 years from payment (reference, date, plan, amount, currency, payment provider, status, '
                 'time paid and billing email)', 'a payment you reported that has not cleared, for 90 days',
                 'privacy requests and our admin records, for 2 years', 'network codes on free videos, until their 90 days end',
                 'daily India backup copies, until they expire within 90 days', 'until it is deleted by 25 Dec 2026'):
        assert kept in notice, kept                                                                            # N6
    for kept in ('paid orders for 8 years', 'for 90 days', 'for 2 years', 'until their 90 days end', 'within 90 days',
                 'until 25 Dec 2026'):
        assert kept in account.split('Delete your account', 1)[1], kept                                        # N6 mirror
    assert ("We don't set a referral or tracking cookie. The only cookies are the sign-in and security cookies "
            'described in our privacy notice.') in account                                                     # N3


def test_notice_pins_the_legacy_archive_higgsfield_and_payments(pages, monkeypatch):
    notice = pages('/privacy')
    assert ('An encrypted copy of our previous system, from before 26 Sep 2026: accounts (including password hashes), '
            'Google Drive tokens and old job data, including reviews') in notice                               # N10
    assert 'Deleted automatically 90 days after it was made, by 25 Dec 2026' in notice
    assert 'It is not part of "Download my data"' in notice and 'archive' not in pages('/account', 'alice').split('Download my data', 1)[1].split('Delete your account', 1)[0]
    assert 'We cannot delete them from Higgsfield' not in notice                                               # N7
    assert 'We do not delete these photos from Higgsfield after use' in notice
    assert '"may remain on our active servers for 30 days, and copies of the content may be held in backups' in notice
    assert ('We send Stripe your account email, the order reference and an internal account number. '
            'Your card details go to Stripe directly') in notice                                              # N14
    assert 'Card checkout and payment links are off until we switch them on' in notice                         # transfers
    assert "Higgsfield, in the United States, receives a listing's photos only for listing-link reels with AI camera motion" in notice
    monkeypatch.setenv('CHECKOUT_STARTER', 'https://pay.provider.test/starter')
    notice = pages('/privacy')
    assert 'hosted at pay.provider.test' in notice and 'Card checkout and payment links are off' not in notice


def test_notice_pins_orders_outreach_hosts_backup_companies_and_cookies(pages, monkeypatch):
    notice = pages('/privacy')
    assert 'invoice details you type (such as company name, VAT/GST number or PO number)' in notice             # N15
    assert 'payment method' in notice and 'any payment link we send you' in notice
    assert ('names, town, public Airbnb profile and listing links, LinkedIn search links' in notice
            and "each record's status and when you marked it sent" in notice)                                  # N17
    assert 'how many listings they have and whether they are a Superhost' in notice                           # N16
    assert ('public Airbnb listing, search and Co-Host Network pages. We link to hosts\' public profile pages but do not '
            'fetch them') in notice
    assert 'Amazon Web Services (AWS)' in notice and 'currency, payment provider' in notice                    # N18
    assert 'each copy is deleted within 90 days' in notice
    assert 'active UK companies (limited, unlimited and community interest companies) and LLPs' in notice      # N19
    assert 'Active UK companies (limited, unlimited and community interest companies) and LLPs' in pages('/outreach', 'alice')
    assert ('It is deleted when you close your browser and stops working after 12 hours at most, or lasts 30 days if '
            'you choose to stay signed in') in notice                                                          # N20
    monkeypatch.setenv('INVOICE_BACKUP_BUCKET', 'b'), monkeypatch.setenv('INVOICE_BACKUP_ACCESS_KEY_ID', 'k')
    monkeypatch.setenv('INVOICE_BACKUP_SECRET_ACCESS_KEY', 's'), monkeypatch.setenv('INVOICE_BACKUP_ENDPOINT', 'https://s3.in.example')
    assert 'Amazon Web Services' not in pages('/privacy')  # AWS is named only while no other endpoint is configured


def test_notice_pins_listing_content_and_the_airbnb_safeguards(pages):
    notice = pages('/privacy')
    assert 'The photos and review text are deleted from our servers when the job ends' in notice               # N11
    assert "Your reel history keeps the listing's title, town, rating and link" in notice
    assert "The host's name and user number are removed after 30 days" in notice
    assert 'logged out and at a limited rate' in notice and 'we pause all fetching at once' in notice          # safeguards
    assert 'If it blocks us again within a day of a pause, or refuses a page for legal reasons, we stop until a person has checked' in notice
    assert 'the block lapses after one month unless we confirm it' in notice
    form = pages('/privacy/request')
    assert 'ReelSieve stops offering you as a contact on reels of your listings' in form                      # N2
    assert form.count('undo it only if someone else sent it') == 2 and notice.count('undo an entry only if someone else sent it') == 2  # S1


def test_terms_pin_photo_rights_ownership_and_the_listing_link_note(pages):
    terms = pages('/terms')
    assert 'Photos you upload must be ones you own or are licensed to use.' in terms                           # N9
    assert "You own what ReelSieve adds; the listing's photos and reviews stay their owners'." in terms
    assert "Do not publish or share a reel of someone else's listing without the host's permission" in terms
    assert "Do not publish or share that reel without the host's permission" in pages('/app', 'alice')
    assert 'full rights' not in terms + pages('/')


def test_sign_in_throttle_counts_a_whole_ipv6_network_but_keeps_ipv4_addresses_apart(db):
    from app import auth
    for i in range(5):
        auth.record_fail(f'2001:db8:1:2::{i + 1}')
    assert auth.too_many('2001:db8:1:2::99')          # same /64, another address: still limited
    assert not auth.too_many('2001:db8:1:3::1')       # another /64
    for i in range(5):
        auth.record_fail('203.0.113.7')
    assert auth.too_many('203.0.113.7') and not auth.too_many('203.0.113.8')  # shared offices are not locked out


def test_default_host_message_does_not_claim_the_sender_runs_reelsieve():
    from app import hostmsg
    assert 'I run ReelSieve' not in hostmsg.DEFAULT_MESSAGE and 'Braivex' not in hostmsg.DEFAULT_MESSAGE
    assert '{host_name}' in hostmsg.DEFAULT_MESSAGE and '{listing_title}' in hostmsg.DEFAULT_MESSAGE
