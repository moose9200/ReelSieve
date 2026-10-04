"""Refer-a-host programme through the browser: invite link, signup attribution, Account card, admin totals, notice.
PECR reg 6: no referral cookie or browser storage. ICO on refer-a-friend: a link and guidance, never a messaging tool."""
import hashlib
import re

from app import admin, plans, referrals, store
from test_braivex_sso import Broker, broker, client_for, csrf_of, start  # noqa: F401  (broker is a fixture)
from test_privacy_rights import ALICE, BOB, web  # noqa: F401  (web is a fixture)
from test_referrals import bonus, new_user, referral_rows


def braivex_sign_in(client, broker: Broker, email, ip, path='/auth/braivex/start?next=/app'):
    """Continue with Braivex up to the point the account exists or is made: start, then the broker's callback."""
    _, state = start(client, path)
    sub = 'gid://shopify/Customer/' + str(int(hashlib.sha256(email.encode()).hexdigest()[:12], 16))
    return client.post('/auth/braivex/callback', data={'assertion': broker.assertion(state=state, email=email, sub=sub)},
                       headers={'X-Forwarded-For': ip}, follow_redirects=False)


def signup(client, code, email, broker, ip='198.51.100.20'):
    """Sign up through an invite link the only way there is (04 Oct 2026): the page's Continue with Braivex link,
    which carries the code, then the "name your business" step that creates the account."""
    page = client.get('/signup?ref=' + code).text
    href = re.search(r'href="(/auth/braivex/start\?[^"]+)"', page).group(1).replace('&amp;', '&')
    assert braivex_sign_in(client, broker, email, ip, href).headers['location'] == '/auth/braivex/workspace'
    form = client.get('/auth/braivex/workspace').text
    return client.post('/auth/braivex/workspace', data={'csrf': csrf_of(form)}, headers={'X-Forwarded-For': ip},
                       follow_redirects=False)


def test_link_redirects_to_signup_with_the_code_and_stores_nothing_in_the_browser(web, db):
    code = referrals.code_for(ALICE)
    r = client_for().get('/r/' + code, follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'] == '/signup?ref=' + code
    assert code not in ' '.join(r.headers.get_list('set-cookie'))
    assert [c.split('=')[0] for c in r.headers.get_list('set-cookie')] == ['__Host-reelsieve_csrf']  # what the Account card says
    assert client_for().get('/r/not-a-code!', follow_redirects=False).headers['location'] == '/signup'
    page = client_for().get('/signup?ref=' + code)
    assert f'&amp;ref={code}"' in page.text           # the code rides on the Continue with Braivex link
    assert code not in ' '.join(page.headers.get_list('set-cookie'))
    assert 'invited you' in page.text
    js = web['anon'].get('/static/app.js').text
    assert js.count('localStorage.setItem') == 1 and 'lr-theme' in js and 'document.cookie' not in js  # theme only


def test_signup_through_the_link_records_who_invited_the_new_account(web, db, broker):
    code = referrals.code_for(ALICE)
    r = signup(client_for(), code, 'carol@example.org', broker)
    assert r.status_code == 303
    [row] = referral_rows(db)
    assert row['referrer_id'] == db.user_id(ALICE) and row['referee_id'] == db.user_id('carol@example.org')
    assert row['ts'] and row['rewarded_at'] is None and row['reward_reason'] is None


def test_a_refused_signup_records_no_invite(web, db, broker):
    code = referrals.code_for(ALICE)
    r = signup(client_for(), code, 'carol@mailinator.com', broker)        # a disposable address: the guard refuses
    assert r.status_code == 400 and referral_rows(db) == []


def test_unknown_codes_and_deactivated_referrers_are_ignored(web, db, broker):
    assert signup(client_for(), 'aaaaaaaaaaaa', 'carol@example.org', broker).status_code == 303
    code = referrals.code_for(BOB)
    admin.deactivate(BOB)
    assert signup(client_for(), code, 'dave@example.org', broker).status_code == 303
    assert referral_rows(db) == []


def test_signup_from_a_network_the_referrer_made_free_videos_on_is_never_rewarded(web, db, broker):
    plans.reserve(ALICE, 'https://www.airbnb.co.uk/rooms/1', 'aaaaaa000001', ip='203.0.113.7')
    carol = client_for()
    signup(carol, referrals.code_for(ALICE), 'carol@example.org', broker, ip='203.0.113.99')  # same /24
    [row] = referral_rows(db)
    assert row['reward_reason'] is None  # nothing decided at signup, so there is no instant answer to read
    [mine] = carol.get('/api/account/export').json()['referrals']
    assert mine == {'you_are': 'referee', 'ts': row['ts'], 'rewarded_at': None, 'status': 'pending', 'google_account_hash': None}
    with db.connect() as c:
        assert referrals.reward_first_delivery(c, db.user_id('carol@example.org')) == 'same_network'
    assert carol.get('/api/account/export').json()['referrals'][0]['status'] == 'not_rewarded'
    assert bonus(db, ALICE) == 0 and bonus(db, 'carol@example.org') == 0


def test_sign_in_and_sign_up_keep_a_network_hash_never_the_address(web, db, broker):
    r = braivex_sign_in(client_for(), broker, BOB, '198.51.100.77')
    assert r.status_code == 303 and r.headers['location'] == '/app'
    signup(client_for(), referrals.code_for(ALICE), 'carol@example.org', broker, ip='203.0.113.5')
    with db.connect() as c:
        rows = c.execute('SELECT owner_id,ip_hash FROM signin_networks ORDER BY owner_id').fetchall()
    assert sorted((r['owner_id'], r['ip_hash']) for r in rows) == sorted(
        [(db.user_id(BOB), store.ip_hash('198.51.100.77')), (db.user_id('carol@example.org'), store.ip_hash('203.0.113.5'))])
    assert '198.51.100' not in str(rows) and '203.0.113' not in str(rows)


def test_account_page_offers_the_link_with_plain_sharing_guidance_and_no_messaging(web, db):
    code = referrals.code_for(ALICE)
    page = web['alice'].get('/account').text
    card = page[page.index('id="invite-card"'):page.index('</section>', page.index('id="invite-card"'))]
    assert 'Invite other hosts' in card and f'value="https://www.reelsieve.braivex.com/r/{code}"' in card  # configured origin
    assert 'id="ref-copy"' in card and '0 rewarded' in card
    for words in ('public posts', 'social media', 'website', '#ad', "Don't put it in Airbnb listings, guidebooks",
                  'someone who has asked you for it', 'both you and ReelSieve', 'security cookie'):
        assert words in card, words
    for wrong in ('your guest guidebook', 'the person who sends them is responsible', "We don't set a cookie"):
        assert wrong not in card, wrong
    for banned in ('mailto:', 'sms:', 'wa.me', 'navigator.share', 'type="email"', '<textarea'):
        assert banned not in card, banned
    js = web['alice'].get('/static/app.js').text
    assert 'ref-copy' in js and 'navigator.share' not in js
    assert 'Invites: your invite code' in web['anon'].get('/privacy').text  # the notice lists the new data
    terms = web['anon'].get('/terms').text
    for words in ('Neither of you gets a bonus video', '10 rewards', 'same Google account', '#ad', 'guidebooks'):
        assert words in terms, words


def test_bonus_videos_show_as_used_first_only_where_they_are_spent(web, db):
    store.ensure_account(ALICE)
    store.set_plan('operator@example.test', 'enterprise', 0)  # admins default to unlimited Enterprise
    with db.connect() as c:
        c.execute('UPDATE accounts SET bonus_videos=1')
    assert '1 bonus video from invites, used first' in web['alice'].get('/account').text
    assert 'from invites, used first' not in web['admin'].get('/account').text


def test_admin_settings_show_programme_totals(web, db):
    store.ensure_account(BOB)
    new_user('carol@example.org')
    referrals.attribute('carol@example.org', referrals.code_for(BOB))
    totals = referrals.totals()
    assert totals['signups'] == 1 and totals['pending'] == 1 and totals['rewarded'] == 0
    page = web['admin'].get('/settings').text
    assert 'id="referrals-card"' in page and 'Signed up through a link' in page
    assert web['alice'].get('/settings', follow_redirects=False).status_code == 303
