"""LinkedIn prospect links: never present a search as a profile, and carry the exact Airbnb host profile.
Airbnb is the synthetic fake (tests/fakes.py); no real network."""
import csv
import io
import json

import pytest

from app import cohost, linkedin

LISTING_HTML = ('<html>Hosted by Leo<script>{"pdpContext":{"isSuperHost":"true","hostId":"987654321"},'
                '"listingsCount":4}</script></html>')


def test_listing_host_reads_the_host_id_already_on_the_listing_page(airbnb_net):
    airbnb_net.pages['/rooms/'] = LISTING_HTML
    h = cohost._listing_host('42')
    assert h['name'] == 'Leo' and h['host_id'] == '987654321'


def test_listing_host_without_an_id_has_no_profile(airbnb_net):
    airbnb_net.pages['/rooms/'] = '<html>Hosted by Leo</html>'
    assert cohost._listing_host('42')['host_id'] is None


def test_operators_carry_the_exact_airbnb_profile(airbnb_net, monkeypatch):
    monkeypatch.setattr(cohost.listing_search, 'search', lambda *a, **k: {'items': [
        {'id': '42', 'url': 'https://www.airbnb.co.uk/rooms/42', 'name': 'Sea view', 'rating': 4.9, 'reviews': 10}]})
    airbnb_net.pages['/rooms/'] = LISTING_HTML
    [op] = cohost._operators('Poole')
    assert op['profile_url'] == 'https://www.airbnb.co.uk/users/show/987654321'


def fake_discover(city, limit=10):
    return {'items': [
        {'name': 'William', 'listing_title': 'Harbour flat', 'listing_url': 'https://www.airbnb.co.uk/rooms/1',
         'profile_url': 'https://www.airbnb.co.uk/users/show/111', 'listings': 3, 'tagline': '3 listings'},
        {'name': 'Trinh', 'listing_title': 'Quay loft', 'listing_url': 'https://www.airbnb.co.uk/rooms/2', 'profile_url': None},
    ]}


def test_build_never_presents_a_search_as_a_profile(monkeypatch):
    monkeypatch.setattr(linkedin.cohost, 'discover', fake_discover)
    items = linkedin.build('Poole', 'property manager')['items']
    assert [i['link_label'] for i in items] == ['Search LinkedIn ↗', 'Search LinkedIn ↗']
    assert all(i['url'].startswith('https://www.linkedin.com/search/results/people/?keywords=') for i in items)
    assert not any('linkedin.com/in/' in json.dumps(i) for i in items)
    assert items[0]['airbnb_profile'] == 'https://www.airbnb.co.uk/users/show/111'
    assert items[1]['airbnb_profile'] == ''


@pytest.mark.parametrize('url,label', [
    ('https://www.linkedin.com/in/jane-doe-123/', 'LinkedIn profile ↗'),
    ('https://uk.linkedin.com/in/jane-doe', 'LinkedIn profile ↗'),
    ('https://www.linkedin.com/search/results/people/?keywords=William%20Poole', 'Search LinkedIn ↗'),
    ('https://www.airbnb.co.uk/users/show/111', 'Airbnb profile ↗'),
    ('https://www.airbnb.co.uk/contact_host/42/send_message', 'Open ↗'),
    ('https://www.linkedin.com.evil.test/in/jane', 'Open ↗'),
    ('http://www.linkedin.com/in/jane', 'Open ↗'),
    ('', 'Open ↗'),
])
def test_link_label_follows_what_the_link_really_is(url, label):
    assert linkedin.link_label(url) == label


@pytest.mark.parametrize('value,ok', [
    ('https://www.airbnb.co.uk/users/show/111', True),
    ('https://www.airbnb.com/users/show/111', True),
    ('https://www.airbnb.co.uk.evil.test/users/show/111', False),
    ('javascript:alert(1)//https://www.airbnb.co.uk/users/show/1', False),
    ('https://www.airbnb.co.uk/users/show/111?x=<script>', False),
    (None, False),
])
def test_airbnb_profile_accepts_only_exact_airbnb_profile_urls(value, ok):
    assert linkedin.airbnb_profile(value) == (value if ok else '')


def test_csv_names_the_link_type_and_the_airbnb_profile():
    rows = [{'ts': 0, 'channel': 'linkedin', 'name': 'William', 'city': 'Poole', 'status': 'queued',
             'url': 'https://www.linkedin.com/search/results/people/?keywords=William',
             'meta': json.dumps({'airbnb_profile': 'https://www.airbnb.co.uk/users/show/111'}), 'message': 'Hi', 'note': None}]
    out = list(csv.reader(io.StringIO(linkedin.csv_rows(rows))))
    assert out[0] == ['when', 'channel', 'name', 'city', 'status', 'link_type', 'link_url', 'airbnb_profile', 'message',
                      'note', 'sent_at']
    rec = dict(zip(out[0], out[1]))
    assert rec['link_type'] == 'LinkedIn search' and rec['airbnb_profile'] == 'https://www.airbnb.co.uk/users/show/111'
