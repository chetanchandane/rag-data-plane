"""Verifying access tokens in the API itself, for when no API Gateway is in front of it.

On Lambda, API Gateway's JWT authorizer has already verified the token, and the API reads
the claims it passes on (api/auth.py, DECISIONS 2026-11-21 "Step 12"). Locally (`api.local`)
and in tests nothing has, so this module makes the same checks with PyJWT.

Only api/app.py imports it, and only when JWT_ISSUER is set: PyJWT is a dev dependency and is
not in the Lambda image.
"""

import json
import urllib.request
from typing import Any

import jwt

from api.auth import TokenRejected
from shared import tokens

DISCOVERY_PATH = "/.well-known/openid-configuration"
FETCH_TIMEOUT_SECONDS = 10


class TokenVerifier:
    def __init__(self, issuer: str, jwks: jwt.PyJWKClient, audience: str = tokens.AUDIENCE) -> None:
        self._issuer = issuer
        self._audience = audience
        # PyJWKClient caches the JWKS for 5 minutes. For a kid it hasn't seen, it fetches
        # the JWKS again, at most once every 30 seconds, so a key published after the last
        # fetch is found and junk kids can't make it fetch on every request.
        self._jwks = jwks

    @classmethod
    def from_issuer(cls, issuer: str, audience: str = tokens.AUDIENCE) -> "TokenVerifier":
        """Find the JWKS the way API Gateway does: the issuer's discovery document names it.
        Called once at startup, so a wrong issuer URL fails there, not on the first request."""
        config = _fetch_json(issuer + DISCOVERY_PATH)
        if config.get("issuer") != issuer:
            raise RuntimeError(f"{issuer}{DISCOVERY_PATH} names issuer {config.get('issuer')!r}")
        jwks_uri = config.get("jwks_uri", "")
        if not jwks_uri.startswith("https://"):
            raise RuntimeError(f"the discovery document's jwks_uri isn't https: {jwks_uri!r}")
        return cls(issuer, jwt.PyJWKClient(jwks_uri, timeout=FETCH_TIMEOUT_SECONDS), audience)

    def verify(self, token: str) -> dict[str, Any]:
        """The token's claims, if its signature, iss, aud, exp, nbf and iat all check out
        (the same checks as API Gateway's authorizer). Raises TokenRejected otherwise."""
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                # Only RS256. This refuses `alg: none`, and HS256 signed with the public key
                # as the secret, whatever the token's header says.
                algorithms=[tokens.ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as error:
            raise TokenRejected(f"{type(error).__name__}: {error}") from error


def _fetch_json(url: str) -> Any:
    if not url.startswith("https://"):
        raise RuntimeError(f"refusing to fetch over plain HTTP: {url}")
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return json.load(response)
