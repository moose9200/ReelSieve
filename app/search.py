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
    # Airbnb serves several page variants: results live in `data-deferred-state`, or in `data-injector-instances` /
    # `data-initializer-bootstrap` JSON scripts. Walk every application/json script and collect StaySearchResult objects.
    scripts=re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',page_html,re.S);found=[]
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
import concurrent.futures as _cf
_cursor_cache={}
def _slug(location):
    slug=re.sub(r'\s*,\s*','--',location.strip());return re.sub(r'\s+','-',slug)
def _base_url(location,checkin,checkout,adults):
    q=f'adults={int(adults or 2)}'+(f'&checkin={checkin}&checkout={checkout}' if checkin and checkout else '')
    return f'{BASE}/s/{quote(_slug(location))}/homes?{q}'
def _fetch(url):
    r=httpx.get(url,headers=UA,follow_redirects=True,timeout=40);r.raise_for_status();return r
def _cursors(page_html):
    m=re.search(r'"pageCursors":\[([^\]]*)\]',page_html);return re.findall(r'"([A-Za-z0-9+/=]+)"',m.group(1)) if m else []
def price_value(p):
    m=re.search(r'(\d[\d,]*\.?\d*)',str(p or ''));return float(m.group(1).replace(',','')) if m else None
def _rank(items):
    """Default order: price high → low (no price last), ties by rating × reviews."""
    items.sort(key=lambda x:(-(price_value(x['price']) if price_value(x['price']) is not None else -1),-(x['reviews'] or 0)*(x['rating'] or 0)));return items
def search(location,checkin=None,checkout=None,adults=2,offset=0,pages=3):
    """First page + (pages-1) more via Airbnb's embedded pageCursors, fetched in parallel. Returns cursor count so the UI can load more."""
    base=_base_url(location,checkin,checkout,adults);r=_fetch(base);first=r.text;items=parse_results(first);cursors=_cursors(first)
    diag={'status':r.status_code,'bytes':len(first),'variant':('deferred' if 'data-deferred-state' in first else 'injector' if 'data-injector-instances' in first else 'unknown'),'title':(re.search(r'<title>([^<]{0,80})',first) or [None,None])[1],'final_url':str(r.url)}
    _cursor_cache[base]=cursors;seen={i['id'] for i in items}
    more=[c for c in cursors[1:max(1,int(pages))]]
    if more:
        with _cf.ThreadPoolExecutor(min(4,len(more))) as ex:
            for html_ in ex.map(lambda c:_fetch(base+'&cursor='+c).text,more):
                for it in parse_results(html_):
                    if it['id'] not in seen:seen.add(it['id']);items.append(it)
    _rank(items)
    return {'query':{'location':location,'checkin':checkin,'checkout':checkout,'adults':adults},'search_url':base,'count':len(items),'items':items,'pages_loaded':1+len(more),'pages_total':max(1,len(cursors)),'sort':'price_desc','diag':diag}
def search_page(location,checkin,checkout,adults,page):
    """One further page (0-based index into pageCursors) for 'Load more'."""
    base=_base_url(location,checkin,checkout,adults);cursors=_cursor_cache.get(base)
    if cursors is None:cursors=_cursors(_fetch(base).text);_cursor_cache[base]=cursors
    if page<=0 or page>=len(cursors):return {'items':[],'page':page,'pages_total':len(cursors)}
    items=_rank(parse_results(_fetch(base+'&cursor='+cursors[page]).text));return {'items':items,'page':page,'pages_total':len(cursors)}
if __name__=='__main__':
    import sys;print(json.dumps(search(*sys.argv[1:]),indent=1,ensure_ascii=False)[:3000])
