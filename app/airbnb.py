"""Every request ReelSieve makes to Airbnb (airbnb.* pages, muscache.com photos), from any process, passes here:
the kill switch, the block cool-down, and one rate limit in PostgreSQL shared by the web and every worker.

A block (401, 403, 407, 429, 451, 503 or a challenge page) is recorded in an append-only history, pauses all Airbnb
fetching everywhere for AIRBNB_BLOCK_COOLDOWN_MIN and raises Unavailable, which callers let through: no retry by
another route. A 451, or a block again after a pause ended, stops Airbnb fetching until an admin resumes it.
"""
import contextvars
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from app import database

BLOCKED = 'Airbnb is not serving this page to us right now. Try again later.'
DISABLED = 'ReelSieve is not fetching from Airbnb right now. Try again later, or make a reel from your own photos instead.'
BUSY = 'Airbnb lookups are busy right now. Try again in a minute.'
BLOCK_STATUSES = (401, 403, 407, 429, 451, 503)
HARD_STATUSES = (451,)  # Unavailable For Legal Reasons: never resumed on a timer
REPEAT_WINDOW = 24 * 3600  # a block within this long of an earlier one, after that one's pause ended, is a hard stop
# Conservative: bot-wall challenge markers only. A normal Airbnb page mentions "datadome" and "recaptcha" in its config.
CHALLENGE = re.compile(r'captcha-delivery\.com|px-captcha|_Incapsula_Resource|/cdn-cgi/challenge-platform|'
                       r'<title>\s*(?:Access Denied|Pardon Our Interruption|Just a moment\.\.\.)\s*</title>', re.I)
PAGE_HOST = re.compile(r'(?:[a-z0-9-]+\.)*airbnb\.[a-z]{2,3}(?:\.[a-z]{2})?')
# Reserve the next slot in one statement (the UPDATE locks the row), on the database clock so hosts need not agree.
# No row back = the wait would pass the caller's limit: nothing is booked.
RESERVE = ('UPDATE airbnb_rate SET next_at=GREATEST(next_at, extract(epoch FROM clock_timestamp())) + %(step)s '
           'WHERE bucket=%(bucket)s AND next_at - extract(epoch FROM clock_timestamp()) <= %(limit)s '
           'RETURNING next_at - %(step)s - extract(epoch FROM clock_timestamp()) AS wait')
# The longest a request may queue for its slot. The web sets WEB_MAX_WAIT for every request it serves, so user
# lookups never hold a web thread for long and never push a render job back by more than that; jobs queue up to
# JOB_MAX_WAIT behind them.
WEB_MAX_WAIT, JOB_MAX_WAIT = 10, 120
max_wait = contextvars.ContextVar('airbnb_max_wait', default=JOB_MAX_WAIT)
OFF, ON = ('0', 'false', 'off', 'no'), ('1', 'true', 'on', 'yes')
log = logging.getLogger('reelsieve.airbnb')


class Unavailable(RuntimeError):
    """Airbnb fetching is stopped: a block, its cool-down, the kill switch or a full queue. The message is safe to show."""


def enabled():
    return setting('AIRBNB_FETCH_ENABLED').lower() not in OFF


def validate():
    """At start: refuse a setting that would fail silently (a switch left on) or break every request (a rate of 0)."""
    if setting('AIRBNB_FETCH_ENABLED').lower() not in OFF + ON:
        raise RuntimeError('AIRBNB_FETCH_ENABLED must be 1 (on) or 0 (off)')
    for k in (*RATES.values(), 'AIRBNB_BLOCK_COOLDOWN_MIN'):
        try:
            ok = 0 < float(setting(k)) < math.inf
        except ValueError:
            ok = False
        if not ok:
            raise RuntimeError(f'{k} must be a number above 0')


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


def state(conn=None):
    """The newest block (or None), when its pause ends, whether an admin must resume (hard) and whether fetching is
    stopped now. Resuming clears every block, so an uncleared newest block is the one that counts."""
    with database.transaction(conn) as c:
        last = c.execute('SELECT * FROM airbnb_blocks ORDER BY ts DESC, id DESC LIMIT 1').fetchone()
        hard = bool(c.execute('SELECT 1 FROM airbnb_blocks WHERE hard AND cleared_at IS NULL LIMIT 1').fetchone())
    until = last['ts'] + cooldown() if last else None
    return {'last': last, 'until': until, 'hard': hard,
            'stopped': hard or bool(last and last['cleared_at'] is None and time.time() < until)}


def resume():
    """An admin checked: clear every block so fetching starts again. The history stays."""
    with database.connect() as c:
        return c.execute('UPDATE airbnb_blocks SET cleared_at=%s WHERE cleared_at IS NULL', (time.time(),)).rowcount


def _refuse_if_paused(c):
    if state(c)['stopped']:
        raise Unavailable(BLOCKED)


def gate(url, bucket=None):
    """Before every request: kill switch, cool-down, then wait for this request's slot in the shared budget, or refuse
    at once (BUSY, nothing booked) when the queue is longer than max_wait. bucket: the budget the slot comes from; by
    default the host's kind ('page' or 'image')."""
    k = kind(url)
    if not k:
        return
    if not enabled():
        raise Unavailable(DISABLED)
    bucket = bucket or k
    step = interval(bucket)
    with database.connect() as c:
        _refuse_if_paused(c)
        row = c.execute(RESERVE, {'step': step, 'bucket': bucket, 'limit': max_wait.get()}).fetchone()
    if not row:
        raise Unavailable(BUSY)
    wait = row['wait']
    if wait > 0:
        time.sleep(wait)
        with database.connect() as c:  # a block may have happened while this request waited its turn
            _refuse_if_paused(c)


def check(url, status, body=None, content_type='', record=True):
    """After every response: a block status, or a challenge page, records the block and stops this fetch path.
    record=False, for a URL a user chose (the image proxy): nothing is recorded or raised, and the caller treats the
    answer as a failed fetch, so one odd URL cannot pause the service for everyone."""
    if not kind(url) or not record:
        return
    if status in BLOCK_STATUSES:
        reason = 'status'
    elif body and 'html' in (content_type or '').lower() and CHALLENGE.search(
            body if isinstance(body, str) else body.decode('utf-8', 'replace')):
        reason = 'challenge'
    else:
        return
    host, now = urlsplit(url).hostname, time.time()
    with database.connect() as c:
        prior = c.execute('SELECT max(ts) AS ts FROM airbnb_blocks WHERE cleared_at IS NULL AND ts>%s',
                          (now - REPEAT_WINDOW,)).fetchone()['ts']
        # Refused again after a pause ended: stop until a person has checked. Refusals of requests already in flight
        # when the first came (inside its pause) are the same block.
        hard = status in HARD_STATUSES or (prior is not None and now - prior >= cooldown())
        c.execute('INSERT INTO airbnb_blocks(ts,status,host,reason,hard) VALUES(%s,%s,%s,%s,%s)', (now, status, host, reason, hard))
    if hard:
        log.error('Airbnb block: %s answered %s (%s); all Airbnb fetching stopped until an admin resumes it', host, status, reason)
    else:
        log.warning('Airbnb block: %s answered %s (%s); all Airbnb fetching paused for %.0f min', host, status, reason, cooldown() / 60)
    raise Unavailable(BLOCKED)


def get(url, headers=None, timeout=40):
    """GET an Airbnb page. Every hop, redirects included, is gated and checked; the final page is checked for a challenge."""
    hooks = {'request': [lambda req: gate(str(req.url))], 'response': [lambda r: check(str(r.request.url), r.status_code)]}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks=hooks) as client:
        r = client.get(url)
    check(str(r.url), r.status_code, r.text, r.headers.get('content-type', ''))
    return r
