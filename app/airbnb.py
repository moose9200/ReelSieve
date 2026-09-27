"""Every request ReelSieve makes to Airbnb (airbnb.* pages, muscache.com photos), from any process, passes here:
the kill switch, the block cool-down, and one rate limit in PostgreSQL shared by the web and every worker.

A block (403, 429, 451, 503 or a challenge page) records the time, pauses all Airbnb fetching everywhere for
AIRBNB_BLOCK_COOLDOWN_MIN and raises Unavailable, which callers let through: no retry by another route.
"""
import logging
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from app import database

BLOCKED = 'Airbnb is not serving this page to us right now. Try again later.'
DISABLED = 'ReelSieve is not fetching from Airbnb right now. Try again later.'
BLOCK_STATUSES = (403, 429, 451, 503)
# Conservative: bot-wall challenge markers only. A normal Airbnb page mentions "datadome" and "recaptcha" in its config.
CHALLENGE = re.compile(r'captcha-delivery\.com|px-captcha|_Incapsula_Resource|/cdn-cgi/challenge-platform|'
                       r'<title>\s*(?:Access Denied|Pardon Our Interruption|Just a moment\.\.\.)\s*</title>', re.I)
PAGE_HOST = re.compile(r'(?:[a-z0-9-]+\.)*airbnb\.[a-z]{2,3}(?:\.[a-z]{2})?')
# Reserve the next slot in one statement (the UPDATE locks the row), on the database clock so hosts need not agree.
RESERVE = ('UPDATE airbnb_rate SET next_at=GREATEST(next_at, extract(epoch FROM clock_timestamp())) + %s WHERE bucket=%s '
           'RETURNING next_at - %s - extract(epoch FROM clock_timestamp()) AS wait')
log = logging.getLogger('reelsieve.airbnb')


class Unavailable(RuntimeError):
    """Airbnb fetching is stopped: a block, its cool-down, or the kill switch. The message is safe to show."""


def enabled():
    return os.getenv('AIRBNB_FETCH_ENABLED', '1').strip() != '0'


def kind(url):
    host = (urlsplit(url or '').hostname or '').lower()
    if PAGE_HOST.fullmatch(host):
        return 'page'
    return 'image' if host == 'muscache.com' or host.endswith('.muscache.com') else None


# Requests per second in each shared budget: listing and search pages, photos, and the scripts and data calls the
# headless reviews page makes (about 190 of them; a person's browser loads the same in about 3 s).
RATES = {'page': 'AIRBNB_PAGE_RPS', 'image': 'AIRBNB_IMAGE_RPS', 'browser': 'AIRBNB_BROWSER_RPS'}
DEFAULTS = {'AIRBNB_FETCH_ENABLED': '1', 'AIRBNB_BLOCK_COOLDOWN_MIN': '30', 'AIRBNB_PAGE_RPS': '1', 'AIRBNB_IMAGE_RPS': '10',
            'AIRBNB_BROWSER_RPS': '20'}


def setting(k):
    return (os.getenv(k) or '').strip() or DEFAULTS[k]


def interval(bucket):
    """Seconds between two requests of this budget across every process."""
    return 1 / float(setting(RATES[bucket]))


def cooldown():
    return float(setting('AIRBNB_BLOCK_COOLDOWN_MIN')) * 60


def last_block(conn=None):
    """The last block with the time fetching resumes, or None."""
    with database.transaction(conn) as c:
        row = c.execute('SELECT ts,status,host,reason FROM airbnb_blocks WHERE id=1').fetchone()
    return {**row, 'until': row['ts'] + cooldown()} if row else None


def _refuse_if_paused(c):
    b = last_block(c)
    if b and time.time() < b['until']:
        raise Unavailable(BLOCKED)


def gate(url, bucket=None):
    """Before every request: kill switch (pages), cool-down, then wait for this request's slot in the shared budget.
    bucket: the budget the slot comes from; by default the host's kind ('page' or 'image')."""
    k = kind(url)
    if not k:
        return
    if k == 'page' and not enabled():
        raise Unavailable(DISABLED)
    bucket = bucket or k
    step = interval(bucket)
    with database.connect() as c:
        _refuse_if_paused(c)
        wait = c.execute(RESERVE, (step, bucket, step)).fetchone()['wait']
    if wait > 0:
        time.sleep(wait)
        with database.connect() as c:  # a block may have happened while this request waited its turn
            _refuse_if_paused(c)


def check(url, status, body=None, content_type=''):
    """After every response: a block status, or a challenge page, records the block and stops this fetch path."""
    if not kind(url):
        return
    if status in BLOCK_STATUSES:
        reason = 'status'
    elif body and 'html' in (content_type or '').lower() and CHALLENGE.search(
            body if isinstance(body, str) else body.decode('utf-8', 'replace')):
        reason = 'challenge'
    else:
        return
    host = urlsplit(url).hostname
    with database.connect() as c:
        c.execute('INSERT INTO airbnb_blocks(id,ts,status,host,reason) VALUES(1,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE '
                  'SET ts=EXCLUDED.ts,status=EXCLUDED.status,host=EXCLUDED.host,reason=EXCLUDED.reason',
                  (time.time(), status, host, reason))
    log.warning('Airbnb block: %s answered %s (%s); all Airbnb fetching paused for %.0f min', host, status, reason, cooldown() / 60)
    raise Unavailable(BLOCKED)


def get(url, headers=None, timeout=40):
    """GET an Airbnb page. Every hop, redirects included, is gated and checked; the final page is checked for a challenge."""
    hooks = {'request': [lambda req: gate(str(req.url))], 'response': [lambda r: check(str(r.request.url), r.status_code)]}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks=hooks) as client:
        r = client.get(url)
    check(str(r.url), r.status_code, r.text, r.headers.get('content-type', ''))
    return r
