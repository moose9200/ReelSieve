# VENDORED, DO NOT EDIT. Everything below this header is byte-identical to
#   braivex-accounts/packages/verify-py/braivex_verify.py at commit 7463ece
#   (sha256 fe51ee42a4aa59b39f5152d9d77d788a67f69923d7aa9f1a5a8ad00f9464eb09).
# To update: copy that file again under this header and change the commit and digest here and in
# tests/test_braivex_sso.py. Its dependency, PyJWT[crypto], is pinned in requirements.txt (>=2.14 for the
# 30-second refetch cooldown on an unknown kid). The product checks around it are in app/braivex_sso.py.

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
from typing import Any, Dict, Optional

import jwt
from jwt import PyJWKClient

#: Accepts a little clock drift between the product and accounts.braivex.com.
CLOCK_TOLERANCE_SECONDS = 30
#: How long a product must remember a jti to make replay impossible.
JTI_TTL_SECONDS = 600

_CUSTOMER_GID_PREFIX = "gid://shopify/Customer/"

_clients: Dict[str, PyJWKClient] = {}


class BraivexAssertionError(Exception):
    """Raised for any assertion this service will not accept."""


def _jwk_client(issuer: str) -> PyJWKClient:
    client = _clients.get(issuer)
    if client is None:
        # PyJWKClient caches keys and refetches on an unknown kid, which is what
        # makes signing-key rotation invisible to the product.
        client = PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=300)
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
