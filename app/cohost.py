"""Find and contact professional hosts / co-hosts in a city.

Airbnb's Co-Host Network profile pages are not served to logged-out fetches, so discovery has two sources:
  1. `network`  — the public Co-Host Network page for the city, when Airbnb serves it
  2. `operators`— derived from the city's own listings: the hosts running several listings (the people who
                  actually buy this), with their name, a listing and Airbnb's own contact-host URL
ReelSieve never sends these messages: each customer opens Airbnb's contact form in their own browser
and sends it themselves. Airbnb's Terms forbid unsolicited commercial messages — keep volume low,
make it relevant, stop if asked."""
import re,html,time,random
import httpx
from app import search as listing_search
UA=listing_search.UA
BASE='https://www.airbnb.co.uk'
def _slug(city):
    s=re.sub(r'\s*,\s*','-',city.strip().lower());s=re.sub(r'[^a-z0-9]+','-',s);return re.sub(r'-+','-',s).strip('-')
def _try_network(city):
    out=[]
    for u in (f'{BASE}/host/{_slug(city)}/co-hosts',f'https://www.airbnb.com/host/{_slug(city)}/co-hosts'):
        try:r=httpx.get(u,headers=UA,follow_redirects=True,timeout=25)
        except Exception:continue
        if r.status_code!=200 or 'co-host' not in r.text.lower():continue
        t=r.text
        for m in re.finditer(r'href="(/users/show/(\d+)[^"]*)"[^>]*>\s*([^<]{2,40})',t):
            out.append({'id':'u'+m.group(2),'name':html.unescape(m.group(3)).strip(),'url':BASE+m.group(1),'listings':None,'tagline':'Co-Host Network',
                        'profile_url':f'{BASE}/users/show/{m.group(2)}'})
        if out:break
    dedup={};[dedup.setdefault(o['url'],o) for o in out]
    return list(dedup.values())[:20]
def _listing_host(lid):
    try:
        t=httpx.get(f'{BASE}/rooms/{lid}',headers=UA,follow_redirects=True,timeout=30).text
    except Exception:return None
    u=html.unescape(t)
    name=(re.search(r'Hosted by ([A-Z][\w\'’-]{1,30})',u) or [None,''])[1]
    m=re.search(r'"listingsCount"\s*:\s*(\d+)',t) or re.search(r'(\d+)\s+listings?',u)
    n=int(m.group(1)) if m else None
    sup=bool(re.search(r'"isSuperhost":true',t))
    years=(re.search(r'(\d+)\s+years? hosting',u) or [None,''])[1]
    hid=re.search(r'"hostId"\s*:\s*"(\d{1,20})"',t)  # the listing's host (pdpContext); /users/show/<id> is their public profile
    return {'name':name,'listings':n,'superhost':sup,'years':years,'host_id':hid.group(1) if hid else None}
def _operators(city,limit=12):
    """Hosts in this city worth pitching: most-reviewed listings first, one row per host."""
    res=listing_search.search(city,None,None,2,pages=2)
    items=sorted(res.get('items',[]),key=lambda x:-((x.get('reviews') or 0)*(x.get('rating') or 0)))[:limit*2]
    out=[];seen=set()
    for it in items:
        if len(out)>=limit:break
        h=_listing_host(it['id']) or {}
        nm=(h.get('name') or '').strip()
        key=(nm.lower() or it['id'])
        if not nm or key in seen:continue
        seen.add(key)
        bits=[]
        if h.get('listings'):bits.append(f"{h['listings']} listings")
        if h.get('superhost'):bits.append('Superhost')
        if it.get('rating'):bits.append(f"{it['rating']}★ ({it.get('reviews')})")
        out.append({'id':it['id'],'name':nm,'url':f"{BASE}/contact_host/{it['id']}/send_message",'listing_url':it['url'],
                    'listing_title':it.get('name') or it.get('title') or '','listings':h.get('listings'),
                    'profile_url':f"{BASE}/users/show/{h['host_id']}" if h.get('host_id') else None,
                    'tagline':' · '.join(bits) or (it.get('title') or ''),'avatar':it.get('photo'),'city':city})
    return out
def discover(city,limit=12):
    net=_try_network(city)
    if net:
        for n in net:n.setdefault('city',city);n.setdefault('listing_title','')
        return {'city':city,'items':net[:limit],'source':'network','note':'From the Airbnb Co-Host Network page for this city.'}
    ops=_operators(city,limit)
    return {'city':city,'items':ops,'source':'operators',
            'note':"Airbnb doesn't serve the Co-Host Network page to logged-out requests, so these are the professional hosts running this city's best-reviewed listings — the same people the network lists. Each opens Airbnb's own contact form."}
def render(template,item):
    return (template or '').replace('{name}',item.get('name') or 'there').replace('{city}',item.get('city') or '')\
        .replace('{listing_title}',item.get('listing_title') or 'your listing').replace('{listings}',str(item.get('listings') or ''))
