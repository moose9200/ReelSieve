"""UK property companies from the Companies House Free Company Data Product (business to business).
Synthetic zips only: no test downloads the real snapshot (conftest refuses the network for app.companies)."""
import hashlib
import hmac
import io
import zipfile

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import auth, companies, server, store

ALICE, BOB, ADMIN = 'alice@example.test', 'bob@example.test', 'operator@example.test'
# The header line of the real snapshot, copied from BasicCompanyData-2026-09-01-part7_7.csv (fetched 27 Sep 2026).
HEADER = ('CompanyName, CompanyNumber,RegAddress.CareOf,RegAddress.POBox,RegAddress.AddressLine1, RegAddress.AddressLine2,'
          'RegAddress.PostTown,RegAddress.County,RegAddress.Country,RegAddress.PostCode,CompanyCategory,CompanyStatus,'
          'CountryOfOrigin,DissolutionDate,IncorporationDate,Accounts.AccountRefDay,Accounts.AccountRefMonth,'
          'Accounts.NextDueDate,Accounts.LastMadeUpDate,Accounts.AccountCategory,Returns.NextDueDate,Returns.LastMadeUpDate,'
          'Mortgages.NumMortCharges,Mortgages.NumMortOutstanding,Mortgages.NumMortPartSatisfied,Mortgages.NumMortSatisfied,'
          'SICCode.SicText_1,SICCode.SicText_2,SICCode.SicText_3,SICCode.SicText_4,LimitedPartnerships.NumGenPartners,'
          'LimitedPartnerships.NumLimPartners,URI,' + ','.join(f'PreviousName_{i}.CONDATE, PreviousName_{i}.CompanyName'
                                                                for i in range(1, 11)) + ',ConfStmtNextDueDate, ConfStmtLastMadeUpDate')
COLS = [h.strip() for h in HEADER.split(',')]
MGMT, HOLIDAY, OTHER = ('68320 - Management of real estate on a fee or contract basis',
                        '55209 - Other holiday and other collective accommodation', '47990 - Other retail sale')


def row(number, name, sic=(MGMT,), status='Active', category='Private Limited Company', origin='United Kingdom',
        town='BOURNEMOUTH', postcode='BH1 1AA', careof='', line1='1 SEA ROAD'):
    d = {'CompanyName': name, 'CompanyNumber': number, 'RegAddress.CareOf': careof, 'RegAddress.AddressLine1': line1,
         'RegAddress.PostTown': town, 'RegAddress.Country': 'ENGLAND', 'RegAddress.PostCode': postcode,
         'CompanyCategory': category, 'CompanyStatus': status, 'CountryOfOrigin': origin, 'IncorporationDate': '14/05/2024',
         **{f'SICCode.SicText_{i + 1}': s for i, s in enumerate(sic)}}
    return ','.join('"' + d.get(c, '').replace('"', '""') + '"' for c in COLS)


def snapshot_zip(path, rows, header=HEADER):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(path.stem + '.csv', '\r\n'.join([header, *rows]) + '\r\n')


def page(date, parts=2, listed=None):
    links = ''.join(f'<li><a href="BasicCompanyData-{date}-part{n}_{parts}.zip">BasicCompanyData-{date}-part{n}_{parts}.zip (69Mb)</a></li>'
                    for n in (listed or range(1, parts + 1)))
    return (f'<p>Company data as one file:</p><a href="BasicCompanyDataAsOneFile-{date}.zip">BasicCompanyDataAsOneFile-{date}.zip</a>'
            f'<ul>{links}</ul>')


@pytest.fixture
def ch(monkeypatch, tmp_path):
    """A synthetic Companies House: set .snapshots[date] = [rows of part 1, rows of part 2]."""
    class CH:
        snapshots, downloads, pages = {}, [], 0
        date = None
    fake = CH()
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))

    def get_page():
        fake.pages += 1
        return page(fake.date, len(fake.snapshots[fake.date]))

    def download(url, dest):
        fake.downloads.append(url.rsplit('/', 1)[1])
        assert url.startswith('https://download.companieshouse.gov.uk/BasicCompanyData-')
        assert dest.parent == tmp_path / 'scratch' / 'companies'
        n = int(url.rsplit('part', 1)[1].split('_')[0])
        snapshot_zip(dest, fake.snapshots[fake.date][n - 1])
    monkeypatch.setattr(companies, '_get_page', get_page)
    monkeypatch.setattr(companies, '_download', download)
    return fake


def table(db):
    with db.connect() as c:
        return {r['company_number']: r for r in c.execute('SELECT * FROM companies').fetchall()}


# ---------------- ingestion ----------------

def test_ingest_keeps_active_uk_corporate_property_companies_and_nothing_about_people(db, ch, tmp_path):
    ch.date = '2026-09-01'
    ch.snapshots[ch.date] = [
        [row('00000001', 'SEASIDE LETS LTD', careof='JOHN SMITH', line1='7 PRIVATE LANE'),
         row('OC000002', 'HARBOUR STAYS LLP', sic=(OTHER, HOLIDAY), category='Limited Liability Partnership', town='POOLE',
             postcode='bh15  1aa'),
         row('00000003', 'CLOSED LETS LTD', status='Liquidation'),
         row('00000004', 'STRIKE OFF LETS LTD', status='Active - Proposal to Strike off')],
        [row('LP000005', 'PARTNERS LP', category='Limited Partnership'),         # not a body corporate in England
         row('OE000006', 'OFFSHORE LETS', category='Overseas Entity', origin='JERSEY'),
         row('00000007', 'SHOP LTD', sic=(OTHER,)),
         row('00000008', 'BIG HOTEL LTD', sic=('55100 - Hotels and similar accommodation',))]]
    out = companies.refresh(now=1790000000)
    assert out == {'snapshot': '2026-09-01', 'rows': 2, 'deleted': 0}
    got = table(db)
    assert sorted(got) == ['00000001', 'OC000002']
    assert got['OC000002']['name'] == 'HARBOUR STAYS LLP' and got['OC000002']['town'] == 'POOLE'
    assert got['OC000002']['postcode'] == 'BH15 1AA' and got['OC000002']['sic_codes'] == ['47990', '55209']
    assert str(got['00000001']['incorporated']) == '2024-05-14' and got['00000001']['snapshot'] == '2026-09-01'
    assert set(got['00000001']) == {'company_number', 'name', 'town', 'postcode', 'country', 'sic_codes', 'incorporated', 'snapshot'}
    assert 'JOHN SMITH' not in str(got) and 'PRIVATE LANE' not in str(got)
    assert ch.downloads == ['BasicCompanyData-2026-09-01-part1_2.zip', 'BasicCompanyData-2026-09-01-part2_2.zip']
    assert not (tmp_path / 'scratch' / 'companies').exists()  # temp files deleted
    assert companies.status()['snapshot'] == '2026-09-01' and companies.status()['rows'] == 2


def test_a_new_snapshot_upserts_deletes_the_absent_and_runs_once(db, ch):
    ch.date = '2026-08-01'
    ch.snapshots[ch.date] = [[row('00000001', 'ONE LTD'), row('00000002', 'TWO LTD')]]
    companies.refresh(now=1788000000)
    ch.date = '2026-09-01'
    ch.snapshots[ch.date] = [[row('00000002', 'TWO RENAMED LTD', town='POOLE')], [row('00000003', 'THREE LTD')]]
    assert companies.refresh(now=1790000000) == {'snapshot': '2026-09-01', 'rows': 2, 'deleted': 1}
    got = table(db)
    assert sorted(got) == ['00000002', '00000003'] and got['00000002']['name'] == 'TWO RENAMED LTD'
    assert {r['snapshot'] for r in got.values()} == {'2026-09-01'}
    downloads, pages = len(ch.downloads), ch.pages
    assert companies.refresh(now=1790003600)['skipped']  # same month: no network at all
    assert (len(ch.downloads), ch.pages) == (downloads, pages)
    assert companies.refresh(now=1792000000)['skipped']  # next month, page still shows September: page only
    assert len(ch.downloads) == downloads and ch.pages == pages + 1


def test_only_one_worker_replica_ingests_at_a_time(db, ch):
    import os
    ch.date = '2026-09-01'
    ch.snapshots[ch.date] = [[row('00000001', 'ONE LTD')]]
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as other:
        other.execute('SELECT pg_advisory_lock(hashtext(%s))', ('reelsieve-companies-' + db.schema_name(),))
        assert companies.refresh(now=1790000000) == {'skipped': 'another worker is ingesting'}
    assert ch.pages == 0 and table(db) == {}
    assert companies.refresh(now=1790000000)['rows'] == 1


@pytest.mark.parametrize('damage', ['truncated', 'columns', 'empty', 'parts'])
def test_a_bad_snapshot_changes_nothing_and_leaves_no_files(db, ch, tmp_path, monkeypatch, damage):
    ch.date = '2026-08-01'
    ch.snapshots[ch.date] = [[row('00000001', 'ONE LTD')]]
    companies.refresh(now=1788000000)
    ch.date = '2026-09-01'
    ch.snapshots[ch.date] = [[row('00000002', 'TWO LTD')], [row('00000003', 'THREE LTD')]]
    if damage == 'truncated':
        original = companies._download

        def cut(url, dest):
            original(url, dest)
            dest.write_bytes(dest.read_bytes()[:-30])
        monkeypatch.setattr(companies, '_download', cut)
    elif damage == 'columns':
        monkeypatch.setattr(companies, '_download', lambda url, dest: snapshot_zip(dest, [row('00000002', 'TWO LTD')],
                                                                                    HEADER.replace('CompanyStatus', 'Status')))
    elif damage == 'empty':
        ch.snapshots[ch.date] = [[row('00000009', 'SHOP LTD', sic=(OTHER,))], []]
    else:
        monkeypatch.setattr(companies, '_get_page', lambda: page('2026-09-01', 3, listed=(1, 3)))
    out = companies.refresh(now=1790000000)
    assert 'error' in out
    assert sorted(table(db)) == ['00000001'] and companies.status()['snapshot'] == '2026-08-01'
    assert not (tmp_path / 'scratch' / 'companies').exists()


def test_the_download_page_names_the_snapshot_and_every_part():
    snap, urls = companies.latest(page('2026-09-01', 7))
    assert snap == '2026-09-01' and len(urls) == 7
    assert urls[0] == 'https://download.companieshouse.gov.uk/BasicCompanyData-2026-09-01-part1_7.zip'
    for bad in ('<html>maintenance</html>', page('2026-09-01', 7, listed=(1, 2, 3)),
                page('2026-09-01', 2) + page('2026-08-01', 2)):
        with pytest.raises(RuntimeError):
            companies.latest(bad)


def test_worker_checks_for_a_new_snapshot_hourly_next_to_retention(db, monkeypatch, tmp_path):
    from app import retention, worker
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(retention, 'run', lambda: calls.append('retention') or {})
    monkeypatch.setattr(companies, 'refresh', lambda: calls.append('companies') or {})
    monkeypatch.setattr(worker, '_last_purge', [0.0])
    worker.run_once('w')
    worker.run_once('w')
    assert calls == ['companies', 'retention']  # refresh never raises, so a failing retention cannot block it or vice versa


def test_a_failed_refresh_never_raises_into_the_worker(db, capsys):
    out = companies.refresh(now=1790000000)  # conftest refuses the network
    assert 'error' in out and '"companies"' in capsys.readouterr().out


# ---------------- search, queue, suppression ----------------

def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


def post(client, url, body=None):
    return client.post(url, json=body or {}, headers={'X-CSRF-Token': auth.csrf_token(client.cookies.get(auth.COOKIE))})


@pytest.fixture
def web(owners):
    auth.create_user(ADMIN, 'operator-password', 'admin')
    clients = {name: client_for(tok) for name, tok in {**owners, 'admin': auth.issue(ADMIN)[0]}.items()}
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def seed(db, rows):
    with db.connect() as c:
        for number, name, town, postcode, sic in rows:
            c.execute("INSERT INTO companies(company_number,name,town,postcode,country,sic_codes,incorporated,snapshot) "
                      "VALUES(%s,%s,%s,%s,'ENGLAND',%s,'2020-01-01','2026-09-01')", (number, name, town, postcode, sic))


SENDER = {'name': 'Priya Shah', 'business': 'Reel Studio', 'email': 'priya@reelstudio.test'}


def test_search_filters_by_town_postcode_area_and_category_and_pages(web, db):
    seed(db, [(f'1{n:07d}', f'BOURNEMOUTH LETS {n:02d} LTD', 'BOURNEMOUTH', f'BH1 {n % 9}AA', ['68320']) for n in range(30)] +
             [('20000001', 'POOLE STAYS LTD', 'POOLE', 'BH15 1AA', ['55209']),
              ('20000002', 'BIRMINGHAM LETS LTD', 'BIRMINGHAM', 'B1 1AA', ['68320', '55209'])])
    get = lambda q: web['alice'].get('/api/outreach/companies' + q).json()  # noqa: E731
    first = get('?place=bournemouth')
    assert first['total'] == 30 and first['pages'] == 2 and len(first['items']) == 25
    assert first['items'][0] == {'company_number': '10000000', 'name': 'BOURNEMOUTH LETS 00 LTD', 'town': 'Bournemouth',
                                 'category': 'Property management',
                                 'record_url': 'https://find-and-update.company-information.service.gov.uk/company/10000000',
                                 'search_url': 'https://www.google.com/search?q=%22BOURNEMOUTH%20LETS%2000%20LTD%22%20Bournemouth'}
    assert [i['name'] for i in get('?place=Bournemouth&page=2')['items']][-1] == 'BOURNEMOUTH LETS 29 LTD'
    assert get('?place=BH')['total'] == 31                  # postcode area: BH1 and BH15, not B1
    assert get('?place=bh15')['total'] == 1                 # district
    assert get('?place=BH15 1AA')['total'] == 1             # a full postcode searches its district
    assert get('?place=B')['total'] == 1
    assert [i['name'] for i in get('?category=short_stay')['items']] == ['BIRMINGHAM LETS LTD', 'POOLE STAYS LTD']
    assert get('?place=Birmingham')['items'][0]['category'] == 'Holiday and short-stay accommodation, property management'
    assert get('?place=Nowhere')['total'] == 0
    assert web['alice'].get('/api/outreach/companies?category=nope').status_code == 400
    assert TestClient(server.app).get('/api/outreach/companies').status_code == 401


def test_do_not_contact_hides_a_company_from_every_user_by_keyed_hash(web, db):
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1 1AA', ['68320']),
              ('00000002', 'HARBOUR STAYS LTD', 'BOURNEMOUTH', 'BH1 2AA', ['55209'])])
    assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': '00000001'}).status_code == 200
    assert [i['name'] for i in web['bob'].get('/api/outreach/companies?place=Bournemouth').json()['items']] == ['HARBOUR STAYS LTD']
    assert web['bob'].get('/api/outreach/companies?place=Bournemouth').json()['total'] == 1
    r = post(web['bob'], '/api/outreach/companies/queue', {'company_number': '00000001', 'template': 'Hi', 'sender': SENDER})
    assert r.status_code == 404 and store.outreach_rows(BOB) == []
    with db.connect() as c:
        rows = c.execute('SELECT * FROM outreach_suppressions').fetchall()
    assert len(rows) == 1 and set(rows[0]) == {'key', 'ts'} and '00000001' not in str(rows)
    assert rows[0]['key'] == hmac.new(store._suppression_key(), b'company:00000001', hashlib.sha256).hexdigest()
    assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': 'x; drop'}).status_code == 400


def test_do_not_contact_on_a_queued_company_row_suppresses_the_company(web, db):
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1 1AA', ['68320'])])
    rid = post(web['alice'], '/api/outreach/companies/queue', {'company_number': '00000001', 'template': 'Hi', 'sender': SENDER}).json()['id']
    assert post(web['alice'], '/api/outreach/suppress', {'id': rid}).status_code == 200
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 0 and store.outreach_rows(ALICE) == []


def test_queue_stores_the_company_number_and_name_and_a_complete_email(web, db):
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1 1AA', ['68320'])])
    r = post(web['alice'], '/api/outreach/companies/queue',
             {'company_number': '00000001', 'name': 'SPOOFED NAME', 'template': 'Hello {company} team, fancy a video?', 'sender': SENDER})
    assert r.status_code == 200 and r.json()['stats']['queued'] == 1
    (got,) = store.outreach_rows(ALICE)
    assert (got['channel'], got['name'], got['city']) == ('company', 'SEASIDE LETS LTD', None)
    assert got['url'] == 'https://find-and-update.company-information.service.gov.uk/company/00000001'
    assert got['meta'] == '{"company_number": "00000001"}' and 'BH1' not in str(got) and 'BOURNEMOUTH' not in str(got)
    assert got['message'].startswith('Hello SEASIDE LETS LTD team, fancy a video?')
    assert 'Priya Shah, Reel Studio' in got['message'] and 'priya@reelstudio.test' in got['message'] and 'unsubscribe' in got['message']
    assert 'SEASIDE LETS LTD' in web['alice'].get('/api/account/export').text  # tracker rows are in the account export
    page = web['alice'].get('/outreach').text
    assert 'SEASIDE LETS LTD' in page and '>Company<' in page and 'Companies House record ↗' in page
    for bad in ({'company_number': '00000001', 'template': 'Hi', 'sender': {**SENDER, 'name': ''}},
                {'company_number': '00000001', 'template': 'Hi', 'sender': {**SENDER, 'email': 'not-an-email'}}):
        assert post(web['alice'], '/api/outreach/companies/queue', bad).status_code == 400
    assert post(web['alice'], '/api/outreach/companies/queue', {'company_number': '99999999', 'template': 'Hi', 'sender': SENDER}).status_code == 404


def test_every_business_email_says_who_it_is_from_and_how_to_opt_out():
    for template in ('', 'Hi {company}', companies.TEMPLATE, 'Hi {company}. To stop, just say.'):
        msg = companies.compose(template, 'SEASIDE LETS LTD', SENDER)
        assert msg.rstrip().endswith(companies.footer('SEASIDE LETS LTD', SENDER))
        assert 'Priya Shah, Reel Studio\npriya@reelstudio.test' in msg and 'unsubscribe' in msg and '{' not in msg
    assert 'Priya Shah\npriya@' in companies.compose('Hi', 'X LTD', {**SENDER, 'business': ''})
    for bad in ({**SENDER, 'name': ' '}, {**SENDER, 'email': 'priya@'}, {}):
        with pytest.raises(ValueError):
            companies.compose('Hi', 'X LTD', bad)
    assert len(companies.compose('x' * 9000, 'X LTD', SENDER)) < 3000


def test_outreach_page_explains_when_business_emails_need_consent(web, db):
    page = web['alice'].get('/outreach').text
    card = page.split('id="companies-card"', 1)[1].split('</section>', 1)[0]
    assert 'UK property companies (business to business)' in card and 'no directors' in card.lower()
    notice = page.split('id="b2b-consent"', 1)[1].split('</p>', 1)[0]
    for phrase in ('limited company', 'Sole traders', 'agreed', 'opt out', 'say who you are'):
        assert phrase in notice, phrase
    assert companies.TEMPLATE.split('\n', 1)[0] in page and 'value="alice@example.test"' in page


def test_admin_settings_show_the_last_snapshot_and_row_count(web, db):
    assert 'Not loaded yet' in web['admin'].get('/settings').text
    seed(db, [('00000001', 'ONE LTD', 'LEEDS', 'LS1 1AA', ['68320']), ('00000002', 'TWO LTD', 'LEEDS', 'LS1 1AB', ['68320'])])
    with db.connect() as c:
        c.execute("INSERT INTO app_meta(key,value) VALUES('companies','{\"snapshot\": \"2026-09-01\", \"ingested_at\": 1790000000}')")
    card = web['admin'].get('/settings').text.split('id="companies-card"', 1)[1].split('</section>', 1)[0]
    assert '2026-09-01' in card and '2 companies' in card
    assert web['alice'].get('/settings', follow_redirects=False).status_code == 303
