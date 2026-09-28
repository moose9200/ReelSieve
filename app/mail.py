"""The one email ReelSieve sends: a password-reset link, through Resend.

POST https://api.resend.com/emails with a Bearer RESEND_API_KEY and a from address in RESEND_FROM, over the same
httpx client the rest of the app uses (no new dependency, no SDK). Off until both variables are set: the forgot
page then says so and points at the fallbacks instead of pretending an email is on its way.
Nothing here logs the address, the body or the key.
"""
import os

import httpx

API = 'https://api.resend.com/emails'
TIMEOUT = 20


def _get(name):
    return (os.getenv(name) or '').strip()


def enabled():
    return bool(_get('RESEND_API_KEY') and _get('RESEND_FROM'))


def send(to, subject, text):
    """True when Resend accepted the message. Never raises, never carries the key or the address into a log line."""
    if not enabled():
        return False
    try:
        with httpx.Client(timeout=TIMEOUT) as h:
            r = h.post(API, headers={'Authorization': 'Bearer ' + _get('RESEND_API_KEY')},
                       json={'from': _get('RESEND_FROM'), 'to': [to], 'subject': subject, 'text': text})
    except httpx.HTTPError:
        print('{"mail": "transport failed"}', flush=True)
        return False
    if r.status_code >= 300:
        print('{"mail": "refused", "status": %d}' % r.status_code, flush=True)
        return False
    return True
