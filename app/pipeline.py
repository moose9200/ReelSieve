#!/usr/bin/env python3
"""ReelSieve pipeline: Airbnb URL -> facts + photos + reviews -> 30 s cinematic reel (-> optional Seedance clips) -> email.
CLI: python app/pipeline.py <airbnb_url> <out_dir> [--email x@y] [--ai-motion]"""
import re,os,sys,json,html,subprocess,shutil,time,smtplib,base64,mimetypes
from pathlib import Path
from email.message import EmailMessage
import httpx
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;PY=sys.executable
def cpu_budget(cpu_max='/sys/fs/cgroup/cpu.max'):
    """CPUs this container may really use. Railway reports the host's 48 cores while the cgroup quota allows 8;
    thread pools sized from the core count (torch, OpenBLAS, x264) then burn the quota and sit throttled."""
    try:
        quota,period=Path(cpu_max).read_text().split()[:2]
        if quota!='max':return max(1,-(-int(quota)//int(period)))
    except (OSError,ValueError):pass
    return len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else (os.cpu_count() or 1)
CPUS=cpu_budget()
# Set before numpy/torch load; depth.py and the renderers inherit them. NumPy madvises hugepages for big arrays, and on a
# long-running, fragmented host every per-frame allocation then stalls in direct compaction (measured: 6 segments 433 s -> 60 s).
for _k,_v in (('OMP_NUM_THREADS',CPUS),('OPENBLAS_NUM_THREADS',CPUS),('MKL_NUM_THREADS',CPUS),('NUMPY_MADVISE_HUGEPAGE',0)):os.environ.setdefault(_k,str(_v))
UA={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36','Accept-Language':'en-GB,en;q=0.9'}
ROUTE=[('exterior',['exterior','front','entrance','building','street','driveway']),('living',['living']),('kitchen',['kitchen','dining']),('bedroom',['bedroom']),
       ('bathroom',['bathroom']),('garden',['garden','patio','terrace','balcony','outdoor','yard']),('spa',['hot tub','pool','sauna','jacuzzi']),('view',['view']),('other',['additional','other','common','office','gym'])]
CAPTIONS={'exterior':[('Arrive in {city}','{hood_or_city}  /  {parking}'),('First impressions','{city}'),('Step inside','{city}'),('Home, for now','{city}')],
          'living':[('Room to unwind','{living_fact}'),('Light, all day','{living_fact}'),('Settle in','{city} living'),('Made for lingering','{living_fact}'),('Your corner of {city}','{living_fact}')],
          'kitchen':[('Cook, gather, linger','{kitchen_fact}'),('Slow breakfasts','{kitchen_fact}'),('Dinner, in','{kitchen_fact}')],
          'bedroom':[('Sleep well','{beds_fact}'),('Second bedroom','{beds_fact}'),('Rest easy','{beds_fact}')],
          'bathroom':[('Soak it all in','{bath_fact}'),('Fresh start','{bath_fact}'),('Unhurried mornings','{bath_fact}')],
          'garden':[('Your private outdoors','{garden_fact}'),('Evenings outside','{garden_fact}'),('Sun, when it shows','{garden_fact}')],
          'spa':[('Switch off completely','{spa_fact}'),('Warm up, wind down','{spa_fact}'),('Nights under the stars','{spa_fact}')],
          'view':[('The view from here','{city}'),('Above it all','{city}'),('Out the window','{city}')],
          'other':[('More to discover','{amenity_fact}'),('The details','{amenity_fact}'),('Every corner considered','{city}'),
                   ('Take a closer look','{amenity_fact}'),('Thoughtful touches','{city}'),('Made to feel at home','{amenity_fact}'),
                   ('Space to breathe','{city}'),('Quiet corners','{amenity_fact}'),('Room for everyone','{city}'),
                   ('Stay a little longer','{amenity_fact}'),('Little luxuries','{city}')]}
def log(cb,msg):
    print(msg,flush=True)
    if cb:cb(msg)
def is_airbnb(url):return bool(re.search(r'airbnb\.[a-z.]+/rooms/\d+',url or ''))
def listing_id(url):
    m=re.search(r'/rooms/(\d+)',url)
    if not m:raise ValueError('Not an Airbnb listing URL (expected /rooms/<id>)')
    return m.group(1)
# ---------------- scraping ----------------
def scrape_listing(url,cb=None):
    from app import airbnb
    lid=listing_id(url);canon=f'https://www.airbnb.co.uk/rooms/{lid}'
    log(cb,f'Fetching listing {lid}');t=airbnb.get(canon,headers=UA,timeout=40).text   # a block raises airbnb.Unavailable: the job stops here
    d={'id':lid,'url':canon}
    ld=re.search(r'<script type="application/ld\+json">(\{"@context":"https://schema.org","@type":"Product".*?)</script>',t)
    if ld:
        try:
            j=json.loads(ld.group(1));d['title']=html.unescape(j.get('name',''));d['description']=html.unescape(j.get('description',''));ar=j.get('aggregateRating') or {}
            d['rating']=float(ar.get('ratingValue',0) or 0);d['count']=int(ar.get('ratingCount',0) or 0)
        except Exception:pass
    if not d.get('title'):
        m=re.search(r'<title>([^<]+)',t);d['title']=html.unescape(m.group(1)).split(' - ')[0] if m else f'Listing {lid}'
    m=re.search(r'"city":"([^"]+)"',t);d['city']=html.unescape(m.group(1)) if m else ''
    m=re.search(r'"guestSatisfactionOverall":([\d.]+)',t);d['rating']=d.get('rating') or (float(m.group(1)) if m else 0)
    m=re.search(r'"visibleReviewCount":"?(\d+)',t);d['count']=d.get('count') or (int(m.group(1)) if m else 0)
    m=re.search(r'"personCapacity":(\d+)',t);d['guests']=int(m.group(1)) if m else None
    d['superhost']=bool(re.search(r'"isSuperhost":true',t));d['guest_favourite']='guestFavoriteDescription":"' in t and 'most loved' in t
    cats={}
    for k in ['accuracy','checkin','cleanliness','communication','location','value']:
        m=re.search(rf'"{k}Rating":([\d.]+)',t)
        if m:cats[{'checkin':'Check-in'}.get(k,k.capitalize())]=round(float(m.group(1)),1)
    d['categories']=cats
    m=re.search(r'(\d+) guests? · (\d+|Studio) bedrooms? · (\d+) beds? · ([\d.]+) (?:shared |private )?bathrooms?',html.unescape(t))
    if m:d.update(guests=int(m.group(1)),bedrooms=m.group(2),beds=int(m.group(3)),baths=m.group(4))
    m=re.search(r'"roomType":"([^"]+)"',t);d['room_type']=m.group(1) if m else ''
    m=re.search(r'Hosted by ([A-Z][\w\'-]{1,30})',html.unescape(t));d['host']=m.group(1) if m else ''
    am=re.findall(r'"title":"([^"]{3,40})","subtitle":null,"icon":"SYSTEM_[A-Z_]+","available":true',t)
    if not am:am=re.findall(r'"available":true,"title":"([^"]{3,40})"',t)
    d['amenities']=list(dict.fromkeys(html.unescape(a) for a in am))[:40]
    hl=re.findall(r'"title":"([^"]{6,60})","subtitle":"([^"]{6,120})","icon":"SYSTEM_',t);d['highlights']=[(html.unescape(a),html.unescape(b)) for a,b in hl][:4]
    photos=[];seen=set()
    for lab,u in re.findall(r'"accessibilityLabel":"([^"]+)","baseUrl":"(https://a0\.muscache\.com/im/pictures/[^"]+)"',t):
        u=u.split('?')[0]
        if u in seen:continue
        seen.add(u);photos.append({'label':html.unescape(lab),'url':u})
    if not photos:
        for u in dict.fromkeys(re.findall(rf'https://a0\.muscache\.com/im/pictures/hosting/Hosting-{lid}/original/[0-9a-f-]+\.(?:jpe?g|png)',t)):photos.append({'label':'','url':u})
    d['photos']=photos;log(cb,f'Found {len(photos)} photos, rating {d.get("rating")} from {d.get("count")} reviews')
    return d
def scrape_reviews(url,cb=None,limit=12):
    """Reviews are client-rendered; use headless Chromium and parse the visible text.
    The base /rooms/<id> page carries no review text or dates (checked live 27 Sep 2026), so this page stays.
    Every request the page makes to Airbnb goes through app.airbnb too: the page takes a slot of the page budget, each
    script, style and data call one of the 'browser' budget, and pictures, video and fonts are not loaded at all (the
    text renders the same without them, checked live 27 Sep 2026). A block status on any Airbnb response, the reviews
    data call included, stops the job like any other."""
    from app import airbnb
    lid=listing_id(url);out=[];scrape_reviews.meta={};rurl=f'https://www.airbnb.co.uk/rooms/{lid}/reviews'
    try:
        airbnb.gate(rurl)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b=p.chromium.launch(headless=True);pg=b.new_page(user_agent=UA['User-Agent'],locale='en-GB')
            answers,stopped=[],[]
            def route(r):
                q=r.request
                if q.resource_type in ('image','media','font'):return r.abort()
                if not (q.url==rurl and q.is_navigation_request()):   # the page itself took its slot above
                    try:airbnb.gate(q.url,None if q.is_navigation_request() else 'browser')
                    except airbnb.Unavailable as e:stopped.append(e);return r.abort()
                r.continue_()
            pg.route('**/*',route);pg.on('response',lambda resp:answers.append((resp.url,resp.status)))
            resp=pg.goto(rurl,wait_until='domcontentloaded',timeout=60000)
            if resp:airbnb.check(rurl,resp.status)
            try:pg.wait_for_selector('text=/Rating, \\d stars/',timeout=45000)   # paced requests: allow for the queue
            except Exception:pass
            pg.wait_for_timeout(1500)
            for u,s in answers:airbnb.check(u,s)   # every Airbnb response the page got, the reviews data call included
            if stopped:raise stopped[0]
            airbnb.check(rurl,200,pg.content(),'text/html');text=pg.inner_text('body');b.close()
        mm=re.search(r'5 stars, (\d+)% of reviews',text)
        if mm:scrape_reviews.meta['five_star_pct']=int(mm.group(1))
        lines=text.split('\n')
        for i,l in enumerate(lines):
            m=re.match(r'^Rating, (\d) stars',l)
            if not m or i<2:continue
            name=lines[i-2].strip();stars=int(m.group(1));j=i+1
            while j<len(lines) and re.match(r'^(,|·|\s*)$',lines[j]):j+=1
            date=lines[j].strip() if j<len(lines) else '';j+=1;txt=[]
            while j<len(lines) and lines[j].strip() and not lines[j].startswith('Show more') and not re.search(r'on Airbnb$',lines[j+1] if j+1<len(lines) else ''):
                txt.append(lines[j].strip());j+=1
                if len(' '.join(txt))>500:break
            body=re.sub(r'\s*Show more$','',' '.join(txt)).strip()
            body=re.sub(r'^[\s,·•]+','',body);body=re.sub(r'^(Stayed (with kids|with a pet|a few nights|one night|about a week|a week|over a week|in a home|for a month or more)|Group trip|Family trip|Trip with friends|Solo trip|Business trip|Couple.?s trip)\s*[,·]?\s*','',body,flags=re.I).strip()
            if body and name and len(name)<40:out.append({'name':name,'stars':stars,'date':date,'text':body})
            if len(out)>=limit:break
    except airbnb.Unavailable:raise   # blocked or paused: stop the job, never continue by another route
    except Exception as e:log(cb,f'Reviews unavailable ({type(e).__name__}: {str(e)[:80]}) — continuing without review card')
    dedup=[];seen=set()
    for r in out:
        k=(r['name'],r['text'][:40])
        if k not in seen:seen.add(k);dedup.append(r)
    log(cb,f'Captured {len(dedup)} reviews')
    return [{k:v for k,v in r.items() if k!='name'} for r in dedup]   # names only told duplicates apart; the reel never names a guest
def pick_review(revs):
    """Shortest 5-star review that names a concrete feature, trimmed to ~140 chars at a sentence boundary."""
    kws=['hot tub','sauna','clean','spotless','host','view','location','beach','bed','kitchen','garden','pool','beautiful','amazing','perfect']
    cands=[r for r in revs if r['stars']==5 and 40<=len(r['text'])<=400] or revs
    def score(r):t=r['text'].lower();return -(sum(k in t for k in kws)*10-abs(len(r['text'])-140)/20)
    for r in sorted(cands,key=score):
        s=re.split(r'(?<=[.!?])\s+',r['text']);acc=''
        for part in s:
            if len(acc)+len(part)<=150:acc=(acc+' '+part).strip()
            else:break
        if len(acc)>=40:return dict(r,text=acc)
    return cands[0] if cands else None
# ---------------- selection ----------------
def classify(label):
    l=label.lower()
    for key,words in ROUTE:
        if any(w in l for w in words):return key
    return 'other'
def month_word(date):
    m=re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})',date or '')
    return f'{m.group(1)} {m.group(2)}' if m else time.strftime('%B %Y')
def build_manifest(d,revs,imgdir,depth_dir,scenes_n=None,max_scenes=14,min_scenes=6,scores=None,fill_any=False):
    """Walkthrough order (research: exterior → entry/living → kitchen/dining → bedrooms → bath → outdoor → best feature + CTA;
    3–5 s per shot, 8–15 photos). Photos stay grouped by room so the reel reads like walking the house; length = photo count × scene_seconds.
    fill_any: short of min_scenes, any unused photo fills in as an 'other' scene, not only unlabelled ones (own photos, which
    may all show one or two rooms)."""
    groups={}
    for p in d['photos']:groups.setdefault(classify(p['label']),[]).append(p)
    if scores:   # best-first inside each room; drop clearly bad frames (blurry / blown-out / off-shoot) when the room has alternatives
        for k,lst in groups.items():
            lst.sort(key=lambda p:-(scores.get(Path(p['url']).name,{}).get('score',0)))
            good=[p for p in lst if scores.get(Path(p['url']).name,{}).get('score',0)>=45]
            if good:groups[k]=good
    order=[k for k,_ in ROUTE];per_room_cap=3
    ext=groups.get('exterior',[]);hero=ext[0] if ext else (groups.get('view') or groups.get('garden') or d['photos'])[0]
    closer=ext[1] if len(ext)>1 else (groups.get('spa') or groups.get('garden') or groups.get('view') or [hero])[0]   # end on the best feature
    chosen=[];used={hero['url'],closer['url']} if closer is not hero else {hero['url']}
    # fill breadth-first (1st photo of every room, then 2nd, then 3rd) so no room is starved, then sort back into route order (grouped)
    for rnd in range(per_room_cap):
        for k in order:
            if k=='other':continue
            cands=[p for p in groups.get(k,[]) if p['url'] not in used]
            if cands and len(chosen)<max_scenes:p=cands[0];chosen.append((k,p));used.add(p['url'])
    chosen.sort(key=lambda kp:order.index(kp[0]))
    if len(chosen)<min_scenes:   # unlabelled "Additional photos" only fill gaps
        for p in groups.get('other',[])+(d['photos'] if fill_any else []):
            if len(chosen)>=min_scenes:break
            if p['url'] not in used:chosen.append(('other',p));used.add(p['url'])
    if hero is closer:closer=next((p for k,p in reversed(chosen) if k in('spa','garden','view','exterior')),hero)
    scene_items=chosen;last=closer
    pool=[p for p in d['photos'] if p['url'] not in used]+[p for k,p in chosen[len(chosen)//2:]]
    bg_trust=pool[0] if pool else hero;bg_review=next((p for p in pool[1:] if p is not bg_trust),scene_items[-2][1] if len(scene_items)>1 else hero)
    if last in (bg_trust,bg_review):last=next((p for p in [x for _,x in reversed(chosen)] if p not in (bg_trust,bg_review) and p is not hero),hero)
    am=[a.lower() for a in d.get('amenities',[])];city=d.get('city') or 'town'
    def has(*w):return any(any(x in a for x in w) for a in am)
    facts={'city':city,'hood_or_city':city,'parking':'Free parking' if has('parking') else ('Superhost' if d.get('superhost') else (f"{d['guests']} guests" if d.get('guests') else '')),
           'living_fact':'  /  '.join([x for x in ['TV' if has('tv') else '','Wifi' if has('wifi') else '','Fireplace' if has('fireplace') else ''] if x]) or 'Space to relax',
           'kitchen_fact':'Full kitchen' if has('kitchen') else 'Dining space','beds_fact':'  /  '.join(x for x in [f"{d['beds']} beds" if d.get('beds') else '',f"sleeps {d['guests']}" if d.get('guests') else ''] if x) or city,
           'bath_fact':'  /  '.join([x for x in ['Bath' if has('bath') else '','Hairdryer' if has('hairdryer') else ''] if x]) or 'Fresh and modern',
           'garden_fact':'  /  '.join([x for x in ['Fire pit' if has('fire pit') else '','BBQ' if has('bbq','barbecue') else '','Private garden' if has('garden') else ''] if x]) or 'Fresh air, your way',
           'spa_fact':'  /  '.join([x for x in ['Hot tub' if has('hot tub') else '','Sauna' if has('sauna') else '','Pool' if has('pool') else ''] if x]) or 'Time to switch off',
           'amenity_fact':'  /  '.join(d.get('amenities',[])[:2]) or city}
    def fill(s):
        try:return s.format(**facts)
        except Exception:return s
    scenes=[];seen_k={}
    for k,p in scene_items:
        opts=CAPTIONS.get(k,CAPTIONS['other']);t,sub=opts[seen_k.get(k,0)%len(opts)];seen_k[k]=seen_k.get(k,0)+1;scenes.append({'image':str(imgdir/Path(p['url']).name),'title':fill(t),'subtitle':fill(sub).strip().rstrip('/').strip() or city,'room':k})   # an empty fact leaves no dangling ' / '
    rv=pick_review(revs);badges=[b for b in ['Guest favourite' if d.get('guest_favourite') else '','Superhost' if d.get('superhost') else '',f"{d.get('count')} reviews" if d.get('count') else ''] if b]
    hooks=[x for x in [('Hot tub' if has('hot tub') else ''),('Sauna' if has('sauna') else ''),('Pool' if has('pool') else ''),(f"Sleeps {d['guests']}" if d.get('guests') else ''),('Free parking' if has('parking') else ''),('Wifi' if has('wifi') else '')] if x][:3]
    m={'brand':'','depth_dir':str(depth_dir),'scene_seconds':4.5,'intro_seconds':4.5,'outro_seconds':5.5,
       'intro':{'eyebrow':f"{city.upper()}" if city else 'AIRBNB','title':re.split(r'\s[|·-]\s',d['title'])[0][:40],'subtitle':' · '.join(hooks) or city,'image':str(imgdir/Path(hero['url']).name)},
       'scenes':scenes,
       'outro':{'eyebrow':(f"GUEST FAVOURITE  ·  " if d.get('guest_favourite') else '')+(f"{d['rating']:.2f} FROM {d['count']} REVIEWS" if d.get('rating') else 'AIRBNB'),
                'title':'Your next escape awaits','subtitle':' · '.join(dict.fromkeys([x for x in [city,f"Sleeps {d['guests']}" if d.get('guests') else '',hooks[0] if hooks else ''] if x])),'cta':'BOOK ON AIRBNB','by':'by Braivex.com','image':str(imgdir/Path(last['url']).name)}}
    if d.get('rating') and d.get('categories'):
        m['trust']={'seconds':5.0,'image':str(imgdir/Path(bg_trust['url']).name),'rating':d['rating'],'count':d['count'],'five_star_pct':getattr(scrape_reviews,'meta',{}).get('five_star_pct'),'badges':badges,'categories':d['categories']}
    m['aspect']=d.get('_aspect','9:16');m['transition_seconds']=0.9
    for sc in m['scenes']:sc['caption']=sc['title'];sc['accent']=sc['title'].split()[-1]
    m['overlays']={'title':m['intro']['title'],'subtitle':m['intro']['subtitle'],
        'trust':(f"{d['rating']:.2f} \u2605 from {d['count']} reviews" if d.get('rating') else None),
        'review':(rv['text'] if rv else None),'review_by':(f"Guest review, {month_word(rv['date'])}" if rv else None),
        'cta':'Your next escape awaits','cta_pill':'BOOK ON AIRBNB','by':'by Braivex.com'}
    if rv:m['reviews']={'seconds':5.5,'bg':[str(imgdir/Path(bg_review['url']).name)],'items':[{'stars':rv['stars'],'date':month_word(rv['date']),'text':rv['text']}]}
    return m

# ---------------- QA guards (regression: each rule maps to a mistake already made once) ----------------
class ManifestError(Exception):pass
def lint_manifest(m,min_images=6):
    """Raise ManifestError on any known failure mode instead of rendering a flawed reel."""
    probs=[]
    titles=[s['title'] for s in m['scenes']]
    if len(titles)!=len(set(titles)):probs.append(f'duplicate scene titles: {[t for t in titles if titles.count(t)>1]}')
    imgs=[s['image'] for s in m['scenes']]
    if len(imgs)!=len(set(imgs)):probs.append('same photo used in two scenes')
    bgs={'intro':m['intro']['image'],'outro':m['outro']['image'],'trust':(m.get('trust') or {}).get('image'),'review':((m.get('reviews') or {}).get('bg') or [None])[0]}
    if bgs['outro'] in (bgs['trust'],bgs['review']):probs.append('outro reuses the trust/review background')
    if bgs['trust'] and bgs['trust']==bgs['review']:probs.append('trust and review cards share a background')
    distinct=set(imgs)|{v for v in bgs.values() if v}
    if len(distinct)<min_images:probs.append(f'only {len(distinct)} distinct photos used (min {min_images})')
    for k in ['intro','outro']:
        parts=[x.strip() for x in m[k]['subtitle'].split('·') if x.strip()]
        if len(parts)!=len(set(parts)):probs.append(f'{k} subtitle repeats a fact: {m[k]["subtitle"]}')
    for rv in (m.get('reviews') or {}).get('items',[]):
        t=rv['text']
        typed=(m.get('reviews') or {}).get('typed')   # the customer's own words (own-photo reel), not a scraped trip tag
        if re.match(r'^[\s,·•]',t) or (not typed and re.match(r'^(Stayed |Group trip|Family trip|Solo trip|Business trip)',t,re.I)):probs.append(f'review text not sanitised: {t[:40]!r}')
        if len(t)<20:probs.append('review text too short')
    if m.get('brand'):probs.append('brand watermark is on (must be off by default)')
    for pth in list(imgs)+[v for v in bgs.values() if v]:
        if not Path(pth).exists():probs.append(f'image missing on disk: {Path(pth).name}')
    total=float(m.get('intro_seconds',5))+float(m.get('outro_seconds',6.5))+len(m['scenes'])*float(m.get('scene_seconds',4.5))+float((m.get('trust') or {}).get('seconds',0))+float((m.get('reviews') or {}).get('seconds',0))
    segs=2+len(m['scenes'])+(1 if m.get('trust') else 0)+len((m.get('reviews') or {}).get('items',[]));total-=0.6*(segs-1)
    if float(m.get('scene_seconds',4.5))<3.5:probs.append('scene_seconds under 3.5 s — images change too fast')
    total=sum(float(x.get('seconds') or m.get('scene_seconds',4.5)) for x in m['scenes'])
    if not 20<=total<=200:probs.append(f'reel would be {total:.1f}s (expected 27–150 s, scaled by photo count)')
    if len(m['scenes'])<6:probs.append('fewer than 6 walkthrough scenes')
    rk=[s.get('room') for s in m['scenes'] if s.get('room') and s.get('room')!='other'];ordr=[k for k,_ in ROUTE]
    if rk!=sorted(rk,key=ordr.index):probs.append(f'scenes out of route order: {rk}')
    if probs:raise ManifestError('; '.join(probs))
    return total
# ---------------- media ----------------
def download_photos(d,imgdir,cb=None,needed=None):
    """Photo URLs come from a scraped page, so each goes through the public-host guard. Airbnb CDN photos also take a slot
    of the shared image budget (fetch.get -> airbnb.gate); a block raises airbnb.Unavailable out of here and stops the job."""
    from app import fetch
    from concurrent.futures import ThreadPoolExecutor
    imgdir.mkdir(parents=True,exist_ok=True);urls=needed or [p['url'] for p in d['photos']]
    def one(u):
        f=imgdir/Path(u).name
        if f.exists():return
        try:_,body=fetch.get(u+('?im_w=1920' if 'muscache.com' in u else ''),headers=UA,timeout=60)
        except (ValueError,httpx.HTTPError):return
        f.write_bytes(body)
    with ThreadPoolExecutor(6) as ex:list(ex.map(one,urls))   # parallel, paced by the shared image budget
    log(cb,f'Downloaded {len(list(imgdir.iterdir()))} photos')
def seedance_clips(m,workdir,cb=None,duration=4):
    """Optional: Higgsfield Seedance 2.5 image-to-video per scene (billable). Falls back per scene on any failure."""
    import higgsfield_client as hf
    from higgsfield_client import Failed,NSFW,Cancelled
    moves=['slow dolly forward','gentle truck right','slow pull back','subtle orbit left']
    for i,s in enumerate(m['scenes']):
        try:
            log(cb,f'Seedance 2.5: uploading scene {i+1}');img_url=hf.upload_file(s['image'])
            bad={'s':None}
            def upd(st):
                if isinstance(st,(Failed,NSFW,Cancelled)):bad['s']=type(st).__name__
            res=hf.subscribe('bytedance/seedance-2.5/image-to-video',arguments={'image_url':img_url,'prompt':f'{moves[i%4]}, stable camera, photorealistic interior walkthrough, seamless, natural lighting, no people','duration':duration,'resolution':'720p','generate_audio':False},on_queue_update=upd)
            if bad['s']:log(cb,f'Seedance scene {i+1} ended {bad["s"]} — using parallax');continue
            v=(res or {}).get('video');url=v.get('url') if isinstance(v,dict) else v
            if not url:log(cb,f'Seedance scene {i+1}: no video url — using parallax');continue
            out=workdir/f'clip{i}.mp4';out.write_bytes(httpx.get(url,timeout=120).content);s['clip']=str(out);log(cb,f'Seedance scene {i+1} ready')
        except Exception as e:log(cb,f'Seedance scene {i+1} failed ({type(e).__name__}: {str(e)[:60]}) — using parallax')
def render_images(m):
    return sorted({s['image'] for s in m['scenes']}|{m['intro']['image'],m['outro']['image']}|({m['trust']['image']} if 'trust' in m else set())|set(m.get('reviews',{}).get('bg',[])))
def render(m,workdir,out,cb=None,renderer='v2'):
    imgs=render_images(m)
    sel=workdir/'sel';sel.mkdir(exist_ok=True)
    for i in imgs:shutil.copy(i,sel/Path(i).name)
    missing=[i for i in imgs if not (Path(m['depth_dir'])/(Path(i).stem+'.png')).exists()]
    if missing:
        sel3=workdir/'sel3';sel3.mkdir(exist_ok=True)
        for i in missing:shutil.copy(i,sel3/Path(i).name)
        log(cb,f'Estimating depth for {len(missing)} more frames');subprocess.run([PY,str(HERE/'depth.py'),str(sel3),m['depth_dir']],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:log(cb,'Depth maps ready')
    (workdir/'manifest.json').write_text(json.dumps(m,indent=1));log(cb,'Rendering '+('tutorial-style 9:16 walkthrough' if renderer=='v3' else 'cinematic 16:9 walkthrough (v2)'))
    renderer_file='render_v3.py' if renderer=='v3' else 'render_v2.py'
    r=subprocess.run([PY,str(HERE/renderer_file),str(workdir/'manifest.json'),str(out),*(['--workers','6'] if renderer=='v3' else ['--workers',str(CPUS),'--threads',str(CPUS)])],capture_output=True,text=True)
    if r.returncode!=0:raise RuntimeError('render failed: '+r.stderr[-800:])
    dur=float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(out)],capture_output=True,text=True).stdout.strip() or 0)
    small=out.with_name(out.stem+'-720p.mp4');subprocess.run(['ffmpeg','-y','-v','error','-i',str(out),'-vf','scale=720:-2' if m.get('aspect')=='9:16' else 'scale=-2:720',*(['-c:v','h264_videotoolbox','-b:v','3M'] if __import__('platform').system()=='Darwin' else ['-c:v','libx264','-preset','veryfast','-crf','23','-threads',str(CPUS)]),'-c:a','aac','-b:a','128k','-movflags','+faststart',str(small)],check=True)
    log(cb,f'Rendered {dur:.1f}s reel');return dur,small
# ---------------- email ----------------
def send_email(to,subject,body_html,attach=None,link=None,cb=None):
    frm=os.getenv('FROM_EMAIL');name=os.getenv('FROM_NAME','Braivex ReelSieve');sg=os.getenv('SENDGRID_API_KEY')
    if not frm:return 'skipped','FROM_EMAIL not set'
    if sg:
        payload={'personalizations':[{'to':[{'email':to}]}],'from':{'email':frm,'name':name},'subject':subject,'content':[{'type':'text/html','value':body_html}]}
        if attach and attach.stat().st_size<20_000_000:payload['attachments']=[{'content':base64.b64encode(attach.read_bytes()).decode(),'type':'video/mp4','filename':attach.name,'disposition':'attachment'}]
        r=httpx.post('https://api.sendgrid.com/v3/mail/send',headers={'Authorization':f'Bearer {sg}'},json=payload,timeout=120)
        return ('sent',f'SendGrid {r.status_code}') if r.status_code in (200,202) else ('failed',f'SendGrid {r.status_code}: {r.text[:200]}')
    host=os.getenv('SMTP_HOST')
    if not host:return 'skipped','no SendGrid key or SMTP host configured'
    msg=EmailMessage();msg['From']=f'{name} <{frm}>';msg['To']=to;msg['Subject']=subject;msg.set_content(re.sub('<[^>]+>','',body_html));msg.add_alternative(body_html,subtype='html')
    if attach and attach.stat().st_size<20_000_000:msg.add_attachment(attach.read_bytes(),maintype='video',subtype='mp4',filename=attach.name)
    try:
        port=int(os.getenv('SMTP_PORT','587'))
        with (smtplib.SMTP_SSL(host,port,timeout=60) if port==465 else smtplib.SMTP(host,port,timeout=60)) as s:
            if port!=465:s.starttls()
            if os.getenv('SMTP_USER'):s.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASS',''))
            s.send_message(msg)
        return 'sent',f'SMTP {host}'
    except Exception as e:return 'failed',f'SMTP {type(e).__name__}: {str(e)[:160]}'
def email_html(d,link,dur):
    return f"""<div style="font-family:Assistant,system-ui,sans-serif;background:#070606;color:#fff;padding:32px"><p style="letter-spacing:.12em;font-size:11px;color:#8a8a8a;margin:0 0 8px">BRAIVEX · LISTING REEL</p>
<h1 style="margin:0 0 12px;font-size:24px">{html.escape(d['title'])}</h1><p style="color:#b3b3b3;margin:0 0 20px">{html.escape(d.get('city',''))} · {d.get('rating','')}★ from {d.get('count','')} reviews · {dur:.0f}s reel</p>
{'<p><a href="'+link+'" style="background:#00f0ff;color:#04070a;padding:12px 18px;border-radius:10px;font-weight:700;text-decoration:none">Watch the 1080p reel</a></p>' if link else ''}
<p style="color:#8a8a8a;font-size:12px">A 720p copy is attached when under 20 MB. Made with ReelSieve, a Braivex product · braivex.com</p></div>"""
# ---------------- orchestration ----------------
# ---------------- own photos (no scraping): the customer's photos + typed facts ----------------
PHOTO_LABEL={k:words[0] for k,words in ROUTE}   # a label classify() maps back to its room ('hot tub' -> spa)
OWN_PHOTOS={'min_scenes':8,'fill_any':True}      # own photos may be unlabelled or all of one room: use more of them
def photo_listing(facts,names):
    """The listing dict build_manifest expects, built from the customer's typed facts and photo files: nothing is fetched.
    `names` are the photo files in upload order; facts['rooms'] is aligned with them."""
    rooms=list(facts.get('rooms') or [])
    d={'id':'photos','url':None,'title':facts['title'],'city':facts['location'],'rating':None,'count':None,'guests':None,'host':'',
       'amenities':list(facts.get('highlights') or []),'highlights':[],'categories':{},'superhost':False,'guest_favourite':False,
       'photos':[{'label':PHOTO_LABEL.get(rooms[i] if i<len(rooms) else 'other',PHOTO_LABEL['other']),'url':n} for i,n in enumerate(names)]}
    # The first quote as typed, at the rating given: pick_review's five-star preference is for scraped reviews only
    # (DMCC Act 2024 Sch 20 para 13(5)(i): no greater prominence for positive reviews).
    revs=[{'stars':int(q['stars']),'date':'','text':q['text']} for q in facts.get('quotes') or []][:1]
    return d,revs
def own_photos_manifest(m,d):
    """Replace the wording that only fits an Airbnb listing. A typed guest quote is shown as the customer wrote it
    (the renderers add the quote marks), with no date (none is known) and never a name."""
    m['intro']['eyebrow']=(d.get('city') or '').upper() or 'YOUR NEXT STAY'
    m['outro']['eyebrow']=m['intro']['title'].upper()   # the outro subtitle already names the place
    m['outro']['cta']=m['overlays']['cta_pill']='BOOK YOUR STAY'
    if m.get('reviews'):
        for it in m['reviews']['items']:it['date']=''
        m['reviews']['typed']=True;m['overlays']['review_by']='Guest review'
    return m
def run_photos(images_dir,facts,out_dir,ai_motion=False,cb=None,renderer='v2',max_seconds=None,ai_resolution='1080p'):
    """Reel from the customer's own photos (already in disposable scratch as p01.jpg…) and typed facts: the same scoring,
    selection, depth, audit, AI motion and renderers as a listing reel, with no scraping."""
    out_dir=Path(out_dir);work=out_dir/'work';work.mkdir(parents=True,exist_ok=True);imgdir=Path(images_dir)
    names=sorted(p.name for p in imgdir.glob('p*.jpg'));log(cb,f'Using your {len(names)} photos')
    d,revs=photo_listing(facts,names)
    return _reel(d,revs,imgdir,out_dir,work,ai_motion,cb,renderer,max_seconds,ai_resolution,own=True)
def run(url,out_dir,email=None,ai_motion=False,cb=None,public_base=None,renderer='v2',max_seconds=None,ai_resolution='1080p'):
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True);work=out_dir/'work';work.mkdir(exist_ok=True)
    if is_airbnb(url):
        d=scrape_listing(url,cb);revs=scrape_reviews(url,cb)
    else:
        from app import extract
        log(cb,'Not an Airbnb link — reading the listing page directly');d=extract.scrape(url,cb);revs=[]
        log(cb,f"Found {len(d['photos'])} photos on {d.get('source')}")
        if len(d['photos'])<5:raise RuntimeError(f"Only {len(d['photos'])} usable photos found on that page. Try the listing's Airbnb link, or a page that shows the full photo gallery.")
    (out_dir/'listing.json').write_text(json.dumps({**d,'reviews':revs},indent=1))
    imgdir=work/'images';download_photos(d,imgdir,cb)   # every photo, so selection is on quality not on Airbnb's order
    return _reel(d,revs,imgdir,out_dir,work,ai_motion,cb,renderer,max_seconds,ai_resolution)
def _reel(d,revs,imgdir,out_dir,work,ai_motion,cb,renderer,max_seconds,ai_resolution,own=False):
    """Photos on disk + listing facts -> scored selection, depth, audit, optional AI motion, render. Shared by both entry points."""
    from app import photoscore
    have=[imgdir/Path(p['url']).name for p in d['photos'] if (imgdir/Path(p['url']).name).exists()]
    log(cb,f'Scoring {len(have)} photos for sharpness, light and colour')
    scores=photoscore.score_all(have,'9:16' if renderer=='v3' else '16:9')
    # depth maps for the shortlist (top 18 by cheap score) so the depth axis can count
    short=sorted(scores,key=lambda k:-scores[k]['score'])[:18];sel=work/'sel';sel.mkdir(exist_ok=True)
    for k in short:shutil.copy(imgdir/k,sel/k)
    log(cb,f'Estimating depth for {len(short)} photos')
    subprocess.run([PY,str(HERE/'depth.py'),str(sel),str(work/'depth')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    photoscore.add_depth(scores,work/'depth')
    m=build_manifest(d,revs,imgdir,work/'depth',scores=scores,**(OWN_PHOTOS if own else {}))
    if own:m=own_photos_manifest(m,d)   # own photos may be unlabelled: use more of them than the 6-scene floor
    m['photo_scores']=scores
    used={Path(s['image']).name for s in m['scenes']}|{Path(m['intro']['image']).name,Path(m['outro']['image']).name}
    ranked=sorted(scores.items(),key=lambda kv:-kv[1]['score'])
    log(cb,f"Scored {len(scores)} photos; using {len(used)} (best {ranked[0][1]['score']}, median {sorted(v['score'] for v in scores.values())[len(scores)//2]}, lowest used {min(scores[k]['score'] for k in used if k in scores)})")
    m['selection']={'downloaded':len(scores),'used':sorted(used),'skipped':[k for k,_ in ranked if k not in used]}
    # --- audit (free): is this photo set video-worthy? ---
    from app import aimotion
    try:
        # every frame the render will need, not just the scenes, so render() never loads the depth model a third time
        depth_dir=work/'depth';sel2=work/'sel2';sel2.mkdir(exist_ok=True);missing=[i for i in render_images(m) if not (depth_dir/(Path(i).stem+'.png')).exists()]
        for im_ in missing:shutil.copy(im_,sel2/Path(im_).name)
        if missing:log(cb,f'Estimating depth for {len(missing)} more frames');subprocess.run([PY,str(HERE/'depth.py'),str(sel2),str(depth_dir)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        aud=aimotion.audit([sc['image'] for sc in m['scenes']],depth_dir,{Path(sc['image']).name:sc.get('room') for sc in m['scenes']});m['audit']=aud
        log(cb,f"Audit: {aud['verdict']} ({aud['score']}/100) — "+'; '.join(aud['reasons']))
    except Exception as e:log(cb,f'Audit skipped ({type(e).__name__})')
    if ai_motion:
        # the method wants ~6 frames with the strongest depth axis, not every photo — cap paid shots (AI_MAX_SHOTS, default 6), route order kept
        cap=int(os.getenv('AI_MAX_SHOTS','6'));ds=(m.get('audit') or {}).get('depth_scores') or {};full_scenes=list(m['scenes'])
        if len(m['scenes'])>cap and ds:
            ranked=sorted(range(len(m['scenes'])),key=lambda i:-(ds.get(Path(m['scenes'][i]['image']).name) or 0))[:cap];keep=sorted(ranked)
            log(cb,f"AI motion: keeping {cap} of {len(m['scenes'])} frames with the strongest depth axis (route order kept)");m['scenes']=[m['scenes'][i] for i in keep]
        res_=ai_resolution if ai_resolution in ('720p','1080p') else '1080p';pl=aimotion.plan(m['scenes'],d,res_);m['ai_plan']=pl
        log(cb,f"AI motion plan: {len(pl['shots'])} shots on {pl['model']} @ {res_} ("+', '.join(sh['move'] for sh in pl['shots'])+')')
        if m.get('audit',{}).get('verdict')=='REJECT':log(cb,'Audit REJECT — skipping paid generation; parallax fallback (pick a listing with a clearer walking route)')
        elif not os.getenv('HF_KEY'):log(cb,'HF_KEY not configured — using parallax')
        else:
            for sh in pl['shots']:
                sc=m['scenes'][sh['index']];out=work/f"ai{sh['index']:02d}.mp4"
                log(cb,f"Seedance {sh['index']+1}/{len(pl['shots'])}: {sh['move']} on {sh['room']}")
                ok,info=aimotion.generate(sc['image'],sh['prompt'],out,res_,5,'9:16' if renderer=='v3' else '16:9',cb)
                if not ok:log(cb,f"  ↳ not generated ({info}) — parallax for this shot");sh['status']='failed';sh['error']=str(info);continue
                pr=aimotion.profile(out);sh['profile']=pr
                if pr.get('frozen'):log(cb,'  ↳ clip is frozen — dropping it (parallax instead)');sh['status']='frozen';continue
                a,b=pr['best_window'];tr=work/f"ai{sh['index']:02d}-trim.mp4";aimotion.trim(out,a,b,tr);sc['clip']=str(tr);sc['seconds']=round(b-a,2);sh['status']='ok'
                log(cb,f"  ↳ ok · motion {pr['mean_motion']} · kept {a:.1f}–{b:.1f}s"+(' · dying tail cut' if pr.get('dying_tail') else '')+(' · REVERSAL detected' if pr.get('reverses') else ''))
            if any(sc.get('clip') for sc in m['scenes']):m['transition_seconds']=0   # hard cuts between generated clips (no dissolves)
            else:m['scenes']=full_scenes;log(cb,'No AI clips were generated — using the full photo set with parallax motion')
        if not any(sc.get('clip') for sc in m['scenes']) and len(m['scenes'])<len(full_scenes):m['scenes']=full_scenes;log(cb,'AI motion not run — using the full photo set')
    cap=float(max_seconds or os.getenv('MAX_SECONDS','0') or 0)
    if cap:
        per=float(m.get('scene_seconds',4.5));fixed=float(m.get('intro_seconds',4.5))+float(m.get('outro_seconds',5.5))+float((m.get('trust') or {}).get('seconds',0))+float((m.get('reviews') or {}).get('seconds',0))
        room=max(3,int((cap-fixed)//per))
        if len(m['scenes'])>room:log(cb,f'Trimming to {room} scenes for the {int(cap)}s plan limit');m['scenes']=m['scenes'][:room]
    est=lint_manifest(m);log(cb,f'QA guards passed ({len(m["scenes"])} scenes, ~{est:.0f}s)')
    safe=re.sub(r'[^A-Za-z0-9]+','-',d['title'])[:40].strip('-');out=out_dir/f"{time.strftime('%Y-%m-%d')}_{safe}-by-Braivex.mp4"
    m['aspect']='9:16' if renderer=='v3' else '16:9'
    dur,small=render(m,work,out,cb,renderer)
    res={'video':str(out),'video_720':str(small),'duration':dur,'audit':m.get('audit'),'ai_plan':m.get('ai_plan'),'selection':m.get('selection'),'photo_scores':m.get('photo_scores'),'listing':{**{k:d.get(k) for k in ['id','url','title','city','rating','count','guests','host']},'photo':None if own else (d.get('photos') or [{}])[0].get('url')},'review_used':m.get('reviews',{}).get('items',[None])[0]}
    (out_dir/'result.json').write_text(json.dumps(res,indent=1));return res
if __name__=='__main__':
    import argparse
    # Developer tool: renders into an explicit directory with the caller's own environment. Customer
    # jobs never come through here; they run through app.worker in disposable scratch space.
    ap=argparse.ArgumentParser();ap.add_argument('url');ap.add_argument('out');ap.add_argument('--ai-motion',action='store_true');ap.add_argument('--ai-resolution',default='1080p');a=ap.parse_args()
    print(json.dumps(run(a.url,a.out,None,a.ai_motion,ai_resolution=a.ai_resolution),indent=1))
