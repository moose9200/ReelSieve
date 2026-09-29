"""PostgreSQL identities, versioned signed sessions and shared login limits."""
import base64
import hashlib
import hmac
import ipaddress
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


def has_account():
    with database.connect() as c:
        return bool(c.execute('SELECT 1 FROM users WHERE active LIMIT 1').fetchone())


def users():
    with database.connect() as c:
        return c.execute('SELECT email AS "user", role, created FROM users WHERE active ORDER BY email').fetchall()


def validate_password(pw):
    if len(pw) < 8:
        raise ValueError('Password must be at least 8 characters')
    if pw.lower() in ('password', '12345678', 'qwertyui'):
        raise ValueError('Choose a less common password')


def create_user(user, pw, role='member'):
    user = norm(user)
    if not EMAIL.match(user):
        raise ValueError('Enter a valid email address')
    validate_password(pw)
    if role not in ('member', 'admin'):
        raise ValueError('Unknown role')
    salt = secrets.token_hex(16)
    try:
        with database.connect() as c:
            c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                      (str(uuid.uuid4()), user, salt, _hash(pw, salt), ITERATIONS, role, time.time()))
    except UniqueViolation:
        raise ValueError('That email already has an account') from None


# A hash no PBKDF2 output can equal (those are 64 hex characters), so verify() can never match one: an account that
# signs in with Braivex has no usable password until it asks for a reset link.
SSO_ONLY_HASH = 'braivex-sso:'


def create_sso_user(user, braivex_customer_id):
    """A customer Braivex Accounts verified who has never had a ReelSieve account. Always a member: signing in with
    Braivex never grants operator rights, whatever the address."""
    user = norm(user)
    if not EMAIL.match(user):
        raise ValueError('Enter a valid email address')
    try:
        with database.connect() as c:
            c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created,braivex_customer_id) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                      (str(uuid.uuid4()), user, secrets.token_hex(16), SSO_ONLY_HASH + secrets.token_hex(32),
                       ITERATIONS, 'member', time.time(), braivex_customer_id))
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
    """Attach a Shopify customer id to an account that has none. False when the row already carries another one, or
    another account claimed this id first: neither may be overwritten, so nobody can take over an account."""
    try:
        with database.connect() as c:
            return bool(c.execute('UPDATE users SET braivex_customer_id=%s WHERE email=%s AND active '
                                  'AND braivex_customer_id IS NULL RETURNING id',
                                  (braivex_customer_id, norm(user))).fetchone())
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
    validate_password(pw)
    salt = secrets.token_hex(16)
    with database.connect() as c:
        row = c.execute('UPDATE users SET salt=%s,hash=%s,iterations=%s,changed=%s,session_version=session_version+1 WHERE email=%s AND active RETURNING id',
                        (salt, _hash(pw, salt), ITERATIONS, time.time(), norm(user))).fetchone()
        if not row:
            raise ValueError('No such user')
        c.execute('DELETE FROM password_resets WHERE owner_id=%s', (row['id'],))  # a new password voids any reset link


RESET_TTL = 60 * 60  # a reset link lives one hour


def _reset_hash(token):
    """The link carries the random bytes; the database keeps only this hash of them."""
    return hashlib.sha256((token or '').encode()).hexdigest()


def start_reset(user):
    """A single-use reset token for an account that can sign in, or None. Asking again voids the earlier links."""
    with database.connect() as c:
        row = c.execute('SELECT id FROM users WHERE email=%s AND active', (norm(user),)).fetchone()
        if not row:
            return None
        c.execute('DELETE FROM password_resets WHERE owner_id=%s', (row['id'],))
        token, now = secrets.token_urlsafe(32), time.time()
        c.execute('INSERT INTO password_resets(token_hash,owner_id,created,expires_at) VALUES(%s,%s,%s,%s)',
                  (_reset_hash(token), row['id'], now, now + RESET_TTL))
        return token


def reset_token_live(token):
    """True while that exact link is unused and unexpired. Nothing else about the account is revealed."""
    with database.connect() as c:
        return bool(c.execute('SELECT 1 FROM password_resets WHERE token_hash=%s AND expires_at>%s',
                              (_reset_hash(token), time.time())).fetchone())


def finish_reset(token, pw):
    """Spend the link and set the password. Every session of that account ends (session_version), and every other
    reset link it has is deleted. Raises ValueError for a weak password, or a used, expired or unknown link."""
    validate_password(pw)
    with database.connect() as c:
        row = c.execute('DELETE FROM password_resets WHERE token_hash=%s AND expires_at>%s RETURNING owner_id',
                        (_reset_hash(token), time.time())).fetchone()
        if not row:
            raise ValueError('That reset link has expired or has already been used. Ask for a new one.')
        salt = secrets.token_hex(16)
        done = c.execute('UPDATE users SET salt=%s,hash=%s,iterations=%s,changed=%s,session_version=session_version+1 '
                         'WHERE id=%s AND active RETURNING email', (salt, _hash(pw, salt), ITERATIONS, time.time(), row['owner_id'])).fetchone()
        c.execute('DELETE FROM password_resets WHERE owner_id=%s', (row['owner_id'],))
        if not done:
            raise ValueError('That account can no longer be reset here. Email hello@braivex.com.')
        return done['email']


def role(user):
    with database.connect() as c:
        row = c.execute('SELECT role FROM users WHERE email=%s AND active', (norm(user),)).fetchone()
        return row['role'] if row else 'member'


def verify(user, pw):
    with database.connect() as c:
        row = c.execute('SELECT id,salt,hash,iterations FROM users WHERE email=%s AND active', (norm(user),)).fetchone()
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


def issue(user, long=True):
    with database.connect() as c:
        row = c.execute('SELECT id,session_version FROM users WHERE email=%s AND active', (norm(user),)).fetchone()
        if not row:
            raise ValueError('No such user')
    ttl = LONG_TTL if long else SHORT_TTL
    payload = f"{row['id']}|{int(time.time()) + ttl}|{row['session_version']}|{secrets.token_hex(8)}"
    sig = hmac.new(secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{payload}|{sig}'.encode()).decode(), (ttl if long else None)


def check(token):
    key = secret().encode()
    try:
        owner, exp, version, nonce, sig = base64.urlsafe_b64decode(token.encode()).decode().split('|')
        if int(exp) <= time.time():
            return None
        payload = f'{owner}|{exp}|{version}|{nonce}'
        if not hmac.compare_digest(hmac.new(key, payload.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        version = int(version)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return None
    with database.connect() as c:
        row = c.execute('SELECT email FROM users WHERE id=%s AND session_version=%s AND active', (owner, version)).fetchone()
        return row['email'] if row else None


def csrf_token(session_token):
    return hmac.new(secret().encode(), ('csrf|' + (session_token or 'anon')).encode(), hashlib.sha256).hexdigest()[:32]


def csrf_ok(session_token, submitted):
    return bool(submitted) and hmac.compare_digest(csrf_token(session_token), submitted)
