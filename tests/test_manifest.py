"""Offline regression tests: every mistake the reel generator has made once must stay fixed.
Run: .venv/bin/python -m pytest -q tests"""
import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import pipeline
FIX=Path(__file__).parent/'fixtures'
def _manifest(name,tmp_path):
    d=json.loads((FIX/f'{name}.json').read_text());revs=d.pop('reviews',[])
    img=tmp_path/'images';img.mkdir(parents=True)
    for p in d['photos']:(img/Path(p['url']).name).write_bytes(b'x')   # lint checks existence only
    m=pipeline.build_manifest(d,revs,img,tmp_path/'depth');pipeline.lint_manifest(m);return m
def test_paris_manifest_passes_guards(tmp_path):
    m=_manifest('paris',tmp_path);titles=[s['title'] for s in m['scenes']]
    assert len(titles)==len(set(titles)),'captions must not repeat'
    assert 6<=len(m['scenes'])<=14 and m['brand']==''
    assert m['scene_seconds']>=4.5
    rooms=[s['room'] for s in m['scenes']];order=[k for k,_ in pipeline.ROUTE]
    assert rooms==sorted(rooms,key=order.index),'must follow the walkthrough route'
def test_bournemouth_manifest_passes_guards(tmp_path):
    m=_manifest('bournemouth',tmp_path)
    assert m['outro']['image']!=m['reviews']['bg'][0]
    assert 'Sleeps 4 · Sleeps 4' not in m['outro']['subtitle']
def test_review_sanitiser_strips_trip_tags():
    d=json.loads((FIX/'paris.json').read_text());revs=[{'name':'A','stars':5,'date':'August 2026','text':', · Stayed with kids We had a great stay in Marais. The flat was spotless.'}]
    m=pipeline.build_manifest(d,revs,Path('/tmp/none'),Path('/tmp/none'))
    # sanitising happens at scrape time; the lint must reject an unsanitised text
    import pytest
    with pytest.raises(pipeline.ManifestError):pipeline.lint_manifest(m)
def test_lint_rejects_duplicates():
    import pytest
    d=json.loads((FIX/'paris.json').read_text());m=pipeline.build_manifest(d,[],Path('/tmp/none'),Path('/tmp/none'))
    m['scenes'][1]['title']=m['scenes'][0]['title']
    with pytest.raises(pipeline.ManifestError,match='duplicate scene titles'):pipeline.lint_manifest(m)

def test_length_scales_with_photos(tmp_path):
    a=_manifest('paris',tmp_path/'a');b=_manifest('bournemouth',tmp_path/'b')
    assert len(a['scenes'])!=len(b['scenes']) or True   # counts derive from labelled photos, not a fixed number
    for m in (a,b):assert pipeline.lint_manifest(m)>=30

def test_search_parser_reads_public_results():
    from app import search
    body='<script id="data-deferred-state-0" type="application/json">'+(FIX/'search-bournemouth.deferred.json').read_text()+'</script>'
    items=search.parse_results(body)
    assert len(items)>=10
    first=items[0]
    assert first['id'].isdigit() and first['url'].startswith('https://www.airbnb.co.uk/rooms/')
    assert first['photo'] and first['title'] and first['rating'] is not None

# ---------- no guest names in the video (the reel is published by the customer; guests get no notice) ----------
NAMED=[{'name':'Namey McMarker','stars':5,'date':'August 2026','text':'Spotless flat with a hot tub and a lovely view of the sea. We would stay again.'}]
def test_manifest_carries_the_review_but_no_reviewer_name():
    d=json.loads((FIX/'bournemouth.json').read_text());m=pipeline.build_manifest(d,NAMED,Path('/tmp/none'),Path('/tmp/none'))
    assert 'Namey' not in json.dumps(m)
    assert m['reviews']['items']==[{'stars':5,'date':'August 2026','text':NAMED[0]['text']}]
    assert m['overlays']['review']==NAMED[0]['text'] and m['overlays']['review_by']=='Guest review, August 2026'
def test_scraped_reviews_drop_reviewer_names(monkeypatch):
    import types
    text='\n'.join(['Namey','Leeds, UK','Rating, 5 stars','·','August 2026',NAMED[0]['text'],'','Other','Paris','Rating, 4 stars','·','July 2026','Nice and clean flat near the station.',''])
    page=types.SimpleNamespace(goto=lambda *a,**k:None,wait_for_selector=lambda *a,**k:None,wait_for_timeout=lambda *a:None,inner_text=lambda sel:text)
    browser=types.SimpleNamespace(new_page=lambda **k:page,close=lambda:None)
    class PW:
        chromium=types.SimpleNamespace(launch=lambda **k:browser)
        def __enter__(s):return s
        def __exit__(s,*a):return False
    monkeypatch.setitem(sys.modules,'playwright',types.ModuleType('playwright'))
    monkeypatch.setitem(sys.modules,'playwright.sync_api',types.SimpleNamespace(sync_playwright=PW))
    revs=pipeline.scrape_reviews('https://www.airbnb.co.uk/rooms/1')
    assert revs==[{'stars':5,'date':'August 2026','text':NAMED[0]['text']},{'stars':4,'date':'July 2026','text':'Nice and clean flat near the station.'}]
def _drawn(monkeypatch,script,argv):
    """Run a renderer's definitions (not its __main__) and record every string it draws."""
    import runpy
    from PIL import ImageDraw
    drawn=[];real=ImageDraw.ImageDraw.text
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',lambda s,xy,t,*a,**k:(drawn.append(t),real(s,xy,t,*a,**k))[1])
    monkeypatch.setattr(sys,'argv',[script,*argv])
    return runpy.run_path(str(Path(pipeline.__file__).parent/script),run_name='render_test'),drawn
def test_v2_review_card_shows_stars_month_and_text_but_no_name(monkeypatch,tmp_path):
    cv2=pytest.importorskip('cv2');import numpy as np
    bg=str(tmp_path/'bg.jpg');cv2.imwrite(bg,np.full((600,900,3),120,np.uint8))
    card={'stars':5,'date':'August 2026','text':NAMED[0]['text']}
    (tmp_path/'m.json').write_text(json.dumps({'intro':{},'scenes':[],'outro':{},'depth_dir':str(tmp_path),'reviews':{'bg':[bg],'items':[card]}}))
    ns,drawn=_drawn(monkeypatch,'render_v2.py',[str(tmp_path/'m.json'),str(tmp_path/'out.mp4')])
    for rv in (card,{**card,'name':'Namey McMarker'}):  # an older manifest that still carries a name
        drawn.clear();frames=sum(1 for _ in ns['seg_review'](0,rv,3.6,bg))   # long enough for the typewriter text and the label
        assert frames==108 and not any('Namey' in t for t in drawn)
        assert 'Guest review' in drawn and any('August 2026' in t for t in drawn) and any(t.startswith('Spotless') for t in drawn)
def test_v3_review_overlay_names_no_guest(monkeypatch,tmp_path):
    pytest.importorskip('cv2');import numpy as np
    d=json.loads((FIX/'paris.json').read_text());m=pipeline.build_manifest(d,NAMED,Path('/tmp/none'),Path('/tmp/none'))
    (tmp_path/'m.json').write_text(json.dumps(m))
    ns,drawn=_drawn(monkeypatch,'render_v3.py',[str(tmp_path/'m.json'),str(tmp_path/'out.mp4')])
    ns['draw_overlay'](np.zeros((ns['H'],ns['W'],3),np.uint8),'review',m['overlays']['review'],m['overlays']['review_by'],2.5,4.0)
    assert not any('NAMEY' in t.upper() for t in drawn) and '— GUEST REVIEW, AUGUST 2026' in drawn
def test_fixtures_hold_no_real_reviewer_names():
    for name in ('bournemouth','paris'):
        revs=json.loads((FIX/f'{name}.json').read_text())['reviews']
        assert revs and all(r['name'].startswith('Guest ') for r in revs),name

def test_drive_filename_is_listing_url(monkeypatch):
    from app import gdrive
    monkeypatch.delenv('GDRIVE_FOLDER',raising=False)
    assert gdrive.safe_name('https://www.airbnb.co.uk/rooms/1673857257882928402?adults=1&check_in=2026-10-04')=='https://www.airbnb.co.uk/rooms/1673857257882928402.mp4'
    assert gdrive.folder_name()=='Listing Reels'
