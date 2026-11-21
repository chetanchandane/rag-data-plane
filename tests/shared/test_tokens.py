"""shared/tokens.py: key ids, public JWKs and minting. A throwaway key, no network."""

import functools
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from shared import tokens

ISSUER = "https://d111111abcdef8.cloudfront.net"
TENANT = uuid.UUID("73b7c720-9a87-44c3-a1c8-c0bd1611c54b")


@functools.cache
def throwaway_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(**overrides: object) -> str:
    args = {"issuer": ISSUER, "tenant_id": TENANT, "role_level": 2, "subject": "test"}
    return tokens.mint(throwaway_key(), **(args | overrides))


def test_key_id_is_the_rfc_7638_thumbprint() -> None:
    # The example key and thumbprint from RFC 7638, section 3.1.
    n = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_B"
        "JECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_F"
        "DW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI"
        "4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    )
    public_key = RSAAlgorithm.from_jwk({"kty": "RSA", "n": n, "e": "AQAB"})
    assert tokens.key_id(public_key) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_the_public_jwk_has_no_private_members() -> None:
    jwk = tokens.public_jwk(throwaway_key().public_key())
    assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}
    assert jwk["kid"] == tokens.key_id(throwaway_key().public_key())


def test_a_minted_token_verifies_with_its_published_jwk() -> None:
    token = mint(scopes=["documents:read", "query"], now=1_700_000_000)

    header = jwt.get_unverified_header(token)
    assert header == {
        "alg": "RS256",
        "kid": tokens.key_id(throwaway_key().public_key()),
        "typ": "JWT",
    }
    published = jwt.PyJWK(tokens.public_jwk(throwaway_key().public_key()))
    claims = jwt.decode(
        token,
        published.key,
        algorithms=["RS256"],
        audience="rag-data-plane",
        issuer=ISSUER,
        options={"verify_exp": False},  # minted at a fixed time
    )
    assert claims["sub"] == "test"
    assert claims["tenant_id"] == str(TENANT)
    assert claims["role_level"] == 2
    assert claims["scope"] == "documents:read query"
    assert claims["iat"] == 1_700_000_000
    assert claims["exp"] - claims["iat"] == 15 * 60
    uuid.UUID(claims["jti"])


def test_no_scopes_means_no_scope_claim() -> None:
    claims = jwt.decode(mint(), options={"verify_signature": False})
    assert "scope" not in claims


def test_each_token_has_its_own_jti() -> None:
    first, second = (jwt.decode(mint(), options={"verify_signature": False}) for _ in range(2))
    assert first["jti"] != second["jti"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"role_level": 0},
        {"role_level": 4},
        {"issuer": ISSUER + "/"},  # `iss` must equal the authorizer's issuer exactly
        {"issuer": "http://d111111abcdef8.cloudfront.net"},
        {"subject": ""},
    ],
)
def test_bad_claims_are_refused_before_signing(overrides: dict) -> None:
    with pytest.raises(ValueError):
        mint(**overrides)
