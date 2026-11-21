"""get_claims(): tokens verified by the API itself, and claims passed on by API Gateway's JWT
authorizer (DECISIONS 2026-11-21, "Step 12"). A throwaway key and an in-memory JWKS; no
network.
"""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from api import token_verifier
from api.auth import Claims, get_claims
from api.token_verifier import TokenVerifier
from shared import tokens
from tests.api.apphelpers import ISSUER, FakeJwks, make_verifier, mint, signing_key

TENANT = uuid.UUID("7d1e0000-0000-4000-8000-000000000001")
KID = tokens.key_id(signing_key().public_key())


def claims_app(verifier: TokenVerifier | None) -> FastAPI:
    """One route that returns get_claims()'s result, so these tests see exactly what an
    endpoint would get."""
    app = FastAPI()
    app.state.verifier = verifier

    @app.get("/claims")
    def claims(c: Annotated[Claims, Depends(get_claims)]) -> dict:
        return {"tenant_id": str(c.tenant_id), "role_level": c.role_level}

    return app


async def get(app, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/claims", headers=headers or {})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims_now(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": tokens.AUDIENCE,
        "sub": "t",
        "tenant_id": str(TENANT),
        "role_level": 2,
        "iat": now,
        "exp": now + 900,
    }
    return {k: v for k, v in (claims | overrides).items() if v is not None}


def sign(claims: dict[str, Any], key: rsa.RSAPrivateKey | None = None) -> str:
    """An RS256 token with any claims at all (tokens.mint refuses bad ones)."""
    key = key or signing_key()
    kid = tokens.key_id(key.public_key())
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


# --- tokens the API verifies itself ---------------------------------------------------------


async def test_a_minted_token_gives_its_tenant_and_role_level():
    resp = await get(claims_app(make_verifier()), bearer(mint(TENANT, 3)))
    assert resp.status_code == 200
    assert resp.json() == {"tenant_id": str(TENANT), "role_level": 3}


def _alg_none() -> str:
    header = _b64url(json.dumps({"alg": "none", "kid": KID, "typ": "JWT"}).encode())
    return f"{header}.{_b64url(json.dumps(claims_now(role_level=3)).encode())}."


def _hs256_with_the_public_key() -> str:
    # The classic confusion attack: HMAC "signed" with the public key, which anyone has.
    public_pem = (
        signing_key()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    header = _b64url(json.dumps({"alg": "HS256", "kid": KID, "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims_now(role_level=3)).encode())
    signature = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def _another_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _tampered() -> str:
    header, _, signature = mint(TENANT, 1).split(".")
    payload = _b64url(json.dumps(claims_now(role_level=3)).encode())
    return f"{header}.{payload}.{signature}"


REJECTED = {
    "expired": lambda: mint(TENANT, 2, now=int(time.time()) - 3600),
    "not_yet_valid_nbf": lambda: sign(claims_now(nbf=int(time.time()) + 3600)),
    "issued_in_the_future": lambda: mint(TENANT, 2, now=int(time.time()) + 3600),
    "wrong_issuer": lambda: mint(TENANT, 2, issuer="https://evil.example"),
    "issuer_with_trailing_slash": lambda: sign(claims_now(iss=ISSUER + "/")),
    "wrong_audience": lambda: mint(TENANT, 2, audience="someone-else"),
    "unknown_kid": lambda: sign(claims_now(), key=_another_key()),
    "alg_none": _alg_none,
    "hs256": _hs256_with_the_public_key,
    "tampered_payload": _tampered,
    "not_a_jwt": lambda: "not.a.jwt",
    "no_exp": lambda: sign(claims_now(exp=None)),
    "no_iat": lambda: sign(claims_now(iat=None)),
    "no_sub": lambda: sign(claims_now(sub=None)),
    "no_tenant_id": lambda: sign(claims_now(tenant_id=None)),
    "tenant_id_not_a_uuid": lambda: sign(claims_now(tenant_id="acme")),
    "tenant_id_a_number": lambda: sign(claims_now(tenant_id=42)),
    "no_role_level": lambda: sign(claims_now(role_level=None)),
    "role_level_0": lambda: sign(claims_now(role_level=0)),
    "role_level_4": lambda: sign(claims_now(role_level=4)),
    "role_level_true": lambda: sign(claims_now(role_level=True)),
    "role_level_float": lambda: sign(claims_now(role_level=2.0)),
}


@pytest.mark.parametrize("make_token", REJECTED.values(), ids=REJECTED.keys())
async def test_bad_tokens_get_401(make_token):
    resp = await get(claims_app(make_verifier()), bearer(make_token()))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"detail": "invalid or missing bearer token"}  # never says why


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dGVzdDp4"}, {"Authorization": "Bearer"}],
    ids=["missing", "basic_scheme", "empty_bearer"],
)
async def test_no_bearer_token_gets_401(headers):
    resp = await get(claims_app(make_verifier()), headers)
    assert resp.status_code == 401


async def test_without_a_verifier_or_api_gateway_every_request_gets_401():
    resp = await get(claims_app(verifier=None), bearer(mint(TENANT, 2)))
    assert resp.status_code == 401


# --- the JWKS cache -------------------------------------------------------------------------


async def test_the_jwks_is_fetched_once_and_cached():
    jwks = FakeJwks()
    app = claims_app(make_verifier(jwks))
    for _ in range(3):
        assert (await get(app, bearer(mint(TENANT, 1)))).status_code == 200
    assert jwks.fetches == 1


async def test_a_key_published_after_the_last_fetch_is_fetched_again_for_its_kid():
    # cooldown_duration=0 here; the next test covers the default cooldown.
    jwks = FakeJwks(cooldown_duration=0)
    app = claims_app(make_verifier(jwks))
    assert (await get(app, bearer(mint(TENANT, 1)))).status_code == 200

    new_key = _another_key()
    jwks.published["keys"].append(tokens.public_jwk(new_key.public_key()))
    resp = await get(app, bearer(sign(claims_now(), key=new_key)))

    assert resp.status_code == 200
    assert jwks.fetches == 2


async def test_junk_kids_dont_cause_a_fetch_per_request():
    jwks = FakeJwks()  # PyJWKClient's default 30-second cooldown
    app = claims_app(make_verifier(jwks))
    for _ in range(5):
        assert (await get(app, bearer(sign(claims_now(), key=_another_key())))).status_code == 401
    assert jwks.fetches == 1


# --- discovery at startup -------------------------------------------------------------------


def _discovery(monkeypatch, document: dict) -> None:
    monkeypatch.setattr(token_verifier, "_fetch_json", lambda url: document)


def test_the_verifier_finds_the_jwks_through_the_discovery_document(monkeypatch):
    _discovery(monkeypatch, {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
    verifier = TokenVerifier.from_issuer(ISSUER)
    assert verifier._jwks.uri == f"{ISSUER}/.well-known/jwks.json"


@pytest.mark.parametrize(
    "document",
    [
        {"issuer": "https://evil.example", "jwks_uri": f"{ISSUER}/.well-known/jwks.json"},
        {"issuer": ISSUER, "jwks_uri": "http://d111111abcdef8.cloudfront.net/jwks.json"},
        {"issuer": ISSUER},
    ],
    ids=["other_issuer", "plain_http_jwks", "no_jwks_uri"],
)
def test_a_bad_discovery_document_fails_at_startup(monkeypatch, document):
    _discovery(monkeypatch, document)
    with pytest.raises(RuntimeError):
        TokenVerifier.from_issuer(ISSUER)


# --- claims passed on by API Gateway's JWT authorizer ---------------------------------------


def behind_api_gateway(app: FastAPI, authorizer: dict | None) -> Any:
    """The app as Mangum calls it on Lambda: the event is in the ASGI scope as "aws.event".
    authorizer is the event's requestContext.authorizer (payload format 2.0)."""
    event = {"version": "2.0", "requestContext": {"http": {"method": "GET"}}}
    if authorizer is not None:
        event["requestContext"]["authorizer"] = authorizer

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = scope | {"aws.event": event}
        await app(scope, receive, send)

    return asgi


def authorizer_claims(**claims: Any) -> dict:
    # Every value is a string, as API Gateway may pass them (DECISIONS: step 13 confirms).
    base = {"iss": ISSUER, "aud": tokens.AUDIENCE, "sub": "t", "exp": "1795286894"}
    return {"jwt": {"claims": base | claims, "scopes": None}}


@pytest.mark.parametrize("role_level", ["2", 2])
async def test_api_gateway_claims_are_used_without_verifying_again(role_level):
    # No verifier and no bearer token: API Gateway has already verified it.
    app = behind_api_gateway(
        claims_app(verifier=None), authorizer_claims(tenant_id=str(TENANT), role_level=role_level)
    )
    resp = await get(app)
    assert resp.status_code == 200
    assert resp.json() == {"tenant_id": str(TENANT), "role_level": 2}


@pytest.mark.parametrize(
    "authorizer",
    [
        None,
        {},
        {"jwt": {"claims": None}},
        {"lambda": {"tenant_id": str(TENANT), "role_level": "3"}},  # not the JWT authorizer
    ],
    ids=["no_authorizer", "empty", "no_claims", "other_authorizer_type"],
)
async def test_a_lambda_event_without_jwt_claims_gets_401(authorizer):
    # A valid bearer token and a verifier don't rescue it: on Lambda only API Gateway's
    # claims count.
    app = behind_api_gateway(claims_app(make_verifier()), authorizer)
    resp = await get(app, bearer(mint(TENANT, 2)))
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"role_level": "2"},
        {"tenant_id": "acme", "role_level": "2"},
        {"tenant_id": str(TENANT)},
        {"tenant_id": str(TENANT), "role_level": "0"},
        {"tenant_id": str(TENANT), "role_level": "4"},
        {"tenant_id": str(TENANT), "role_level": "02"},
        {"tenant_id": str(TENANT), "role_level": "2.0"},
        {"tenant_id": str(TENANT), "role_level": "true"},
    ],
    ids=[
        "no_tenant",
        "tenant_not_uuid",
        "no_role",
        "role_0",
        "role_4",
        "role_02",
        "role_2.0",
        "role_true",
    ],
)
async def test_bad_api_gateway_claims_get_401(claims):
    app = behind_api_gateway(claims_app(verifier=None), authorizer_claims(**claims))
    assert (await get(app)).status_code == 401


# --- the Lambda images don't need PyJWT -----------------------------------------------------


def test_the_api_and_the_worker_import_without_pyjwt():
    # PyJWT is a dev dependency, so it isn't in the Lambda images. Importing what runs on
    # Lambda must not import it. A fresh interpreter, so other tests' imports don't count.
    services = Path(__file__).resolve().parents[2] / "services"
    code = (
        "import sys, api.handler, api.auth, api.documents, worker.handler; "
        "loaded = sorted(m for m in ('jwt', 'cryptography') if m in sys.modules); "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ | {"PYTHONPATH": str(services)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""
