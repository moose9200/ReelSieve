"""Hand the reel to the Airbnb host through Airbnb's own messaging — draft-only by design.
The tool never presses "Send message" itself: it prepares the message, gives a public link for the reel,
and opens the host's contact form pre-filled in a browser so the user reviews and sends it.
- Airbnb session: Playwright persistent Chromium profile at .listing-reel/airbnb-profile (user logs in once, headed).
- Public link: PUBLIC_BASE_URL setting, else a cloudflared quick tunnel started from Settings.
Form verified 19 Sep 2026 on /contact_host/<id>/send_message: textbox "Message the host" + button "Send message"."""
import os,re,subprocess,threading,time,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent;PROFILE=ROOT/'.listing-reel'/'airbnb-profile';PROFILE.mkdir(parents=True,exist_ok=True)
DEFAULT_MESSAGE=("Hi! I put together a short cinematic reel of your listing from its photos and guest reviews — thought you might like it for your own marketing: {reel_link}\n\n"
                 "Made by Braivex (braivex.com). Happy to send the full-resolution file or tailor it if useful.")
_lock=threading.Lock()
def contact_url(listing_id):return f'https://www.airbnb.co.uk/contact_host/{listing_id}/send_message'
def _ctx(p,headless=True):
    return p.chromium.launch_persistent_context(str(PROFILE),headless=headless,locale='en-GB',viewport={'width':1280,'height':900},
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36')
def status():
    """Logged in? Airbnb sets the `_aat` cookie for authenticated sessions."""
    if not _lock.acquire(blocking=False):return {'connected':False,'busy':True}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            c=_ctx(p);ok=any(k['name']=='_aat' and k.get('value') for k in c.cookies('https://www.airbnb.co.uk'));c.close()
        return {'connected':ok}
    except Exception as e:return {'connected':False,'error':f'{type(e).__name__}: {str(e)[:120]}'}
    finally:_lock.release()
def connect():
    """Open a headed browser on the Airbnb login page; the user logs in and closes it. Runs in a thread."""
    def run():
        from playwright.sync_api import sync_playwright
        with _lock:
            with sync_playwright() as p:
                c=_ctx(p,headless=False);pg=c.pages[0] if c.pages else c.new_page();pg.goto('https://www.airbnb.co.uk/login')
                try:
                    while len(c.pages)>0:
                        if any(k['name']=='_aat' and k.get('value') for k in c.cookies('https://www.airbnb.co.uk')):
                            pg.wait_for_timeout(1500);break
                        pg.wait_for_timeout(1000)
                except Exception:pass
                try:c.close()
                except Exception:pass
    threading.Thread(target=run,daemon=True).start()
def disconnect():
    if PROFILE.exists():shutil.rmtree(PROFILE,ignore_errors=True);PROFILE.mkdir(parents=True,exist_ok=True)
def open_draft(listing_id,message):
    """Open the contact-host form in a headed window with the message pre-filled. The user presses Send.
    Returns (status, info): draft | failed. Never clicks Send."""
    from playwright.sync_api import sync_playwright
    def run():
        with _lock:
            with sync_playwright() as p:
                c=_ctx(p,headless=False);pg=c.pages[0] if c.pages else c.new_page()
                try:
                    pg.goto(contact_url(listing_id),wait_until='domcontentloaded',timeout=60000)
                    box=pg.get_by_role('textbox',name=re.compile('Message the host',re.I));box.wait_for(timeout=20000);box.fill(message)
                    while len(c.pages)>0:pg.wait_for_timeout(1000)
                except Exception:pass
                try:c.close()
                except Exception:pass
    threading.Thread(target=run,daemon=True).start()
    return 'draft','opened pre-filled in a browser window — review and press Send message'
# ---------------- public link / tunnel ----------------
_tunnel={'proc':None,'url':None,'log':[]}
def tunnel_status():
    pr=_tunnel['proc'];running=bool(pr and pr.poll() is None)
    return {'running':running,'url':_tunnel['url'] if running else None,'installed':bool(shutil.which('cloudflared'))}
def tunnel_start(port):
    if tunnel_status()['running']:return tunnel_status()
    if not shutil.which('cloudflared'):return {'running':False,'url':None,'installed':False,'error':'cloudflared not installed (brew install cloudflared)'}
    pr=subprocess.Popen(['cloudflared','tunnel','--url',f'http://localhost:{port}','--no-autoupdate'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    _tunnel.update(proc=pr,url=None,log=[])
    def reader():
        for line in pr.stdout:
            _tunnel['log'].append(line.rstrip()[-200:]);m=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
            if m:_tunnel['url']=m.group(0)
    threading.Thread(target=reader,daemon=True).start()
    for _ in range(60):
        if _tunnel['url']:break
        time.sleep(0.5)
    return tunnel_status()
def tunnel_stop():
    pr=_tunnel['proc']
    if pr and pr.poll() is None:pr.terminate()
    _tunnel.update(proc=None,url=None);return tunnel_status()
def public_base():
    b=(os.getenv('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if b:return b
    t=tunnel_status();return t['url'] if t['running'] and t['url'] else None
