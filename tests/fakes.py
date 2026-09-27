"""Synthetic Google OAuth/Drive endpoints behind HTTPX MockTransport. Only the network boundary is fake."""
import json
from urllib.parse import parse_qs, urlparse

import httpx

from app import gdrive


class Google:
    def __init__(self):
        self.scope = 'https://www.googleapis.com/auth/drive.file openid email'
        self.sub = 'google-alice'
        self.refresh = 'synthetic-refresh-secret'
        self.calls = []
        self.files = {}
        self.sessions = {}
        self.next_id = 1
        self.hook = None
        self.fail_revoke = False
        self.fail_permission = False
        self.bad_receipt = False
        self.bad_range = False
        self.interrupt = False
        self.refresh_error = False
        self.drop_chunk = False
        self.folder_queries = []

    def handle(self, req):
        self.calls.append(req)
        if self.hook:
            hook, self.hook = self.hook, None
            hook(req)
        path = req.url.path
        if path == '/token':
            if self.refresh_error and b'grant_type=refresh_token' in req.content:
                return httpx.Response(400, json={'error': 'invalid_grant', 'error_description': 'sensitive-do-not-leak'})
            tok = {'access_token': 'synthetic-access-secret', 'expires_in': 3600, 'scope': self.scope}
            if self.refresh:
                tok['refresh_token'] = self.refresh
            return httpx.Response(200, json=tok)
        if path == '/oauth2/v3/userinfo':
            return httpx.Response(200, json={'sub': self.sub, 'email': self.sub + '@gmail.test', 'email_verified': True})
        if path == '/revoke':
            return httpx.Response(500 if self.fail_revoke else 200)
        if path.endswith('/generateIds'):
            fid = 'generated-' + str(self.next_id)
            self.next_id += 1
            return httpx.Response(200, json={'ids': [fid]})
        if '/permissions' in path:
            if self.fail_permission:
                return httpx.Response(403, json={'error': 'sensitive-do-not-leak'})
            if req.method == 'GET':
                return httpx.Response(200, json={'permissions': [{'id': 'anyone', 'type': 'anyone', 'role': 'reader'}]})
            return httpx.Response(200, json={'id': 'anyone', 'type': 'anyone', 'role': 'reader'})
        if path == '/drive/v3/files' and req.method == 'GET':
            self.folder_queries.append(req.url.params['q'])
            return httpx.Response(200, json={'files': []})
        if path == '/drive/v3/files' and req.method == 'POST':
            meta = json.loads(req.content)
            return httpx.Response(200, json={'id': 'folder-' + meta['appProperties']['owner']})
        if path == '/upload/drive/v3/files':
            meta = json.loads(req.content)
            session = 'https://www.googleapis.com/upload/session/' + meta['id']
            self.sessions[session] = {'meta': meta, 'data': b''}
            return httpx.Response(200, headers={'Location': session})
        if path.startswith('/upload/session/'):
            s = self.sessions[str(req.url)]
            cr = req.headers['Content-Range']
            if cr.startswith('bytes */'):
                if len(s['data']) == int(cr.split('/')[-1]):
                    return httpx.Response(200, json=self.files[s['meta']['id']])
                return httpx.Response(308, headers={'Range': 'bytes=0-' + str(len(s['data']) - 1)} if s['data'] else {})
            if self.drop_chunk:
                self.drop_chunk = False
                return httpx.Response(308)
            start = int(cr.split(' ')[1].split('-')[0])
            s['data'] = s['data'][:start] + req.content
            size = int(cr.split('/')[-1])
            if len(s['data']) == size:
                info = {'id': s['meta']['id'], 'name': s['meta']['name'], 'size': str(size),
                        'webViewLink': 'https://drive.google.com/file/d/' + s['meta']['id'],
                        'appProperties': s['meta']['appProperties']}
                self.files[info['id']] = info
                if self.interrupt:
                    self.interrupt = False
                    raise httpx.ReadTimeout('sensitive-session-url', request=req)
                return httpx.Response(200, json={} if self.bad_receipt else info)
            return httpx.Response(308, headers={'Range': 'bytes=0-' + str(size + 1 if self.bad_range else len(s['data']) - 1)})
        if path.startswith('/drive/v3/files/'):
            fid = path.rsplit('/', 1)[-1]
            if req.url.params.get('alt') == 'media':
                return httpx.Response(206 if 'Range' in req.headers else 200, content=b'video', headers={'Content-Type': 'video/mp4', 'Content-Range': 'bytes 0-4/5'})
            return httpx.Response(200, json=self.files[fid]) if fid in self.files else httpx.Response(404, json={})
        raise AssertionError('Unexpected synthetic Google request: ' + path)


def connect(owners, google, name='alice'):
    user = name + '@example.test'
    url = gdrive.auth_url('https://app.test/callback', user, owners[name])
    state = parse_qs(urlparse(url).query)['state'][0]
    return gdrive.exchange('synthetic-code', state, 'https://app.test/callback', user, owners[name])


JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64
# A normal listing page. Like the real one (fetched 27 Sep 2026) its config mentions "datadome" and "recaptcha";
# neither may be read as a challenge page.
LISTING = ('<html><title>Sea view flat - Flats for Rent in Poole - Airbnb</title>'
           '<script type="application/json">{"datadome_integration":{"enabled":true},"disable_google_recaptcha":true,'
           '"city":"Poole","visibleReviewCount":"12","pdpContext":{"hostId":"987654321"},"listingsCount":4,'
           '"accessibilityLabel":"Living room","baseUrl":"https://a0.muscache.com/im/pictures/hosting/a.jpeg",'
           '"accessibilityLabel":"Kitchen","baseUrl":"https://a0.muscache.com/im/pictures/hosting/b.jpeg"}</script>'
           'Hosted by Leo</html>')
CHALLENGE = ('<html><head><title>airbnb.co.uk</title></head><body><script>var dd={"rt":"c","cid":"x"}</script>'
             '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>')


class Airbnb:
    """Synthetic Airbnb pages and photo CDN behind HTTPX MockTransport.
    status / pages: path prefix -> status code / HTML body. down: hosts that fail with a network error."""
    def __init__(self):
        self.calls, self.status, self.pages, self.down = [], {}, {}, set()

    def handle(self, req):
        host, path = req.url.host, req.url.path
        self.calls.append(host + path)
        if host in self.down:
            raise httpx.ConnectError('synthetic network error', request=req)
        for prefix, code in self.status.items():
            if path.startswith(prefix):
                return httpx.Response(code, text='<html>Denied</html>', headers={'content-type': 'text/html'})
        for prefix, body in self.pages.items():
            if path.startswith(prefix):
                return httpx.Response(200, text=body, headers={'content-type': 'text/html; charset=utf-8'})
        if host.endswith('muscache.com'):
            return httpx.Response(200, content=JPEG, headers={'content-type': 'image/jpeg'})
        if host.startswith('www.airbnb.') and path.startswith('/rooms/'):
            return httpx.Response(200, text=LISTING, headers={'content-type': 'text/html; charset=utf-8'})
        raise AssertionError('Unexpected synthetic Airbnb request: ' + host + path)
