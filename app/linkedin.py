"""LinkedIn prospecting for ReelSieve — compliant by design.

LinkedIn's User Agreement (section 8.2) forbids scraping and automation, and its official APIs return only the
signed-in member's own profile, so this module never touches LinkedIn. Airbnb shows hosts by first name only, and
a first name plus a city cannot be matched to one verified person, so ReelSieve never presents a LinkedIn link as a
profile unless it really is one. What it can give exactly is the host's own Airbnb profile, from the listing page it
already reads. You search, check the match against that profile, paste and send."""
import csv
import datetime as dt
import io
import json
import re
from urllib.parse import quote
from app import cohost
from app.invoices import csv_cell
CONNECT_DEFAULT=("Hi {name} — I make short cinematic walkthrough videos for short-let listings from the photos that are "
                 "already on them. Made one for a {city} place this week. Happy to do one of yours free, no strings.")
FOLLOWUP_DEFAULT=("Thanks for connecting, {name}. I built a 60-second walkthrough of {company} from its own listing photos "
                  "and guest reviews — no filming, no shoot day. Want me to send it over? If it is useful, I do them at "
                  "volume for operators with several properties.")
AIRBNB_PROFILE=re.compile(r'https://www\.airbnb\.(?:co\.uk|com)/users/show/\d{1,20}')
LINKS=((re.compile(r'https://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?'),'LinkedIn profile'),
       (re.compile(r'https://www\.linkedin\.com/search/results/people/\?\S*'),'LinkedIn search'),
       (AIRBNB_PROFILE,'Airbnb profile'),
       (re.compile(r'https://www\.airbnb\.(?:co\.uk|com)/contact_host/\d{1,20}/send_message'),'Airbnb message form'),
       (re.compile(r'https://find-and-update\.company-information\.service\.gov\.uk/company/[A-Z0-9]{8}'),'Companies House record'))
LABELS={'LinkedIn profile':'LinkedIn profile ↗','LinkedIn search':'Search LinkedIn ↗','Airbnb profile':'Airbnb profile ↗',
        'Companies House record':'Companies House record ↗'}
def link_type(url):
    """What a stored link really is, judged by its exact shape."""
    return next((t for rx,t in LINKS if rx.fullmatch(url or '')),'Link' if url else '')
def link_label(url):
    return LABELS.get(link_type(url),'Open ↗')
def airbnb_profile(value):
    """The value only when it is exactly an Airbnb public profile URL, else ''."""
    return value if isinstance(value,str) and AIRBNB_PROFILE.fullmatch(value) else ''
def airbnb_profile_of(row):
    try:m=json.loads(row.get('meta') or '{}')
    except (TypeError,ValueError):m={}
    return airbnb_profile(m.get('airbnb_profile')) if isinstance(m,dict) else ''
def search_url(city,role='property manager'):
    q=f'{role} {city}'.strip()
    return 'https://www.linkedin.com/search/results/people/?keywords='+quote(q)
def company_of(item):
    """A usable company handle from listing branding, else the host's name."""
    t=(item.get('listing_title') or '').strip()
    m=re.search(r'\b(?:by|from|at)\s+([A-Z][\w&\'’-]+(?:\s+[A-Z][\w&\'’-]+){0,2})',t)
    if m:return m.group(1)
    m=re.match(r'([A-Z][\w&\'’-]+(?:\s+[A-Z][\w&\'’-]+){0,2})\s+[-–|]',t)
    if m:return m.group(1)
    return (item.get('name') or '').strip() or 'your properties'
def build(city,role='property manager',limit=10):
    """Prospects = the professional hosts we can already see in that city, a LinkedIn people search for each, and
    their exact Airbnb profile when the listing names it."""
    ops=cohost.discover(city,limit=limit).get('items',[])
    items=[]
    for o in ops:
        nm=(o.get('name') or '').strip()
        if not nm:continue
        url=search_url(f'{nm} {city}',role)
        items.append({'name':nm,'company':company_of(o),'city':city,'listings':o.get('listings'),
                      'url':url,'link_label':link_label(url),'airbnb_profile':airbnb_profile(o.get('profile_url')),
                      'listing_url':o.get('listing_url') or o.get('url'),'note':o.get('tagline') or ''})
    return {'items':items,'search_url':search_url(city,role),'source':'airbnb-operators',
            'note':"Airbnb shows hosts by first name only, so these are LinkedIn searches, not profiles. Check the person against their Airbnb profile before you connect. Nothing is sent automatically — LinkedIn's terms forbid that and it is how accounts get restricted."}
def render(template,item,limit=None):
    s=(template or '').replace('{name}',(item.get('name') or 'there').split()[0]).replace('{company}',item.get('company') or 'your properties').replace('{city}',item.get('city') or '')
    s=s.replace('{listings}',str(item.get('listings') or ''))
    return s[:limit] if limit else s
def csv_rows(rows):
    """Prospect names, notes and messages are other people's text, so every cell goes through the same OWASP
    formula guard the invoice CSV uses (app/invoices.py), quoted like it too."""
    b=io.StringIO();w=csv.writer(b,quoting=csv.QUOTE_ALL)
    w.writerow(['when','channel','name','city','status','link_type','link_url','airbnb_profile','message','note','sent_at'])
    for r in rows:
        when=dt.datetime.fromtimestamp(r.get('ts') or 0).strftime('%Y-%m-%d %H:%M')
        sent=dt.datetime.fromtimestamp(r['sent_at']).strftime('%Y-%m-%d %H:%M') if r.get('sent_at') else ''
        w.writerow([csv_cell(v) for v in (when,r.get('channel'),r.get('name'),r.get('city'),r.get('status'),link_type(r.get('url')),r.get('url'),
                    airbnb_profile_of(r),(r.get('message') or '').replace('\n',' '),r.get('note') or '',sent)])
    return b.getvalue()
