# VENDORED, DO NOT EDIT. Everything below this header is byte-identical to
#   braivex-accounts/packages/verify-py/braivex_verify.py at commit cf9abb7
#   (sha256 18d694351eaa62629df290aee0dc00e830a66916333aa192804147792a8b4794).
# To update: copy that file again under this header and change the commit and digest here and in
# tests/test_braivex_sso.py. Its dependency, PyJWT[crypto] >= 2.8, is pinned in requirements.txt.
# The product checks around it are in app/braivex_sso.py.

"""Braivex Accounts assertion verifier (Python).

Dependency: ``PyJWT[crypto] >= 2.8`` (PyJWT alone cannot do RS256 - the ``crypto``
extra pulls in ``cryptography``)::

    pip install "PyJWT[crypto]>=2.8"

Usage::

    from braivex_verify import verify_braivex_assertion
    claims = verify_braivex_assertion(request.form["assertion"], "housieve")

The caller MUST remember ``claims["jti"]`` for 10 minutes and reject a repeat: the
assertion is a bearer token for 120 seconds and nothing here can detect reuse.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import jwt
from jwt import PyJWKClient

#: Accepts a little clock drift between the product and accounts.braivex.com.
CLOCK_TOLERANCE_SECONDS = 30
#: How long a product must remember a jti to make replay impossible.
JTI_TTL_SECONDS = 600
#: How long a fetched JWKS is trusted. A key the broker withdraws stops verifying within this.
JWKS_LIFESPAN_SECONDS = 300
#: Minimum gap between JWKS fetches triggered by an unknown kid (an attacker can mint kids).
JWKS_REFETCH_COOLDOWN_SECONDS = 30

_CUSTOMER_GID_PREFIX = "gid://shopify/Customer/"

_clients: Dict[str, PyJWKClient] = {}


class BraivexAssertionError(Exception):
    """Raised for any assertion this service will not accept."""


class _JWKClient(PyJWKClient):
    """PyJWKClient with a refetch cooldown that works on every PyJWT >= 2.8.

    PyJWT only gained ``cooldown_duration`` in 2.14; before that every unknown kid refetched.
    """

    _fetched_at = float("-inf")

    def fetch_data(self) -> Any:
        data = super().fetch_data()
        self._fetched_at = time.monotonic()
        return data

    def get_signing_key(self, kid: str):  # type: ignore[override]
        key = self.match_kid(self.get_signing_keys(), kid)
        if key is None and time.monotonic() - self._fetched_at >= JWKS_REFETCH_COOLDOWN_SECONDS:
            key = self.match_kid(self.get_signing_keys(refresh=True), kid)  # rotation: a new kid
        if key is None:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return key


def _jwk_client(issuer: str) -> PyJWKClient:
    client = _clients.get(issuer)
    if client is None:
        # cache_keys stays off: PyJWT's per-kid cache is an lru_cache with no expiry, so a key the
        # broker withdrew would verify until restart. The JWK-set cache below does expire.
        # Own User-Agent: Cloudflare in front of accounts.braivex.com answers urllib's
        # default "Python-urllib/3.x" with 403, which failed every sign-in (05 Oct 2026).
        client = _JWKClient(
            f"{issuer}/.well-known/jwks.json",
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=JWKS_LIFESPAN_SECONDS,
            headers={"User-Agent": "braivex-verify-py/1"},
        )
        _clients[issuer] = client
    return client


def verify_braivex_assertion(
    assertion: str,
    product: str,
    public_url: Optional[str] = None,
    expected_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify signature (RS256, published JWKS), iss, aud, exp and nbf.

    :param product: the product's own slug - it must equal the ``aud`` claim.
    :param public_url: defaults to ``$BRAIVEX_PUBLIC_URL``.
    :raises BraivexAssertionError: on anything it does not like.
    """
    issuer = (public_url or os.environ.get("BRAIVEX_PUBLIC_URL") or "").rstrip("/")
    if not issuer:
        raise BraivexAssertionError("BRAIVEX_PUBLIC_URL is not set")

    try:
        signing_key = _jwk_client(issuer).get_signing_key_from_jwt(assertion)
        claims: Dict[str, Any] = jwt.decode(
            assertion,
            signing_key.key,
            algorithms=["RS256"],
            audience=product,
            issuer=issuer,
            leeway=CLOCK_TOLERANCE_SECONDS,
            options={"require": ["sub", "jti", "iat", "nbf", "exp", "aud", "iss"]},
        )
    except Exception as exc:  # PyJWKClientError, InvalidTokenError, ...
        raise BraivexAssertionError(f"assertion rejected: {exc}") from exc

    if claims.get("email_verified") is not True:
        raise BraivexAssertionError("assertion is missing email_verified=true")
    if not claims.get("email"):
        raise BraivexAssertionError("assertion is missing email")
    sub = claims.get("sub", "")
    if not isinstance(sub, str) or not sub.startswith(_CUSTOMER_GID_PREFIX):
        raise BraivexAssertionError("assertion sub is not a Shopify customer GID")

    # Always pass expected_state (the cookie set before redirecting to /start): it stops login CSRF.
    if expected_state is not None:
        import hmac
        got = claims.get("state")
        if not isinstance(got, str) or not expected_state or not hmac.compare_digest(got, expected_state):
            raise BraivexAssertionError("assertion state does not match this browser")
    return claims
