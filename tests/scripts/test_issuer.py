"""scripts/issuer.py: key creation, activation, retirement, minting and the online check,
against a fake SSM and a JWKS file in a temporary directory. No AWS, no network."""

import importlib.util
import json
import uuid
from pathlib import Path

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa

from shared import tokens

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "issuer.py"
_spec = importlib.util.spec_from_file_location("issuer", _PATH)
issuer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issuer)

ISSUER = "https://d111111abcdef8.cloudfront.net"
TENANT = uuid.UUID("73b7c720-9a87-44c3-a1c8-c0bd1611c54b")


class FakeSsm:
    """Parameter Store in a dict, raising the errors boto3 raises."""

    def __init__(self) -> None:
        self.params: dict[str, dict] = {}

    @staticmethod
    def _not_found(operation: str) -> ClientError:
        return ClientError({"Error": {"Code": "ParameterNotFound"}}, operation)

    def put_parameter(self, *, Name: str, Value: str, Type: str, Overwrite: bool, **_: object):
        if Name in self.params and not Overwrite:
            raise ClientError({"Error": {"Code": "ParameterAlreadyExists"}}, "PutParameter")
        self.params[Name] = {"Value": Value, "Type": Type}

    def get_parameter(self, *, Name: str, WithDecryption: bool = False) -> dict:
        if Name not in self.params:
            raise self._not_found("GetParameter")
        return {"Parameter": {"Value": self.params[Name]["Value"]}}

    def delete_parameter(self, *, Name: str) -> None:
        if self.params.pop(Name, None) is None:
            raise self._not_found("DeleteParameter")


@pytest.fixture
def ssm() -> FakeSsm:
    return FakeSsm()


@pytest.fixture
def jwks_path(tmp_path: Path) -> Path:
    return tmp_path / "jwks.json"


def published(jwks_path: Path) -> dict:
    return json.loads(jwks_path.read_text())


def test_the_first_key_is_stored_published_and_activated(ssm, jwks_path, capsys) -> None:
    kid = issuer.create_key(ssm, jwks_path)

    stored = ssm.params[f"/ragdp/issuer/signing_keys/{kid}"]
    assert stored["Type"] == "SecureString"
    assert stored["Value"].startswith("-----BEGIN PRIVATE KEY-----")
    assert ssm.params["/ragdp/issuer/active_kid"] == {"Value": kid, "Type": "String"}
    [jwk] = published(jwks_path)["keys"]
    assert jwk["kid"] == kid
    # Only public members are committed, and the private key is never printed.
    assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}
    assert "PRIVATE" not in capsys.readouterr().out


def test_a_second_key_is_published_but_not_yet_active(ssm, jwks_path) -> None:
    first = issuer.create_key(ssm, jwks_path)
    second = issuer.create_key(ssm, jwks_path)

    assert second != first
    assert issuer.kids(published(jwks_path)) == [first, second]
    assert issuer.active_kid(ssm) == first


def test_activating_switches_the_signing_key(ssm, jwks_path) -> None:
    issuer.create_key(ssm, jwks_path)
    second = issuer.create_key(ssm, jwks_path)

    issuer.activate_key(ssm, jwks_path, second)

    token = tokens.mint(
        issuer.load_signing_key(ssm), issuer=ISSUER, tenant_id=TENANT, role_level=1, subject="t"
    )
    assert jwt.get_unverified_header(token)["kid"] == second


def test_a_key_missing_from_the_jwks_cant_be_activated(ssm, jwks_path) -> None:
    issuer.create_key(ssm, jwks_path)
    with pytest.raises(issuer.IssuerError, match="not in"):
        issuer.activate_key(ssm, jwks_path, "unknown-kid")


def test_the_active_key_cant_be_retired(ssm, jwks_path) -> None:
    kid = issuer.create_key(ssm, jwks_path)
    with pytest.raises(issuer.IssuerError, match="active"):
        issuer.retire_key(ssm, jwks_path, kid)
    assert issuer.kids(published(jwks_path)) == [kid]


def test_retiring_removes_the_key_everywhere(ssm, jwks_path) -> None:
    old = issuer.create_key(ssm, jwks_path)
    new = issuer.create_key(ssm, jwks_path)
    issuer.activate_key(ssm, jwks_path, new)

    issuer.retire_key(ssm, jwks_path, old)

    assert issuer.kids(published(jwks_path)) == [new]
    assert f"/ragdp/issuer/signing_keys/{old}" not in ssm.params


def test_minting_needs_an_active_key(ssm) -> None:
    with pytest.raises(issuer.IssuerError, match="create-key"):
        issuer.load_signing_key(ssm)


def test_a_private_key_stored_under_the_wrong_kid_is_refused(ssm, jwks_path) -> None:
    first = issuer.create_key(ssm, jwks_path)
    second = issuer.create_key(ssm, jwks_path)
    ssm.params[f"/ragdp/issuer/signing_keys/{first}"] = ssm.params[
        f"/ragdp/issuer/signing_keys/{second}"
    ]
    with pytest.raises(issuer.IssuerError, match="not that key"):
        issuer.load_signing_key(ssm)


# --- check_against_issuer: discovery -> jwks_uri -> kid -> signature and claims -------------


def fake_issuer(jwks: dict, discovery_issuer: str = ISSUER):
    documents = {
        f"{ISSUER}/.well-known/openid-configuration": {
            "issuer": discovery_issuer,
            "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        },
        f"{ISSUER}/.well-known/jwks.json": jwks,
    }
    return documents.__getitem__


@pytest.fixture
def token(ssm, jwks_path) -> str:
    issuer.create_key(ssm, jwks_path)
    return tokens.mint(
        issuer.load_signing_key(ssm), issuer=ISSUER, tenant_id=TENANT, role_level=3, subject="t"
    )


def test_a_minted_token_passes_the_check(token, jwks_path) -> None:
    claims = issuer.check_against_issuer(token, ISSUER, fetch=fake_issuer(published(jwks_path)))
    assert claims["tenant_id"] == str(TENANT)
    assert claims["role_level"] == 3


def test_the_check_fails_when_the_key_isnt_published_yet(token) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = tokens.public_jwk(other_key.public_key())
    with pytest.raises(issuer.IssuerError, match="no key"):
        issuer.check_against_issuer(token, ISSUER, fetch=fake_issuer({"keys": [other]}))


def test_the_check_fails_on_another_issuer(token, jwks_path) -> None:
    fetch = fake_issuer(published(jwks_path), discovery_issuer="https://evil.example")
    with pytest.raises(issuer.IssuerError, match="names issuer"):
        issuer.check_against_issuer(token, ISSUER, fetch=fetch)


def test_the_check_fails_on_the_wrong_audience(token, jwks_path) -> None:
    with pytest.raises(jwt.InvalidAudienceError):
        issuer.check_against_issuer(
            token, ISSUER, audience="someone-else", fetch=fake_issuer(published(jwks_path))
        )


def test_keys_are_never_fetched_over_plain_http() -> None:
    with pytest.raises(issuer.IssuerError, match="plain HTTP"):
        issuer.fetch_json("http://d111111abcdef8.cloudfront.net/.well-known/jwks.json")
