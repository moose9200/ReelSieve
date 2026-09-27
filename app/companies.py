"""UK property companies from the official Companies House register: a lawful business-to-business prospect source.

Why limited companies and LLPs only, and never people (sources fetched 27 Sep 2026):
- PECR reg 22 (prior consent for marketing by electronic mail) protects "individual subscribers". Reg 23 applies to every
  recipient: the sender's identity must not be "disguised or concealed", and the message must give "a valid address to
  which the recipient of the communication may send a request that such communications cease".
  https://www.legislation.gov.uk/uksi/2003/2426/regulation/22 and /regulation/23
- ICO: "individual subscribers (people, sole traders, ordinary partnerships); and corporate subscribers (organisations with
  their own legal personality, for example limited companies, LLPs and Scottish partnerships)".
  https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/guidance-on-direct-marketing-using-electronic-mail/key-concepts-for-direct-marketing-using-electronic-mail/
- ICO: "You can send unsolicited electronic mail marketing to corporate subscribers without consent or a soft opt-in. You
  must not disguise or hide your identity in messages to either type of subscriber. You must provide a valid contact
  address for recipients to opt out or unsubscribe."
  https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/guidance-on-direct-marketing-using-electronic-mail/how-do-we-comply-with-the-pecr-electronic-mail-marketing-rules/
- ICO B2B guidance: "you should comply with a corporate subscriber's opt-out request", and a named person's details are
  personal data even in a business context.
  https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/business-to-business-marketing/
Hence: bodies corporate only (CORPORATE), company-level fields only, every email carries the sender and an opt-out line
that cannot be edited away (compose), and "Do not contact" hides a company from every user (store.suppress).

Source: the Free Company Data Product, https://download.companieshouse.gov.uk/en_output.html ("The latest snapshot will be
updated within 5 working days of the previous month end"), files BasicCompanyData-YYYY-MM-01-partN_M.zip, one CSV each;
column names read from the 2026-09-01 file. Reuse: Companies House FOI 158/08/14 says the register is made available under
Companies Act 2006 s1085-1086, not the Open Government Licence.
SIC codes: the Companies House condensed SIC 2007 list, https://resources.companieshouse.gov.uk/sic/
"""
import csv
import datetime as dt
import io
import json
import re
import shutil
import time
import zipfile
from urllib.parse import quote

import httpx
from psycopg.types.json import Jsonb

from app import database, store

BASE = 'https://download.companieshouse.gov.uk/'
PAGE = BASE + 'en_output.html'
RECORD = 'https://find-and-update.company-information.service.gov.uk/company/'
CATEGORIES = {  # label, condensed SIC 2007 codes
    'short_stay': ('holiday and short-stay accommodation', {'55201', '55209'}),  # Holiday centres and villages; Other holiday and other collective accommodation
    'management': ('property management', {'68320'}),                           # Management of real estate on a fee or contract basis
}
SIC = set().union(*(codes for _, codes in CATEGORIES.values()))
# Corporate subscribers only. Left out: Limited Partnership (no legal personality in England, Wales and Northern Ireland),
# Overseas Entity, charities and "Other company type".
CORPORATE = {'Private Limited Company', 'Public Limited Company', 'Old Public Company', 'Limited Liability Partnership',
             'Private Unlimited Company', 'Private Unlimited', 'Community Interest Company',
             'PRI/LTD BY GUAR/NSC (Private, limited by guarantee, no share capital)',
             "PRI/LBG/NSC (Private, Limited by guarantee, no share capital, use of 'Limited' exemption)"}
NEEDED = {'CompanyName', 'CompanyNumber', 'RegAddress.PostTown', 'RegAddress.County', 'RegAddress.Country',
          'RegAddress.PostCode', 'CompanyCategory', 'CompanyStatus', 'CountryOfOrigin', 'IncorporationDate',
          *(f'SICCode.SicText_{i}' for i in range(1, 5))}
NUMBER = re.compile(r'[A-Z0-9]{8}')
POSTCODE = re.compile(r'[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}')
PAGE_SIZE = 25
TEMPLATE = ('Hello {company} team,\n\n'
            'I make short cinematic walkthrough videos for holiday lets and serviced apartments, built from the photos '
            'already on a listing. I would be glad to make one for one of your properties free of charge, so you can see '
            'whether it is useful.\n\n'
            'If that sounds helpful, just reply to this email.')


# ---------------- monthly ingestion (worker) ----------------

def _get_page():
    r = httpx.get(PAGE, timeout=30)
    r.raise_for_status()
    return r.text


def _download(url, dest, limit=2 << 30):
    """Stream one snapshot part to disk: a zip's index is at its end, so it cannot be read straight off the socket."""
    size = 0
    with httpx.stream('GET', url, timeout=60) as r, open(dest, 'wb') as f:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            size += len(chunk)
            if size > limit:
                raise RuntimeError('A snapshot part is far larger than expected')
            f.write(chunk)


def latest(page):
    """(snapshot date, every part's URL) from the download page. Refuses a page that does not list every part."""
    parts = set(re.findall(r'BasicCompanyData-(\d{4}-\d{2}-\d{2})-part(\d+)_(\d+)\.zip', page))
    dates, totals = {p[0] for p in parts}, {int(p[2]) for p in parts}
    if len(dates) != 1 or len(totals) != 1:
        raise RuntimeError('Could not read one snapshot from the Companies House download page')
    date, total = dates.pop(), totals.pop()
    if sorted(int(p[1]) for p in parts) != list(range(1, total + 1)):
        raise RuntimeError('The Companies House download page does not list every part of the snapshot')
    return date, [f'{BASE}BasicCompanyData-{date}-part{n}_{total}.zip' for n in range(1, total + 1)]


def _keep(rec, ix, snapshot):
    """The stored fields of an active UK corporate property company, else None.
    Never a person's name, a street address or a "care of" line: those columns are not read."""
    get = lambda k: rec[ix[k]].strip() if ix[k] < len(rec) else ''  # noqa: E731
    if get('CompanyStatus') != 'Active' or get('CountryOfOrigin') != 'United Kingdom' or get('CompanyCategory') not in CORPORATE:
        return None
    sic = [s[:5] for s in (get(f'SICCode.SicText_{i}') for i in range(1, 5)) if re.match(r'\d{5}\b', s)]
    number = get('CompanyNumber').upper()
    if not SIC & set(sic) or not NUMBER.fullmatch(number):
        return None
    try:
        incorporated = dt.datetime.strptime(get('IncorporationDate'), '%d/%m/%Y').date()
    except ValueError:
        incorporated = None
    pc = re.sub(r'\s+', '', get('RegAddress.PostCode').upper())
    pc = f'{pc[:-3]} {pc[-3:]}' if POSTCODE.fullmatch(pc) else pc
    return (number, get('CompanyName')[:160], (get('RegAddress.PostTown') or get('RegAddress.County'))[:50] or None,
            pc[:10] or None, get('RegAddress.Country')[:50] or None, sic, incorporated, snapshot)


def rows(path, snapshot):
    """Matching companies in one snapshot zip, read one CSV row at a time (constant memory for any file size).
    A truncated or corrupt zip raises (zipfile checks each member's CRC), so nothing half-read is ever kept."""
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():
            if not member.lower().endswith('.csv'):
                continue
            with z.open(member) as raw:
                reader = csv.reader(io.TextIOWrapper(raw, encoding='utf-8', errors='replace', newline=''))
                head = [h.strip() for h in next(reader, [])]
                if NEEDED - set(head):
                    raise RuntimeError('Companies House changed the snapshot columns: ' + ', '.join(sorted(NEEDED - set(head))))
                ix = {h: i for i, h in enumerate(head)}
                for rec in reader:
                    row = _keep(rec, ix, snapshot)
                    if row:
                        yield row


def meta(conn=None):
    """The last snapshot ingested: {'snapshot', 'ingested_at', 'rows'}, or {} before the first."""
    with database.transaction(conn) as c:
        row = c.execute("SELECT value FROM app_meta WHERE key='companies'").fetchone()
    return row['value'] if row else {}


UPSERT = ('INSERT INTO companies SELECT DISTINCT ON (company_number) * FROM companies_new ORDER BY company_number '
          'ON CONFLICT (company_number) DO UPDATE SET name=EXCLUDED.name,town=EXCLUDED.town,postcode=EXCLUDED.postcode,'
          'country=EXCLUDED.country,sic_codes=EXCLUDED.sic_codes,incorporated=EXCLUDED.incorporated,snapshot=EXCLUDED.snapshot')


def ingest(now=None):
    """Load a new monthly snapshot, once, on one worker replica. One transaction: web readers see the old companies until
    the new ones are complete, and any failure (download, zip, columns, empty result) leaves the table as it was."""
    from app.worker import root
    now = now or time.time()
    with database.connect() as c:
        if not c.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok',
                         ('reelsieve-companies-' + database.schema_name(),)).fetchone()['ok']:
            return {'skipped': 'another worker is ingesting'}
        done = meta(c).get('snapshot')
        if done == time.strftime('%Y-%m-01', time.gmtime(now)):
            return {'skipped': 'up to date', 'snapshot': done}  # snapshots are dated the 1st: nothing newer this month
        snapshot, urls = latest(_get_page())
        if snapshot == done:
            return {'skipped': 'up to date', 'snapshot': done}
        scratch = root() / 'companies'
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        try:
            c.execute('CREATE TEMP TABLE companies_new (LIKE companies) ON COMMIT DROP')
            with c.cursor().copy('COPY companies_new FROM STDIN') as copy:
                for url in urls:  # one part (about 70 MB) on disk at a time
                    part = scratch / 'part.zip'
                    _download(url, part)
                    for row in rows(part, snapshot):
                        copy.write_row(row)
                    part.unlink()
            kept = c.execute(UPSERT).rowcount
            if not kept:
                raise RuntimeError('No matching companies in the snapshot, so nothing was changed')
            deleted = c.execute('DELETE FROM companies WHERE snapshot<>%s', (snapshot,)).rowcount
            c.execute("INSERT INTO app_meta(key,value) VALUES('companies',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                      (Jsonb({'snapshot': snapshot, 'ingested_at': now, 'rows': kept}),))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return {'snapshot': snapshot, 'rows': kept, 'deleted': deleted}


def refresh(now=None):
    """The worker's hourly call. Never raises (it retries next hour); prints one JSON line of counts when it did something."""
    try:
        out = ingest(now)
    except Exception as e:  # noqa: BLE001 - a failed refresh must never stop the worker claiming jobs
        out = {'error': str(e)[:200] if isinstance(e, RuntimeError) else type(e).__name__}
    if 'skipped' not in out:
        print(json.dumps({'companies': out}), flush=True)
    return out


def status():
    """Admin settings: the last snapshot ingested, when, and how many companies are held now."""
    with database.connect() as c:
        return {**meta(c), 'rows': c.execute('SELECT count(*) AS n FROM companies').fetchone()['n']}


# ---------------- search, queue (web) ----------------

def number(value):
    n = str(value or '').strip().upper()
    return n if NUMBER.fullmatch(n) else None


def _pads():
    k = store._suppression_key().ljust(64, b'\0')
    return bytes(b ^ 0x36 for b in k), bytes(b ^ 0x5C for b in k)


# HMAC-SHA256 (RFC 2104) of 'company:<number>' spelt out with PostgreSQL's sha256(), equal to store.suppression_keys (tested),
# so companies someone asked us not to contact are left out before counting and paging.
UNSUPPRESSED = ("NOT EXISTS (SELECT 1 FROM outreach_suppressions s WHERE s.key=encode(sha256(%(opad)s || "
                "sha256(%(ipad)s || convert_to('company:' || c.company_number, 'UTF8'))), 'hex'))")


def search(place='', category='', page=1):
    """Companies by town, postcode area (BH) or district (BH1, or a full postcode), and category; PAGE_SIZE a page."""
    ipad, opad = _pads()
    where, args = [UNSUPPRESSED], {'ipad': ipad, 'opad': opad}
    p = ' '.join(str(place or '').upper().split())[:60]
    district = re.fullmatch(r'([A-Z]{1,2}[0-9][A-Z0-9]?)(?: ?[0-9][A-Z]{2})?', p)
    if re.fullmatch(r'[A-Z]{1,2}', p):
        where.append('c.postcode ~ %(pc)s')
        args['pc'] = f'^{p}[0-9]'
    elif district:
        where.append('c.postcode LIKE %(pc)s')
        args['pc'] = district.group(1) + ' %'
    elif p:
        where.append('upper(c.town)=%(town)s')
        args['town'] = p
    if category:
        if category not in CATEGORIES:
            raise ValueError('Unknown category')
        where.append('c.sic_codes && %(sic)s')
        args['sic'] = sorted(CATEGORIES[category][1])
    page = max(1, min(int(page or 1), 10000))
    frm = ' FROM companies c WHERE ' + ' AND '.join(where)
    with database.connect() as c:
        total = c.execute('SELECT count(*) AS n' + frm, args).fetchone()['n']
        found = c.execute('SELECT company_number,name,town,sic_codes' + frm + ' ORDER BY name,company_number LIMIT %(lim)s OFFSET %(off)s',
                          {**args, 'lim': PAGE_SIZE, 'off': (page - 1) * PAGE_SIZE}).fetchall()
    return {'items': [_item(r) for r in found], 'total': total, 'page': page, 'pages': max(1, -(-total // PAGE_SIZE))}


def _item(r):
    town = (r['town'] or '').title()
    labels = ', '.join(label for label, codes in CATEGORIES.values() if codes & set(r['sic_codes']))
    return {'company_number': r['company_number'], 'name': r['name'], 'town': town, 'category': labels[:1].upper() + labels[1:],
            'record_url': RECORD + r['company_number'],
            'search_url': 'https://www.google.com/search?q=' + quote(f'"{r["name"]}" {town}'.strip())}


def _sender(sender):
    s = sender if isinstance(sender, dict) else {}
    name, business, email = (' '.join(str(s.get(k) or '').split())[:120] for k in ('name', 'business', 'email'))
    if not name or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        raise ValueError('Add your name and a reply email first: every business email must say who it is from.')
    return name, business, email


def footer(company, sender):
    name, business, email = _sender(sender)
    return (f'{name}{", " + business if business else ""}\n{email}\n\n'
            f'You are receiving this because {company} is registered at Companies House as a property business. '
            f'If you would rather not hear from me again, reply "unsubscribe" or email {email} and I will not contact you again.')


def compose(template, company, sender):
    """The email as the user will send it: their words, then who it is from and how to opt out (PECR reg 23).
    The footer is added here, so no edit to the template can remove it."""
    body = str(template or '').replace('{company}', company)[:2000].strip()
    return (body + '\n\n' if body else '') + footer(company, sender)


def queue(user, company_number, template, sender):
    """Add a company to the user's tracker: its number and registered name (from the register, not the browser), and the email."""
    n = number(company_number)
    _sender(sender)  # a missing sender is refused before anything is looked up
    with database.connect() as c:
        co = c.execute('SELECT name FROM companies WHERE company_number=%s', (n,)).fetchone() if n else None
    if not co or not store.unsuppressed([{'company_number': n}]):
        raise LookupError('That company is not in the register snapshot, or has asked not to be contacted')
    return store.add_outreach(user, 'company', co['name'], RECORD + n, None, compose(template, co['name'], sender),
                              meta={'company_number': n})
