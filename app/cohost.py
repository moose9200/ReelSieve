"""Find and contact professional hosts / co-hosts in a city.

Airbnb's Co-Host Network profile pages are not served to logged-out fetches, so discovery has two sources:
  1. `network`  — the public Co-Host Network page for the city, when Airbnb serves it
  2. `operators`— derived from the city's own listings: the hosts running several listings (the people who
                  actually buy this), with their name, a listing and Airbnb's own contact-host URL
Sending is deliberately capped and never silent: the caller must pass confirm=True, each send is paced,
and anything that cannot be automated comes back as 'manual' with the URL so a human finishes it.
Airbnb's Terms forbid unsolicited commercial messages — keep volume low, make it relevant, stop if asked."""
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
            out.append({'id':'u'+m.group(2),'name':html.unescape(m.group(3)).strip(),'url':BASE+m.group(1),'listings':None,'tagline':'Co-Host Network'})
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
    return {'name':name,'listings':n,'superhost':sup,'years':years}
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
def send_one(url,message,confirm=False,timeout_ms=45000,shot=None):
    """Fill Airbnb's contact form in the user's own logged-in browser profile and, only with confirm=True, submit it.
    Returns (status, info): sent | draft | manual | failed. Never retries a send whose outcome is unknown."""
    from app import hostmsg
    from playwright.sync_api import sync_playwright
    with hostmsg._lock:
        with sync_playwright() as p:
            c=hostmsg._ctx(p,headless=True);pg=c.new_page()
            try:
                pg.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
                if '/login' in pg.url:return 'failed','Airbnb session expired — reconnect in Settings'
                box=None
                for sel in [pg.get_by_role('textbox',name=re.compile('message',re.I)),pg.locator('textarea')]:
                    try:
                        sel.first.wait_for(timeout=8000);box=sel.first;break
                    except Exception:continue
                if box is None:return 'manual','No message box found on this page — open it and send by hand'
                box.fill(message)
                if shot:
                    try:pg.screenshot(path=shot)
                    except Exception:pass
                if not confirm:return 'draft','filled, not sent'
                btn=None
                for sel in [pg.get_by_role('button',name=re.compile(r'^(send message|send)$',re.I)),pg.locator('button:has-text("Send")')]:
                    try:
                        sel.first.wait_for(timeout=6000);btn=sel.first;break
                    except Exception:continue
                if btn is None:return 'manual','No send button found — finish it by hand'
                btn.click()
                try:pg.wait_for_url(re.compile(r'/(messaging|inbox|guest/messages)'),timeout=25000);return 'sent',pg.url[:120]
                except Exception:
                    pg.wait_for_timeout(4000)
                    try:still=box.input_value().strip()==message.strip()
                    except Exception:still=False
                    if still:return 'failed','Send did not go through — Airbnb may have flagged or rate-limited the message'
                    return 'sent',pg.url[:120]
            except Exception as e:return 'failed',f'{type(e).__name__}: {str(e)[:140]}'
            finally:
                try:c.close()
                except Exception:pass
def send_batch(items,confirm=False,pace=(20,45),cb=None):
    """items: [{id,url,message}]. Paced, sequential, stops on the first hard failure so one flag doesn't become five."""
    results=[]
    for i,it in enumerate(items):
        st,info=send_one(it['url'],it['message'],confirm=confirm)
        results.append({'id':it.get('id'),'status':st,'info':info})
        if cb:cb(f"{it.get('name') or it.get('id')}: {st} — {info}")
        if st=='failed':break
        if i<len(items)-1:time.sleep(random.uniform(*pace))
    return results
