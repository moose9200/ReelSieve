"""Generic property-listing extractor: any listing URL that is not Airbnb.
Reads OpenGraph, JSON-LD and the raw <img> set from the public page, so Booking.com, VRBO, Rightmove,
Zillow, OnTheMarket and agents' own sites all yield a usable photo set. Returns the same shape as
pipeline.scrape_listing so the rest of the pipeline is unchanged."""
import re,html,json
from urllib.parse import urljoin,urlparse
import httpx
UA={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36','Accept-Language':'en-GB,en;q=0.9'}
BAD=re.compile(r'(sprite|logo|icon|favicon|avatar|placeholder|pixel|tracking|banner|badge|flag|star|map-?pin|1x1|blank)',re.I)
MIN_W=700
def _abs(base,u):
    u=(u or '').strip()
    if u.startswith('//'):return 'https:'+u
    return urljoin(base,u) if u else ''
def _jsonld(t):
    out=[]
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',t,re.S):
        try:
            d=json.loads(m.group(1).strip())
            out.extend(d if isinstance(d,list) else [d])
        except Exception:continue
    return out
def _meta(t,prop):
    m=re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',t,re.I)
    if not m:m=re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',t,re.I)
    return html.unescape(m.group(1)) if m else ''
def _size_hint(u):
    m=re.findall(r'(\d{3,4})\s*[x×]\s*(\d{3,4})',u)
    if m:return max(int(a) for a,_ in m)
    m=re.findall(r'[?&/](?:w|width|im_w|size)=?(\d{3,4})',u)
    return max(int(x) for x in m) if m else None
def _photos(t,base):
    urls=[]
    for m in re.finditer(r'<img[^>]+>',t,re.I):
        tag=m.group(0)
        src=(re.search(r'\bsrc=["\']([^"\']+)',tag) or re.search(r'\bdata-src=["\']([^"\']+)',tag) or [None,''])[1]
        srcset=(re.search(r'\bsrcset=["\']([^"\']+)',tag) or [None,''])[1]
        if srcset:
            cands=[c.strip().split()[0] for c in srcset.split(',') if c.strip()]
            if cands:src=cands[-1]
        if src:urls.append(_abs(base,src))
    for m in re.finditer(r'"(https?://[^"\s]+?\.(?:jpe?g|png|webp)(?:\?[^"\s]*)?)"',t):urls.append(m.group(1))
    og=_meta(t,'og:image')
    if og:urls.insert(0,_abs(base,og))
    seen=set();out=[]
    for u in urls:
        if not u or u.startswith('data:') or BAD.search(u):continue
        k=re.sub(r'[?#].*$','',u)
        if k in seen:continue
        w=_size_hint(u)
        if w is not None and w<MIN_W:continue
        seen.add(k);out.append({'label':'','url':u})
    return out[:60]
def scrape(url,cb=None):
    r=httpx.get(url,headers=UA,follow_redirects=True,timeout=45);r.raise_for_status();t=r.text;base=str(r.url)
    host=urlparse(base).netloc.replace('www.','')
    title=_meta(t,'og:title') or (re.search(r'<title>([^<]{0,140})',t) or [None,''])[1]
    desc=_meta(t,'og:description') or _meta(t,'description')
    city='';rating=None;count=None;guests=None
    for d in _jsonld(t):
        ty=str(d.get('@type','')).lower()
        if any(k in ty for k in ('lodging','hotel','apartment','house','product','residence','offer','realestate')):
            title=title or d.get('name') or ''
            desc=desc or d.get('description') or ''
            ad=d.get('address') or {}
            if isinstance(ad,dict):city=city or ad.get('addressLocality') or ad.get('addressRegion') or ''
            ar=d.get('aggregateRating') or {}
            if isinstance(ar,dict) and ar.get('ratingValue'):
                try:rating=float(ar['ratingValue']);count=int(float(ar.get('ratingCount') or ar.get('reviewCount') or 0)) or None
                except Exception:pass
            im=d.get('image')
            if isinstance(im,str):im=[im]
    if not city:
        m=re.search(r'"(?:addressLocality|city|cityName)"\s*:\s*"([^"]{2,40})"',t);city=html.unescape(m.group(1)) if m else ''
    m=re.search(r'(\d+)\s*(?:guests?|sleeps|people)',html.unescape(t),re.I)
    if m:
        try:guests=int(m.group(1))
        except Exception:pass
    am=[]
    for pat in [r'"(?:amenity|facility|feature)(?:Name|Title)?"\s*:\s*"([^"]{3,30})"',r'<li[^>]*class="[^"]*(?:amenity|facility|feature)[^"]*"[^>]*>\s*([^<]{3,30})']:
        am+= [html.unescape(x).strip() for x in re.findall(pat,t,re.I)]
    am=[a for a in dict.fromkeys(am) if a and not a.lower().startswith('http')][:30]
    photos=_photos(t,base)
    title=re.sub(r'\s+',' ',html.unescape(title or '')).strip()[:120]
    return {'id':re.sub(r'\W+','',urlparse(base).path)[-18:] or host,'url':base,'source':host,
            'title':title or f'Listing on {host}','city':city,'rating':rating,'count':count,'guests':guests,
            'description':re.sub(r'\s+',' ',html.unescape(desc or ''))[:1200],'amenities':am,'highlights':[],
            'categories':{},'superhost':False,'guest_favourite':False,'host':'','photos':photos}
