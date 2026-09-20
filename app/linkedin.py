"""LinkedIn prospecting for ReelSieve — compliant by design.

LinkedIn's User Agreement forbids scraping and automated messaging, and automation tools are the usual
cause of restricted accounts. So this module never touches LinkedIn: it builds the target list from the
Airbnb operators we can see legitimately, writes the words, and hands you a ready search link. You paste
and send. That keeps the account safe and the personalisation is what makes the reply rate anyway."""
import re
from urllib.parse import quote
from app import cohost
CONNECT_DEFAULT=("Hi {name} — I make short cinematic walkthrough videos for short-let listings from the photos that are "
                 "already on them. Made one for a {city} place this week. Happy to do one of yours free, no strings.")
FOLLOWUP_DEFAULT=("Thanks for connecting, {name}. I built a 60-second walkthrough of {company} from its own listing photos "
                  "and guest reviews — no filming, no shoot day. Want me to send it over? If it is useful, I do them at "
                  "volume for operators with several properties.")
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
    """Prospects = the professional hosts we can already see in that city, plus the LinkedIn search that finds them."""
    ops=cohost.discover(city,limit=limit).get('items',[])
    items=[]
    for o in ops:
        nm=(o.get('name') or '').strip()
        if not nm:continue
        items.append({'name':nm,'company':company_of(o),'city':city,'listings':o.get('listings'),
                      'url':search_url(f'{nm} {city}',role),'listing_url':o.get('listing_url') or o.get('url'),
                      'note':o.get('tagline') or ''})
    return {'items':items,'search_url':search_url(city,role),'source':'airbnb-operators',
            'note':"Names come from the listings themselves. Open the search, connect with a note, then send the follow-up after they accept. Nothing is sent automatically — LinkedIn's terms forbid that and it is how accounts get restricted."}
def render(template,item,limit=None):
    s=(template or '').replace('{name}',(item.get('name') or 'there').split()[0]).replace('{company}',item.get('company') or 'your properties').replace('{city}',item.get('city') or '')
    s=s.replace('{listings}',str(item.get('listings') or ''))
    return s[:limit] if limit else s
def csv_rows(rows):
    import io,csv
    b=io.StringIO();w=csv.writer(b)
    w.writerow(['when','channel','name','city','status','url','message','note','sent_at'])
    for r in rows:
        import datetime as dt
        when=dt.datetime.fromtimestamp(r.get('ts') or 0).strftime('%Y-%m-%d %H:%M')
        sent=dt.datetime.fromtimestamp(r['sent_at']).strftime('%Y-%m-%d %H:%M') if r.get('sent_at') else ''
        w.writerow([when,r.get('channel'),r.get('name'),r.get('city'),r.get('status'),r.get('url'),(r.get('message') or '').replace('\n',' '),r.get('note') or '',sent])
    return b.getvalue()
