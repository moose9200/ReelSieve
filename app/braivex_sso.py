"""Braivex Accounts sign-in, the only way a customer signs in or up (04 Oct 2026): the checks around the verifier.

Signature (RS256 against the issuer's published JWKS), iss, aud, exp and nbf with 30 seconds of tolerance,
email_verified, email and a Shopify customer GID sub, and the login-CSRF `state`, are all checked by
app/braivex_verify.py, the shared verifier vendored byte for byte from braivex-accounts (see its header).
Contract: braivex-accounts docs/PRODUCT-INTEGRATION.md, including its "Login-CSRF binding" section.

The assertion is a bearer token for 120 seconds and no signature check can see reuse, so the caller must also
spend its `jti` (spend_jti below) before it creates a session.

Nothing here logs an assertion, an email address or a key.
"""
import os
import time

from app import database
from app.braivex_verify import JTI_TTL_SECONDS, BraivexAssertionError, verify_braivex_assertion  # noqa: F401

CLIENT = 'reelsieve'                 # our slug at accounts.braivex.com; it must equal the assertion's `aud`
DEFAULT_ACCOUNTS_URL = 'https://accounts.braivex.com'
CALLBACK_PATH = '/auth/braivex/callback'


def accounts_url():
    """The issuer: the assertion's `iss` must equal it, and its JWKS lives at /.well-known/jwks.json."""
    return ((os.getenv('BRAIVEX_ACCOUNTS_URL') or '').strip() or DEFAULT_ACCOUNTS_URL).rstrip('/')


def callback_url(site):
    """The return_to we send, built from the product's own canonical address and never from a Host header.
    It must equal the URL registered for reelsieve character for character."""
    return site.rstrip('/') + CALLBACK_PATH


def verify(assertion, expected_state):
    """The verified claims, or BraivexAssertionError. expected_state is the state cookie this browser was given and
    is always required: without it an assertion minted for someone else's sign-in would sign them in here.
    Blocking (it may fetch the key set): call it off the event loop."""
    claims = verify_braivex_assertion(assertion, CLIENT, accounts_url(), expected_state=expected_state or '')
    if not isinstance(claims.get('email'), str):
        raise BraivexAssertionError('assertion email is not a string')
    return claims


def spend_jti(jti, now=None):
    """True the first time that assertion is presented, False ever after: the primary key makes the single use
    atomic across web replicas. Expired rows are deleted in app/retention.py."""
    now = now or time.time()
    with database.connect() as c:
        return c.execute('INSERT INTO braivex_sso_jti(jti,seen,expires_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                         (jti, now, now + JTI_TTL_SECONDS)).rowcount == 1
