"""UK property companies from the Companies House Free Company Data Product (business to business).
Synthetic zips only: no test downloads the real snapshot (conftest refuses the network for app.companies)."""
import hashlib
import hmac
import json
import threading
import time
import zipfile

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app import auth, companies, server, store

ALICE, BOB, ADMIN = 'alice@example.test', 'bob@example.test', 'operator@example.test'
REAL_GET_PAGE, REAL_DOWNLOAD = companies._get_page, companies._download  # before conftest refuses the network
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
    assert got['OC000002']['postcode_district'] == 'BH15' and got['OC000002']['sic_codes'] == ['47990', '55209']
    assert got['00000001']['postcode_district'] == 'BH1' and got['00000001']['snapshot'] == '2026-09-01'
    assert set(got['00000001']) == {'company_number', 'name', 'town', 'postcode_district', 'sic_codes', 'snapshot', 'suppression_key'}
    assert 'JOHN SMITH' not in str(got) and 'PRIVATE LANE' not in str(got) and '1AA' not in str(got)  # outward code only
    assert 'ENGLAND' not in str(got) and '2024' not in str(got)       # no country or incorporation date either
    assert got['00000001']['suppression_key'] == store.suppression_keys({'company_number': '00000001'})[0]  # keyed at ingest
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
    for t in threading.enumerate():
        if t.name == 'companies-refresh':
            t.join(5)
    assert sorted(calls) == ['companies', 'retention']  # once an hour; the load runs in its own thread


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
        for number, name, town, district, sic in rows:
            c.execute("INSERT INTO companies(company_number,name,town,postcode_district,sic_codes,snapshot) "
                      "VALUES(%s,%s,%s,%s,%s,'2026-09-01')", (number, name, town, district, sic))


SENDER = {'name': 'Priya Shah', 'business': 'Reel Studio', 'email': 'priya@reelstudio.test'}


def test_search_filters_by_town_postcode_area_and_category_and_pages(web, db):
    seed(db, [(f'1{n:07d}', f'BOURNEMOUTH LETS {n:02d} LTD', 'BOURNEMOUTH', 'BH1', ['68320']) for n in range(30)] +
             [('20000001', 'POOLE STAYS LTD', 'POOLE', 'BH15', ['55209']),
              ('20000002', 'BIRMINGHAM LETS LTD', 'BIRMINGHAM', 'B1', ['68320', '55209'])])
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
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1', ['68320']),
              ('00000002', 'HARBOUR STAYS LTD', 'BOURNEMOUTH', 'BH1', ['55209'])])
    assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': '00000001'}).status_code == 200
    assert [i['name'] for i in web['bob'].get('/api/outreach/companies?place=Bournemouth').json()['items']] == ['HARBOUR STAYS LTD']
    assert web['bob'].get('/api/outreach/companies?place=Bournemouth').json()['total'] == 1
    r = post(web['bob'], '/api/outreach/companies/queue', {'company_number': '00000001', 'template': 'Hi', 'sender': SENDER})
    assert r.status_code == 404 and store.outreach_rows(BOB) == []
    with db.connect() as c:
        rows = c.execute('SELECT * FROM outreach_suppressions').fetchall()
    assert len(rows) == 1 and set(rows[0]) == {'key', 'ts', 'owner_id'} and '00000001' not in str(rows)
    assert rows[0]['key'] == hmac.new(store._suppression_key(), b'company:00000001', hashlib.sha256).hexdigest()
    assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': 'x; drop'}).status_code == 400


def test_do_not_contact_on_a_queued_company_row_suppresses_the_company(web, db):
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1', ['68320'])])
    rid = post(web['alice'], '/api/outreach/companies/queue', {'company_number': '00000001', 'template': 'Hi', 'sender': SENDER}).json()['id']
    assert post(web['alice'], '/api/outreach/suppress', {'id': rid}).status_code == 200
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 0 and store.outreach_rows(ALICE) == []


def test_queue_stores_the_company_number_and_name_and_a_complete_email(web, db):
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1', ['68320'])])
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
    assert 'UK property companies (business to business)' in card and 'no officer or shareholder details' in card
    notice = page.split('id="b2b-consent"', 1)[1].split('</p>', 1)[0]
    for phrase in ('limited company', 'Sole traders', 'agreed', 'opt out', 'say who you are'):
        assert phrase in notice, phrase
    assert companies.TEMPLATE.split('\n', 1)[0] in page and 'value="alice@example.test"' in page


def test_admin_settings_show_the_last_snapshot_and_row_count(web, db):
    assert 'Not loaded yet' in web['admin'].get('/settings').text
    seed(db, [('00000001', 'ONE LTD', 'LEEDS', 'LS1', ['68320']), ('00000002', 'TWO LTD', 'LEEDS', 'LS1', ['68320'])])
    with db.connect() as c:
        c.execute("INSERT INTO app_meta(key,value) VALUES('companies','{\"snapshot\": \"2026-09-01\", \"ingested_at\": 1790000000}')")
    card = web['admin'].get('/settings').text.split('id="companies-card"', 1)[1].split('</section>', 1)[0]
    assert '2026-09-01' in card and '2 companies' in card and 'failed' not in card
    with db.connect() as c:
        companies._set_meta(c, 'companies_failed', {'snapshot': '2026-10-01', 'failed_at': 1791000000, 'error': 'Columns changed'})
    card = web['admin'].get('/settings').text.split('id="companies-card"', 1)[1].split('</section>', 1)[0]
    assert 'The snapshot of 2026-10-01 failed to load' in card and 'Columns changed' in card
    assert web['alice'].get('/settings', follow_redirects=False).status_code == 303


# ---------------- review fixes (27 Sep 2026) ----------------

DAY = 86400


def owner_of(db, email):
    with db.connect() as c:
        return c.execute('SELECT id FROM users WHERE email=%s', (email,)).fetchone()['id']


def suppressions(db):
    with db.connect() as c:
        return c.execute('SELECT * FROM outreach_suppressions ORDER BY ts').fetchall()


def test_do_not_contact_from_search_needs_a_real_company_records_who_and_is_capped_per_day(web, db, monkeypatch):
    seed(db, [(f'1{n:07d}', f'LETS {n:02d} LTD', 'BOURNEMOUTH', 'BH1', ['68320']) for n in range(4)])
    assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': 'ZZ000000'}).status_code == 404
    assert suppressions(db) == []                                     # made-up numbers are never stored
    monkeypatch.setattr(store, 'DAILY_SUPPRESSIONS', 2)
    for n in range(2):
        assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': f'1{n:07d}'}).status_code == 200
    r = post(web['alice'], '/api/outreach/companies/suppress', {'company_number': '10000002'})
    assert r.status_code == 429 and 'tomorrow' in r.json()['detail']
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 2
    assert [s['owner_id'] for s in suppressions(db)] == [owner_of(db, ALICE)] * 2
    rid = post(web['alice'], '/api/outreach/companies/queue', {'company_number': '10000003', 'template': 'Hi', 'sender': SENDER}).json()['id']
    assert post(web['alice'], '/api/outreach/suppress', {'id': rid}).status_code == 429  # the tracker path shares the cap
    assert len(store.outreach_rows(ALICE)) == 1                       # and keeps the row so it can be marked tomorrow
    assert post(web['bob'], '/api/outreach/companies/suppress', {'company_number': '10000002'}).status_code == 200  # per account


def test_the_operator_can_undo_one_accounts_do_not_contact_marks_but_never_real_objections(web, db):
    from app import admin
    seed(db, [('00000001', 'ONE LTD', 'LEEDS', 'LS1', ['68320']), ('00000002', 'TWO LTD', 'LEEDS', 'LS1', ['68320'])])
    assert post(web['alice'], '/api/outreach/companies/suppress', {'company_number': '00000001'}).status_code == 200
    store.suppress({'company_number': '00000002'})                    # an objection through the privacy form: no account
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 0
    assert admin.main(['unsuppress', ALICE]) == 0
    assert [i['name'] for i in web['bob'].get('/api/outreach/companies').json()['items']] == ['ONE LTD']
    assert len(suppressions(db)) == 1 and suppressions(db)[0]['owner_id'] is None
    assert 'unsuppress' in [e['action'] for e in store.admin_events()]


def test_who_marked_do_not_contact_is_exported_and_forgotten_on_erasure_and_after_90_days(web, db):
    from app import admin, retention
    seed(db, [('00000001', 'ONE LTD', 'LEEDS', 'LS1', ['68320']), ('00000002', 'TWO LTD', 'LEEDS', 'LS1', ['68320'])])
    post(web['alice'], '/api/outreach/companies/suppress', {'company_number': '00000001'})
    marks = web['alice'].get('/api/account/export').json()['outreach_suppressions']
    assert len(marks) == 1 and set(marks[0]) == {'ts'}               # when, never the keyed hash about someone else
    retention.run(now=time.time() + 91 * DAY)
    assert [s['owner_id'] for s in suppressions(db)] == [None]        # the objection stays; who marked it goes
    post(web['alice'], '/api/outreach/companies/suppress', {'company_number': '00000002'})
    admin.erase(ALICE)
    assert [s['owner_id'] for s in suppressions(db)] == [None, None]
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 0


def test_do_not_contact_holds_for_companies_loaded_later_and_after_a_new_session_secret(web, db, monkeypatch):
    store.suppress({'company_number': '00000003'})                    # objected before the company was loaded
    seed(db, [('00000001', 'ONE LTD', 'LEEDS', 'LS1', ['68320'])])
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 1
    seed(db, [('00000003', 'THREE LTD', 'LEEDS', 'LS1', ['68320'])])  # arrives in a later snapshot
    assert [i['name'] for i in web['bob'].get('/api/outreach/companies').json()['items']] == ['ONE LTD']
    with db.connect() as c:
        keyed = {r['company_number']: r['suppression_key'] for r in c.execute('SELECT * FROM companies').fetchall()}
    assert keyed['00000003'] == store.suppression_keys({'company_number': '00000003'})[0]
    monkeypatch.setenv('SESSION_SECRET', 'a-rotated-synthetic-session-secret-only')
    store.suppress({'company_number': '00000001'})                    # a new objection under the new key
    assert '00000001' not in [i['company_number'] for i in companies.search()['items']]  # rows were re-keyed


def test_a_page_past_the_end_shows_the_last_page(web, db):
    seed(db, [(f'1{n:07d}', f'LETS {n:02d} LTD', 'BOURNEMOUTH', 'BH1', ['68320']) for n in range(30)])
    got = web['alice'].get('/api/outreach/companies?place=Bournemouth&page=400').json()
    assert (got['page'], got['pages'], len(got['items']), got['total']) == (2, 2, 5, 30)
    js = open('app/static/app.js', encoding='utf-8').read()
    assert 'b2bQuery' in js  # Previous/Next page through the query that produced the results, not the edited boxes


def test_a_snapshot_that_fails_is_retried_after_a_day_not_every_hour(db, ch):
    ch.date = '2026-09-01'
    ch.snapshots[ch.date] = [[row('00000009', 'SHOP LTD', sic=(OTHER,))], []]  # no property company: fails every time
    assert 'error' in companies.refresh(now=1790000000)
    assert companies.refresh(now=1790003600)['skipped'] and companies.refresh(now=1790007200)['skipped']
    assert len(ch.downloads) == 2                                     # one attempt, not one an hour
    assert companies.status()['failed']['snapshot'] == '2026-09-01'
    ch.snapshots[ch.date] = [[row('00000001', 'ONE LTD')], []]
    assert companies.refresh(now=1790000000 + DAY + 60)['rows'] == 1  # tried again a day later
    assert 'failed' not in companies.status()


def test_an_older_snapshot_on_the_page_is_never_loaded_over_a_newer_one(db, ch):
    ch.date = '2026-09-01'
    ch.snapshots[ch.date] = [[row('00000001', 'ONE LTD'), row('00000002', 'TWO LTD')]]
    companies.refresh(now=1790000000)
    ch.date = '2026-08-01'
    ch.snapshots[ch.date] = [[row('00000001', 'ONE LTD')]]
    assert companies.refresh(now=1791500000)['skipped']
    assert sorted(table(db)) == ['00000001', '00000002'] and companies.status()['snapshot'] == '2026-09-01'


def test_the_monthly_load_runs_beside_job_claims_not_in_front_of_them(db, monkeypatch, tmp_path):
    from app import jobs, retention, worker
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path))
    release, started, loads, claims = threading.Event(), threading.Event(), [], []
    monkeypatch.setattr(companies, 'refresh', lambda: loads.append(1) or started.set() or release.wait(10))
    monkeypatch.setattr(retention, 'run', lambda: {})
    monkeypatch.setattr(jobs, 'claim', lambda *a: claims.append(1))
    monkeypatch.setattr(worker, '_last_purge', [0.0])
    try:
        worker.run_once('w')
        assert started.wait(5) and claims == [1]                      # a job was claimed while the load still runs
        worker._last_purge[0] = 0.0
        worker.run_once('w')                                          # the next hourly tick starts no second load
        assert claims == [1, 1] and loads == [1]
    finally:
        release.set()


def test_the_sender_details_live_on_the_account_not_in_the_browser(web, db):
    from app import admin
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1', ['68320'])])
    assert 'rs-b2b' not in open('app/static/app.js', encoding='utf-8').read()  # nothing about the sender in local storage
    post(web['alice'], '/api/outreach/companies/queue', {'company_number': '00000001', 'template': 'Hi', 'sender': SENDER})
    mine, theirs = web['alice'].get('/outreach').text, web['bob'].get('/outreach').text
    for v in ('value="Priya Shah"', 'value="Reel Studio"', 'value="priya@reelstudio.test"'):
        assert v in mine and v not in theirs
    assert 'value="bob@example.test"' in theirs
    assert 'Priya Shah' in json.dumps(web['alice'].get('/api/account/export').json()['accounts'])
    alice = owner_of(db, ALICE)
    admin.erase(ALICE)
    with db.connect() as c:
        assert c.execute('SELECT b2b_sender FROM accounts WHERE owner_id=%s', (alice,)).fetchone()['b2b_sender'] is None


def test_a_company_can_object_through_the_public_privacy_form(web, db):
    import re
    seed(db, [('00000001', 'SEASIDE LETS LTD', 'BOURNEMOUTH', 'BH1', ['68320'])])
    anon = client_for()
    form = anon.get('/privacy/request').text
    assert 'Company number' in form
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', form).group(1)
    base = {'csrf': token, 'email': ALICE, 'type': 'objection', 'details': 'Please stop.', 'airbnb_profile': ''}
    assert anon.post('/privacy/request', data={**base, 'company_number': 'not a number'}).status_code == 400
    assert anon.post('/privacy/request', data={**base, 'company_number': ' 1 '}).status_code == 200  # leading zeros optional
    assert web['bob'].get('/api/outreach/companies').json()['total'] == 0
    with db.connect() as c:
        assert c.execute('SELECT company_number FROM privacy_requests').fetchone()['company_number'] == '00000001'
    assert 'Company 00000001' in web['admin'].get('/settings').text
    assert web['alice'].get('/api/account/export').json()['privacy_requests'][0]['company_number'] == '00000001'
    anon.__exit__(None, None, None)


def test_the_privacy_notice_covers_companies_house_data_and_how_to_object(web, db):
    notice = client_for().get('/privacy').text
    section = notice.split('<h2>If your company is on the Companies House register</h2>', 1)[1].split('<h2>', 1)[0]
    for phrase in ('Free Company Data Product', 'legitimate interests', 'monthly', 'company number', 'Object to outreach', 'BH1'):
        assert phrase.lower() in section.lower(), phrase
    assert 'Companies House' in notice.split('<h2>', 1)[0]            # in the stated scope
    assert 'Do not contact' in notice and 'business emails' in notice  # the new account data is listed
    page = web['alice'].get('/outreach').text
    assert 'no officer or shareholder details' in page and 'no directors' not in page
    consent = page.split('id="b2b-consent"', 1)[1].split('</p>', 1)[0]
    assert 'Gmail' in consent                                         # a personal address belongs to an individual
    assert 'Business name (required if you are writing for a business)' in page


# ---------------- the real network code, against a mock transport ----------------

@pytest.fixture
def mock_ch(monkeypatch, tmp_path):
    """httpx.get and httpx.stream answered by a MockTransport: set .routes[path] = httpx.Response."""
    class Fake:
        routes = {}
    fake = Fake()
    transport = httpx.MockTransport(lambda req: fake.routes.get(req.url.path) or httpx.Response(404))
    monkeypatch.setattr(httpx, 'get', lambda url, **kw: httpx.Client(transport=transport).get(url, **kw))
    monkeypatch.setattr(httpx, 'stream', lambda method, url, **kw: httpx.Client(transport=transport).stream(method, url, **kw))
    monkeypatch.setattr(companies, '_get_page', REAL_GET_PAGE)
    monkeypatch.setattr(companies, '_download', REAL_DOWNLOAD)
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    return fake


def test_download_streams_to_disk_caps_the_size_and_refuses_errors_and_redirects(mock_ch, tmp_path):
    body = b'PK' + b'x' * 5000
    mock_ch.routes['/ok.zip'] = httpx.Response(200, content=body)
    mock_ch.routes['/moved.zip'] = httpx.Response(302, headers={'Location': companies.BASE + 'ok.zip'})
    mock_ch.routes['/en_output.html'] = httpx.Response(200, text=page('2026-09-01', 7))
    companies._download(companies.BASE + 'ok.zip', tmp_path / 'a.zip')
    assert (tmp_path / 'a.zip').read_bytes() == body
    with pytest.raises(RuntimeError):
        companies._download(companies.BASE + 'ok.zip', tmp_path / 'b.zip', limit=1000)
    for bad in ('missing.zip', 'moved.zip'):
        with pytest.raises(httpx.HTTPStatusError):
            companies._download(companies.BASE + bad, tmp_path / 'c.zip')
    assert companies.latest(companies._get_page())[0] == '2026-09-01'


def test_a_part_that_will_not_download_rolls_the_whole_load_back(db, mock_ch, tmp_path):
    good = tmp_path / 'part1.zip'
    snapshot_zip(good, [row('00000001', 'ONE LTD')])
    mock_ch.routes['/en_output.html'] = httpx.Response(200, text=page('2026-09-01', 2))
    mock_ch.routes['/BasicCompanyData-2026-09-01-part1_2.zip'] = httpx.Response(200, content=good.read_bytes())
    assert 'error' in companies.refresh(now=1790000000)                # part 2 answers 404
    assert table(db) == {} and companies.status().get('snapshot') is None
    assert not (tmp_path / 'scratch' / 'companies').exists()
