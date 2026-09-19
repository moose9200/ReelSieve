"""In-app Airbnb listing search: parse the public search-results page (no login) so a listing can be picked inside the tool.
Verified 19 Sep 2026: /s/<location>/homes embeds a `data-deferred-state` JSON with `StaySearchResult` objects."""
import re,json,base64,html
from urllib.parse import quote
import httpx
UA={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36','Accept-Language':'en-GB,en;q=0.9'}
BASE='https://www.airbnb.co.uk'
def _walk(o,out):
    if isinstance(o,dict):
        if o.get('__typename')=='StaySearchResult':out.append(o)
        for v in o.values():_walk(v,out)
    elif isinstance(o,list):
        for v in o:_walk(v,out)
def _id(res):
    raw=(res.get('demandStayListing') or {}).get('id') or res.get('listingId') or ''
    try:dec=base64.b64decode(raw+'='*(-len(raw)%4)).decode()
    except Exception:dec=raw
    m=re.search(r'(\d{6,})',dec);return m.group(1) if m else None
def _text_list(lst):return ' · '.join(x.get('body') or x.get('text') or '' for x in (lst or []) if isinstance(x,dict) and (x.get('body') or x.get('text')))
def parse_results(page_html):
    scripts=re.findall(r'<script[^>]*id="data-deferred-state[^"]*"[^>]*>(.*?)</script>',page_html,re.S);found=[]
    for s in scripts:
        try:_walk(json.loads(s),found)
        except Exception:continue
    out=[];seen=set()
    for r in found:
        lid=_id(r)
        if not lid or lid in seen:continue
        seen.add(lid);pics=[p.get('picture') for p in (r.get('contextualPictures') or []) if isinstance(p,dict) and p.get('picture')]
        price=((r.get('structuredDisplayPrice') or {}).get('primaryLine') or {});pr=price.get('discountedPrice') or price.get('price') or price.get('accessibilityLabel') or ''
        rating=r.get('avgRatingLocalized') or '';m=re.match(r'([\d.]+)\s*\((\d+)\)',rating)
        sc=r.get('structuredContent') or {};loc=(r.get('demandStayListing') or {}).get('location',{}).get('coordinate') or {}
        out.append({'id':lid,'url':f'{BASE}/rooms/{lid}','title':html.unescape(r.get('title') or ''),'name':html.unescape(((r.get('nameLocalized') or {}).get('localizedStringWithTranslationPreference')) or r.get('subtitle') or ''),
                    'rating':float(m.group(1)) if m else None,'reviews':int(m.group(2)) if m else None,'price':pr,'price_qualifier':price.get('qualifier') or '',
                    'badges':[b.get('text') for b in (r.get('badges') or []) if isinstance(b,dict) and b.get('text')],'photo':pics[0] if pics else None,'photos':len(pics),
                    'summary':_text_list(sc.get('primaryLine')),'secondary':_text_list(sc.get('secondaryLine')),'lat':loc.get('latitude'),'lng':loc.get('longitude')})
    return out
def search(location,checkin=None,checkout=None,adults=2,offset=0):
    q=f'adults={int(adults or 2)}'+(f'&checkin={checkin}&checkout={checkout}' if checkin and checkout else '')+(f'&items_offset={int(offset)}' if offset else '')
    slug=re.sub(r'\s*,\s*','--',location.strip());slug=re.sub(r'\s+','-',slug);url=f'{BASE}/s/{quote(slug)}/homes?{q}';r=httpx.get(url,headers=UA,follow_redirects=True,timeout=40)
    r.raise_for_status();items=parse_results(r.text)
    items.sort(key=lambda x:(-(x['reviews'] or 0)*(x['rating'] or 0),-(x['photos'] or 0)))
    return {'query':{'location':location,'checkin':checkin,'checkout':checkout,'adults':adults,'offset':offset},'search_url':str(r.url),'count':len(items),'items':items}
if __name__=='__main__':
    import sys;print(json.dumps(search(*sys.argv[1:]),indent=1,ensure_ascii=False)[:3000])
