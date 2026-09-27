"""Synthetic Google OAuth/Drive endpoints behind HTTPX MockTransport. Only the network boundary is fake."""
import json
import threading
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
        self.folders = {}   # id -> {name, parents, appProperties}
        self.metas = {}     # uploaded file id -> the metadata it was created with
        self.blobs = {}     # uploaded file id -> bytes, served back by alt=media
        self.fail_upload_after = None  # fail every upload session after this many succeeded
        self.lock = threading.RLock()

    def app_folder(self):
        return next(fid for fid, f in self.folders.items() if 'kind' not in f['appProperties'])

    def handle(self, req):
        with self.lock:  # photo uploads run in parallel threads
            return self._handle(req)

    def _handle(self, req):
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
            q = req.url.params['q']
            self.folder_queries.append(q)
            found = [{'id': fid} for fid, f in self.folders.items()
                     if "key='kind' and value='inputs'" in q and f['appProperties'].get('kind') == 'inputs'
                     and 'job' not in f['appProperties'] and f"'{f['parents'][0]}' in parents" in q]
            return httpx.Response(200, json={'files': found})
        if path == '/drive/v3/files' and req.method == 'POST':
            meta = json.loads(req.content)
            props = meta['appProperties']
            fid = 'folder-' + props['owner'] if 'kind' not in props else 'folder-inputs-' + props.get('job', props['owner'])
            self.folders[fid] = {'name': meta['name'], 'parents': meta.get('parents') or [], 'appProperties': props}
            return httpx.Response(200, json={'id': fid})
        if path == '/upload/drive/v3/files':
            meta = json.loads(req.content)
            if 'id' not in meta:
                meta['id'] = 'generated-' + str(self.next_id)
                self.next_id += 1
            if self.fail_upload_after is not None and len(self.blobs) >= self.fail_upload_after:
                return httpx.Response(500, json={'error': 'sensitive-do-not-leak'})
            self.metas[meta['id']] = meta
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
                if s['meta'].get('mimeType') == 'image/jpeg':  # a reel's photos; kept so alt=media can serve them
                    self.blobs[info['id']] = s['data']
                if self.interrupt:
                    self.interrupt = False
                    raise httpx.ReadTimeout('sensitive-session-url', request=req)
                return httpx.Response(200, json={} if self.bad_receipt else info)
            return httpx.Response(308, headers={'Range': 'bytes=0-' + str(size + 1 if self.bad_range else len(s['data']) - 1)})
        if path.startswith('/drive/v3/files/'):
            fid = path.rsplit('/', 1)[-1]
            if req.method == 'DELETE':  # permanent; a folder takes its contents with it
                if fid not in self.folders and fid not in self.files:
                    return httpx.Response(404, json={})
                self.folders.pop(fid, None)
                for gone in [f for f, m in self.metas.items() if f == fid or fid in (m.get('parents') or [])]:
                    self.metas.pop(gone), self.files.pop(gone, None), self.blobs.pop(gone, None)
                return httpx.Response(204)
            if req.url.params.get('alt') == 'media':
                if fid in self.blobs:  # a reel's photos; videos keep the canned reply
                    return httpx.Response(200, content=self.blobs[fid], headers={'Content-Type': 'image/jpeg'})
                if fid not in self.files:
                    return httpx.Response(404, json={})
                return httpx.Response(206 if 'Range' in req.headers else 200, content=b'video', headers={'Content-Type': 'video/mp4', 'Content-Range': 'bytes 0-4/5'})
            return httpx.Response(200, json=self.files[fid]) if fid in self.files else httpx.Response(404, json={})
        raise AssertionError('Unexpected synthetic Google request: ' + path)


def connect(owners, google, name='alice'):
    user = name + '@example.test'
    url = gdrive.auth_url('https://app.test/callback', user, owners[name])
    state = parse_qs(urlparse(url).query)['state'][0]
    return gdrive.exchange('synthetic-code', state, 'https://app.test/callback', user, owners[name])
