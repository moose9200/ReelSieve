"""Braivex Accounts sign-in: the assertion verifier and the switches around it.

The claim checks mirror, one for one, the reference verifier
/Users/hemant/braivex-accounts/packages/verify-ts/index.ts (jose), read 29 Sep 2026, and the contract in that
repo's docs/PRODUCT-INTEGRATION.md including its "Login-CSRF binding" section. The reference Python helper wants
PyJWT[crypto]; this file uses `cryptography`, which ReelSieve already depends on for Fernet, so the app gains no
dependency. Verified here, exactly as jose does it:

  RS256 over the JWS signing input, against the key with that `kid` in the issuer's published JWKS ·
  iss · aud · exp and nbf with 30 seconds of clock tolerance · the required claims are present ·
  email_verified is true · email is a non-empty string · sub is a Shopify customer GID ·
  the `state` claim equals the state cookie this browser was given (constant time).

The assertion is a bearer token for 120 seconds and no signature check can see reuse, so the caller must also
spend its `jti` (spend_jti below) before it creates a session.

Nothing here logs an assertion, an email address or a key.
"""
import base64
import hmac
import json
import os
import threading
import time
from datetime import date, datetime, timezone

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import database

CLIENT = 'reelsieve'                 # our slug at accounts.braivex.com; it must equal the assertion's `aud`
DEFAULT_ACCOUNTS_URL = 'https://accounts.braivex.com'
CALLBACK_PATH = '/auth/braivex/callback'
CLOCK_TOLERANCE = 30                 # seconds; the contract says 30 and no more
JTI_TTL = 600                        # how long a spent assertion is remembered
JWKS_TTL = 600                       # how long a fetched key set is trusted without asking again
JWKS_REFETCH_COOLDOWN = 30           # an unknown kid refetches, but never once per forged token
JWKS_TIMEOUT = 10
REQUIRED_CLAIMS = ('sub', 'jti', 'iat', 'nbf', 'exp', 'email', 'email_verified')
SHOPIFY_CUSTOMER = 'gid://shopify/Customer/'

_JWKS = {}  # issuer -> {'at': fetched_at, 'keys': {kid: RSAPublicKey}}
_JWKS_LOCK = threading.Lock()


class BraivexAssertionError(Exception):
    """Anything at all wrong with an assertion. The reason never reaches the browser."""


# ---------------- switches ----------------

def enabled():
    return (os.getenv('BRAIVEX_SSO') or '').strip().lower() == 'on'


def accounts_url():
    """The issuer: the assertion's `iss` must equal it, and its JWKS lives at /.well-known/jwks.json."""
    return ((os.getenv('BRAIVEX_ACCOUNTS_URL') or '').strip() or DEFAULT_ACCOUNTS_URL).rstrip('/')


def callback_url(site):
    """The return_to we send, built from the product's own canonical address and never from a Host header.
    It must equal the URL registered for reelsieve character for character."""
    return site.rstrip('/') + CALLBACK_PATH


def sunset():
    """The date customer password sign-in ends, or None while BRAIVEX_PASSWORD_SUNSET is unset or unreadable."""
    try:
        return date.fromisoformat((os.getenv('BRAIVEX_PASSWORD_SUNSET') or '').strip())
    except ValueError:
        return None


def passwords_allowed(today=None):
    """False only once Braivex sign-in is on and the sunset date has arrived (UTC). Operators are exempt: that is
    the caller's check, so a locked-out platform has a way back in."""
    if not enabled():
        return True
    day = sunset()
    return not (day and (today or datetime.now(timezone.utc).date()) >= day)


def view():
    """What the templates need: the switch, whether a password form is still shown, and the date to name."""
    day = sunset()
    return {'on': enabled(), 'passwords': passwords_allowed(), 'sunset': day.strftime('%d %b %Y') if day else ''}


# ---------------- the published keys ----------------

def _b64(value):
    """base64url without padding, as every JOSE field is encoded."""
    if not isinstance(value, str):
        raise BraivexAssertionError('assertion is not a JWS')
    try:
        return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (ValueError, TypeError):
        raise BraivexAssertionError('assertion is not base64url') from None


def _fetch_keys(issuer):
    try:
        with httpx.Client(timeout=JWKS_TIMEOUT) as h:
            r = h.get(issuer + '/.well-known/jwks.json')
        r.raise_for_status()
        published = r.json()['keys']
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise BraivexAssertionError('the signing keys could not be read') from None
    keys = {}
    for jwk in published:
        if not isinstance(jwk, dict) or jwk.get('kty') != 'RSA' or not jwk.get('kid'):
            continue
        if jwk.get('alg') not in (None, 'RS256') or jwk.get('use') not in (None, 'sig'):
            continue
        try:
            numbers = rsa.RSAPublicNumbers(int.from_bytes(_b64(jwk['e']), 'big'), int.from_bytes(_b64(jwk['n']), 'big'))
            keys[jwk['kid']] = numbers.public_key()
        except (BraivexAssertionError, ValueError):
            continue
    return keys


def _signing_key(issuer, kid):
    """The published key with that kid. The key set is cached for JWKS_TTL and refetched when a kid is unknown,
    so rotating a signing key is invisible here; the cooldown stops a forged kid becoming an outbound request."""
    if not kid:
        raise BraivexAssertionError('assertion header has no kid')
    with _JWKS_LOCK:
        entry, now = _JWKS.get(issuer), time.time()
        if entry and kid in entry['keys'] and now - entry['at'] < JWKS_TTL:
            return entry['keys'][kid]
        if entry and kid not in entry['keys'] and now - entry['at'] < JWKS_REFETCH_COOLDOWN:
            raise BraivexAssertionError('assertion is signed by an unpublished key')
        keys = _fetch_keys(issuer)
        _JWKS[issuer] = {'at': time.time(), 'keys': keys}
        if kid not in keys:
            raise BraivexAssertionError('assertion is signed by an unpublished key')
        return keys[kid]


# ---------------- the assertion ----------------

def _number(claims, name):
    value = claims.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BraivexAssertionError(f'assertion {name} is not a number')
    return value


def verify(assertion, expected_state, product=CLIENT, issuer=None):
    """The verified claims, or BraivexAssertionError. expected_state is the state cookie this browser was given:
    without it an assertion minted for someone else's sign-in would sign them into this browser (login CSRF)."""
    issuer = (issuer or accounts_url()).rstrip('/')
    parts = (assertion or '').split('.')
    if len(parts) != 3:
        raise BraivexAssertionError('assertion is not a JWS')
    head_b64, body_b64, sig_b64 = parts
    try:
        header = json.loads(_b64(head_b64))
        claims = json.loads(_b64(body_b64))
    except (ValueError, UnicodeError):
        raise BraivexAssertionError('assertion is not JSON') from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise BraivexAssertionError('assertion is not an object')
    if header.get('alg') != 'RS256':
        raise BraivexAssertionError('assertion is not RS256')
    key = _signing_key(issuer, header.get('kid'))
    try:
        key.verify(_b64(sig_b64), f'{head_b64}.{body_b64}'.encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise BraivexAssertionError('assertion signature does not verify') from None

    missing = [name for name in REQUIRED_CLAIMS if claims.get(name) is None]
    if missing:
        raise BraivexAssertionError('assertion is missing ' + ', '.join(missing))
    if claims.get('iss') != issuer:
        raise BraivexAssertionError('assertion was issued by someone else')
    aud = claims.get('aud')
    if aud != product and not (isinstance(aud, list) and product in aud):
        raise BraivexAssertionError('assertion was minted for another product')
    now = time.time()
    if _number(claims, 'exp') <= now - CLOCK_TOLERANCE:
        raise BraivexAssertionError('assertion has expired')
    if _number(claims, 'nbf') > now + CLOCK_TOLERANCE:
        raise BraivexAssertionError('assertion is not valid yet')
    _number(claims, 'iat')
    if claims.get('email_verified') is not True:
        raise BraivexAssertionError('assertion is missing email_verified=true')
    if not isinstance(claims.get('email'), str) or not claims['email']:
        raise BraivexAssertionError('assertion is missing email')
    if not isinstance(claims.get('sub'), str) or not claims['sub'].startswith(SHOPIFY_CUSTOMER):
        raise BraivexAssertionError('assertion sub is not a Shopify customer GID')
    if not isinstance(claims.get('jti'), str) or not claims['jti']:
        raise BraivexAssertionError('assertion is missing jti')
    state = claims.get('state') if isinstance(claims.get('state'), str) else ''
    if not expected_state or not hmac.compare_digest(state, expected_state):
        raise BraivexAssertionError('assertion state does not match this browser')
    return claims


def spend_jti(jti, now=None):
    """True the first time that assertion is presented, False ever after: the primary key makes the single use
    atomic across web replicas. Expired rows are deleted in app/retention.py."""
    now = now or time.time()
    with database.connect() as c:
        return c.execute('INSERT INTO braivex_sso_jti(jti,seen,expires_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                         (jti, now, now + JTI_TTL)).rowcount == 1
