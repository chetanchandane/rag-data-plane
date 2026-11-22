"""Build the API app for tests without its lifespan: the test supplies pool, S3, settings and
the token verifier.

The S3 client has fake static credentials, and the verifier's JWKS is a throwaway key's,
served from memory. Presigning and verifying are local computation, so no test here ever
calls AWS or the network.
"""

import functools
import hashlib
import time
import uuid
from typing import Any

import boto3
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from api.app import create_app
from api.config import Settings
from api.token_verifier import TokenVerifier
from api.uploads import make_s3_client
from shared import tokens

ISSUER = "https://d111111abcdef8.cloudfront.net"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
BUCKET = "ragdp-raw-test"
REGION = "us-east-2"
MODEL_ID = "amazon.titan-embed-text-v2:0"


def fake_s3():
    session = boto3.session.Session(
        aws_access_key_id="AKIATESTTESTTESTTEST",
        aws_secret_access_key="not-a-real-secret",
        region_name=REGION,
    )
    return make_s3_client(REGION, session=session)


# --- tokens ------------------------------------------------------------------------------


@functools.cache
def signing_key() -> rsa.RSAPrivateKey:
    """A throwaway key, made once per test run: RSA key generation takes a moment."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(tenant_id: uuid.UUID, role_level: int, **overrides: Any) -> str:
    """A token from the test issuer, as scripts/issuer.py mint makes one."""
    args = {"issuer": ISSUER, "tenant_id": tenant_id, "role_level": role_level, "subject": "t"}
    return tokens.mint(signing_key(), **(args | overrides))


class FakeJwks(jwt.PyJWKClient):
    """PyJWKClient with the JWKS served from memory (its docs allow overriding fetch_data).
    Caching and the refetch for an unknown kid are PyJWKClient's own, unchanged."""

    def __init__(self, keys: list[dict[str, str]] | None = None, **kwargs: Any) -> None:
        super().__init__(JWKS_URI, **kwargs)
        self.published = {"keys": keys or [tokens.public_jwk(signing_key().public_key())]}
        self.fetches = 0

    def fetch_data(self) -> Any:
        self.fetches += 1
        # The real fetch_data starts the unknown-kid cooldown on every successful fetch.
        self._last_successful_fetch = time.monotonic()
        return self.published


def make_verifier(jwks: FakeJwks | None = None) -> TokenVerifier:
    return TokenVerifier(ISSUER, jwks or FakeJwks())


# --- app and client -----------------------------------------------------------------------


def build_app(pool, tenant_id: uuid.UUID, role_level: int) -> FastAPI:
    """The app, plus a token for tenant_id and role_level that client() sends by default."""
    app = create_app()
    app.state.settings = Settings(
        database_url="unused-in-tests",
        raw_bucket=BUCKET,
        aws_region=REGION,
        embedding_model_id=MODEL_ID,
        jwt_issuer=ISSUER,
    )
    app.state.pool = pool
    app.state.s3 = fake_s3()
    app.state.verifier = make_verifier()
    app.state.test_token = mint(tenant_id, role_level)
    return app


_APP_TOKEN = object()  # client()'s default: the token build_app minted


def client(app: FastAPI, token: str | None | object = _APP_TOKEN) -> httpx.AsyncClient:
    """token=None sends no Authorization header."""
    if token is _APP_TOKEN:
        token = app.state.test_token
    # ASGITransport calls the app in-process; it does not run the lifespan.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    )


def registration(
    content: bytes = b"%PDF-1.7 test", *, external_id: str = "doc-1", min_role_level: int = 1
) -> dict:
    return {
        "external_id": external_id,
        "filename": "handbook.pdf",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "min_role_level": min_role_level,
    }
