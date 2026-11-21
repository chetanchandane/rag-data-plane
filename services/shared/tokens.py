"""JWTs from the stand-in token issuer (DECISIONS 2026-11-21, "Token issuer for Slice 2" and
"Step 11").

Pure functions, no AWS and no network: the public JWK for a key, its key id, and signing a
token. scripts/issuer.py gets the private key from SSM; tests use a throwaway key.
"""

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Iterable

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm

ALGORITHM = "RS256"  # API Gateway's JWT authorizer supports only RSA algorithms
AUDIENCE = "rag-data-plane"
TOKEN_LIFETIME_SECONDS = 15 * 60  # Open decision #2: the limit on how long a leaked token works
ROLE_LEVELS = (1, 2, 3)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def key_id(public_key: RSAPublicKey) -> str:
    """The key's RFC 7638 thumbprint: SHA-256 of its required members (e, kty, n) as JSON
    with sorted keys and no whitespace. Derived from the key, so a kid can't name the wrong
    key."""
    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    members = {"e": jwk["e"], "kty": jwk["kty"], "n": jwk["n"]}
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":"))
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def public_jwk(public_key: RSAPublicKey) -> dict[str, str]:
    """The JWKS entry for a key: public members only."""
    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": key_id(public_key),
        "n": jwk["n"],
        "e": jwk["e"],
    }


def mint(
    private_key: RSAPrivateKey,
    *,
    issuer: str,
    tenant_id: uuid.UUID,
    role_level: int,
    subject: str,
    scopes: Iterable[str] = (),
    audience: str = AUDIENCE,
    now: int | None = None,
) -> str:
    """A signed access token. `kid` in the header tells verifiers which JWKS key to use."""
    if role_level not in ROLE_LEVELS:
        raise ValueError(f"role_level must be one of {ROLE_LEVELS}")
    if not issuer.startswith("https://") or issuer.endswith("/"):
        raise ValueError("issuer must be an https URL with no trailing slash")
    if not subject:
        raise ValueError("subject must not be empty")
    issued_at = int(time.time()) if now is None else now
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "tenant_id": str(tenant_id),
        "role_level": role_level,
        "iat": issued_at,
        "exp": issued_at + TOKEN_LIFETIME_SECONDS,
        "jti": str(uuid.uuid4()),  # names this token in logs
    }
    # OAuth's space-separated `scope`: the claim API Gateway checks route scopes against.
    scope = " ".join(scopes)
    if scope:
        claims["scope"] = scope
    return jwt.encode(
        claims,
        private_key,
        algorithm=ALGORITHM,
        headers={"kid": key_id(private_key.public_key())},
    )
