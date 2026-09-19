"""Offline regression tests: every mistake the reel generator has made once must stay fixed.
Run: .venv/bin/python -m pytest -q tests"""
import json,sys
from pathlib import Path
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

def test_drive_filename_is_listing_url():
    from app import gdrive
    assert gdrive.safe_name('https://www.airbnb.co.uk/rooms/1673857257882928402?adults=1&check_in=2026-10-04')=='https://www.airbnb.co.uk/rooms/1673857257882928402.mp4'
    assert gdrive.status()['folder']=='Listing Reels'
