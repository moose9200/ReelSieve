"""PostgreSQL identities, versioned signed sessions and shared login limits."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import uuid

from psycopg.errors import UniqueViolation
from app import database

COOKIE = 'reelsieve_session'
LONG_TTL = int(os.getenv('SESSION_TTL_DAYS', '30')) * 86400
SHORT_TTL = 12 * 3600
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# OWASP Password Storage Cheat Sheet (fetched 26 Sep 2026): "PBKDF2-HMAC-SHA256: 600,000 iterations (recommended)".
# Older hashes keep their own count and are upgraded on the next successful sign-in.
ITERATIONS = 600_000


def norm(user):
    return (user or '').strip().lower()


def _hash(pw, salt, iterations=ITERATIONS):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), iterations).hex()


def secret():
    value = os.getenv('SESSION_SECRET')
    if not value:
        raise RuntimeError('SESSION_SECRET is required')
    return value


def users():
    with database.connect() as c:
        return c.execute('SELECT email AS "user", role, created FROM users WHERE active ORDER BY email').fetchall()


def validate_password(pw):
    if len(pw) < 8:
        raise ValueError('Password must be at least 8 characters')
    if pw.lower() in ('password', '12345678', 'qwertyui'):
        raise ValueError('Choose a less common password')


def create_user(user, pw=None, role='member', braivex_customer_id=None):
    """The one way an account is made. A customer (member) signs in with Braivex and has no password at all: salt and
    hash stay empty, which no PBKDF2 output equals. Only an operator (admin) has a password, for break-glass sign-in,
    and Braivex sign-in never creates one."""
    user = norm(user)
    if not EMAIL.match(user):
        raise ValueError('Enter a valid email address')
    if role not in ('member', 'admin'):
        raise ValueError('Unknown role')
    if role == 'member' and pw is not None:
        raise ValueError('Customers sign in with Braivex and have no password')
    salt, digest = '', ''
    if role == 'admin':
        validate_password(pw or '')
        salt = secrets.token_hex(16)
        digest = _hash(pw, salt)
    try:
        with database.connect() as c:
            c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created,braivex_customer_id) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                      (str(uuid.uuid4()), user, salt, digest, ITERATIONS, role, time.time(), braivex_customer_id))
    except UniqueViolation:
        raise ValueError('That email already has an account') from None


def identity(user):
    """The signed-in-able account for that address, or None."""
    with database.connect() as c:
        return c.execute('SELECT email,role,braivex_customer_id FROM users WHERE email=%s AND active',
                         (norm(user),)).fetchone()


def by_braivex(braivex_customer_id):
    """The account that Shopify customer already has, or None. `sub` is the identity; an email can change."""
    with database.connect() as c:
        return c.execute('SELECT email,role,braivex_customer_id FROM users WHERE braivex_customer_id=%s AND active',
                         (braivex_customer_id,)).fetchone()


def link_braivex(user, braivex_customer_id):
    """Every Braivex sign-in to an existing customer account goes through here, and leaves it linked to that Shopify
    customer with no password: a hash still on the row is wiped and the sessions it may have made end.
    A row with no Shopify customer yet was made with a password, which never proved the mailbox, so this is a takeover
    by the person Braivex just verified: its sessions end, and the prior holder's business sender, invite attribution
    and invite code go (the caller disconnects their Google Drive first: that is a call to Google).
    False, changing nothing, for a row linked to another Shopify customer (sub never changes, so it is never
    re-pointed), an operator (Braivex sign-in never grants operator rights), or a Shopify customer another row holds."""
    try:
        with database.connect() as c:
            row = c.execute("SELECT id,braivex_customer_id,hash FROM users WHERE email=%s AND active AND role='member' "
                            'FOR UPDATE', (norm(user),)).fetchone()
            if not row or row['braivex_customer_id'] not in (None, braivex_customer_id):
                return False
            takeover = row['braivex_customer_id'] is None
            if takeover or row['hash']:
                c.execute("UPDATE users SET braivex_customer_id=%s,salt='',hash='',changed=%s,"
                          'session_version=session_version+1 WHERE id=%s', (braivex_customer_id, time.time(), row['id']))
            if takeover:
                c.execute('UPDATE accounts SET b2b_sender=NULL,referral_code=DEFAULT WHERE owner_id=%s', (row['id'],))
                c.execute('DELETE FROM referrals WHERE referee_id=%s AND rewarded_at IS NULL', (row['id'],))
            return True
    except UniqueViolation:
        return False


def delete_user(user, by):
    """Deactivate login while retaining the durable owner and business audit history.
    by=None is the operator console (a shell in the service), which has no admin account."""
    user, by = norm(user), (norm(by) if by is not None else None)
    if user == by:
        raise ValueError("You can't remove your own account")
    with database.connect() as c:
        # Serialize the last-admin check across application instances.
        c.execute("SELECT pg_advisory_xact_lock(hashtext('reelsieve-admin-membership'))")
        if by is not None:
            actor = c.execute('SELECT role FROM users WHERE email=%s AND active', (by,)).fetchone()
            if not actor or actor['role'] != 'admin':
                raise ValueError('Admin only')
        row = c.execute('SELECT role FROM users WHERE email=%s AND active FOR UPDATE', (user,)).fetchone()
        if not row:
            raise ValueError('No such user')
        if row['role'] == 'admin' and c.execute("SELECT count(*) AS n FROM users WHERE role='admin' AND active").fetchone()['n'] <= 1:
            raise ValueError('Keep at least one admin')
        c.execute('UPDATE users SET active=FALSE,deactivated_at=%s,session_version=session_version+1 WHERE email=%s', (time.time(), user))


def begin_erase(user, by=None):
    """First step of erasure: end sign-in for good (the account may already be deactivated). Returns the owner id.
    by: the admin doing it, the account itself (self-service) or None (operator console, retention)."""
    user, by = norm(user), (norm(by) if by is not None else None)
    with database.connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('reelsieve-admin-membership'))")
        if by is not None and by != user:
            actor = c.execute('SELECT role FROM users WHERE email=%s AND active', (by,)).fetchone()
            if not actor or actor['role'] != 'admin':
                raise ValueError('Admin only')
        row = c.execute('SELECT id,role,active FROM users WHERE email=%s AND erased_at IS NULL FOR UPDATE', (user,)).fetchone()
        if not row:
            raise ValueError('No such user')
        if row['active'] and row['role'] == 'admin' and \
                c.execute("SELECT count(*) AS n FROM users WHERE role='admin' AND active").fetchone()['n'] <= 1:
            raise ValueError('Keep at least one admin')
        c.execute('UPDATE users SET active=FALSE,deactivated_at=COALESCE(deactivated_at,%s),session_version=session_version+1 '
                  'WHERE id=%s', (time.time(), row['id']))
        return row['id']


def set_password(user, pw):
    """An operator's break-glass password (admin console or another operator). Customers have none to set."""
    validate_password(pw)
    salt = secrets.token_hex(16)
    with database.connect() as c:
        row = c.execute("UPDATE users SET salt=%s,hash=%s,iterations=%s,changed=%s,session_version=session_version+1 "
                        "WHERE email=%s AND active AND role='admin' RETURNING id",
                        (salt, _hash(pw, salt), ITERATIONS, time.time(), norm(user))).fetchone()
        if not row:
            raise ValueError('No such operator')


def role(user):
    with database.connect() as c:
        row = c.execute('SELECT role FROM users WHERE email=%s AND active', (norm(user),)).fetchone()
        return row['role'] if row else 'member'


def verify(user, pw):
    """Operator break-glass only: True for an active admin's right password. A customer's old password never
    verifies, whatever it is, and an unknown or customer address costs the same hash time as an operator's."""
    with database.connect() as c:
        row = c.execute("SELECT id,salt,hash,iterations FROM users WHERE email=%s AND active AND role='admin'",
                        (norm(user),)).fetchone()
    if not row:
        _hash(pw, '00' * 16)
        return False
    if not hmac.compare_digest(_hash(pw, row['salt'], row['iterations']), row['hash']):
        return False
    if row['iterations'] < ITERATIONS:
        # Same password, stronger hash. Not a password change: the session version stays, nobody is signed out.
        salt = secrets.token_hex(16)
        with database.connect() as c:
            c.execute('UPDATE users SET salt=%s,hash=%s,iterations=%s WHERE id=%s AND hash=%s',
                      (salt, _hash(pw, salt), ITERATIONS, row['id'], row['hash']))
    return True


def _ip_key(ip, purpose='login'):
    """IPv6: one /64 is one subscriber, and rotating within it must not reset the count. IPv4 stays per address so a
    shared office or phone NAT is not locked out by one person's typos."""
    try:
        a = ipaddress.ip_address(str(ip))
        ip = ipaddress.ip_network(f'{a}/64', strict=False) if a.version == 6 else a
    except ValueError:
        pass
    return hmac.new(secret().encode(), (purpose + '|' + str(ip)).encode(), hashlib.sha256).hexdigest()


def too_many(ip, purpose='login'):
    """5 per 10 minutes per address. purpose keeps limits apart: 'login' failures, 'privacy' request submissions."""
    with database.connect() as c:
        return c.execute('SELECT count(*) AS n FROM login_failures WHERE ip_hash=%s AND ts>%s',
                         (_ip_key(ip, purpose), time.time() - 600)).fetchone()['n'] >= 5


def record_fail(ip, purpose='login'):
    with database.connect() as c:
        c.execute('DELETE FROM login_failures WHERE ts<%s', (time.time() - 600,))
        c.execute('INSERT INTO login_failures(ip_hash,ts) VALUES(%s,%s)', (_ip_key(ip, purpose), time.time()))


def clear_fails(ip):
    with database.connect() as c:
        c.execute('DELETE FROM login_failures WHERE ip_hash=%s', (_ip_key(ip),))


def seal(purpose, ttl, **data):
    """The one signed-token codec (sessions and the Braivex sign-in cookies): the values as JSON, then an HMAC of
    them under SESSION_SECRET. purpose is signed in, so a token minted for one use is never accepted for another."""
    body = base64.urlsafe_b64encode(json.dumps({**data, 'p': purpose, 'exp': int(time.time()) + ttl},
                                               separators=(',', ':')).encode()).decode().rstrip('=')
    return body + '.' + hmac.new(secret().encode(), body.encode(), hashlib.sha256).hexdigest()


def unseal(purpose, token):
    """What seal(purpose, ...) put there, or None: a wrong signature or purpose, a mangled token and an expired one
    are all None, never an error."""
    body, _, sig = (token or '').partition('.')
    want = hmac.new(secret().encode(), body.encode('utf-8', 'replace'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want.encode(), sig.encode('utf-8', 'replace')):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    except (ValueError, TypeError, UnicodeError):
        return None
    ok = isinstance(data, dict) and data.get('p') == purpose and isinstance(data.get('exp'), int) and data['exp'] > time.time()
    return data if ok else None


def issue(user, long=True):
    with database.connect() as c:
        row = c.execute('SELECT id,session_version FROM users WHERE email=%s AND active', (norm(user),)).fetchone()
        if not row:
            raise ValueError('No such user')
    ttl = LONG_TTL if long else SHORT_TTL
    return seal('session', ttl, o=row['id'], v=row['session_version'], n=secrets.token_hex(8)), (ttl if long else None)


def check(token):
    """The signed-in email, or None. session_version is what ends every session at once (revocation)."""
    data = unseal('session', token)
    if not data:
        return None
    with database.connect() as c:
        row = c.execute('SELECT email FROM users WHERE id=%s AND session_version=%s AND active',
                        (str(data.get('o')), int(data.get('v') or 0))).fetchone()
        return row['email'] if row else None


def csrf_token(session_token):
    return hmac.new(secret().encode(), ('csrf|' + (session_token or 'anon')).encode(), hashlib.sha256).hexdigest()[:32]


def csrf_ok(session_token, submitted):
    # bytes, not str: a non-ASCII header would make compare_digest raise (a 500) instead of refusing
    return bool(submitted) and hmac.compare_digest(csrf_token(session_token).encode(), submitted.encode('utf-8', 'replace'))
