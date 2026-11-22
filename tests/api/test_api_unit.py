"""API unit tests: request validation, presigned URLs, settings. No network.

Auth: tests/api/test_auth.py.
"""

import base64
import hashlib
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.config import MAX_UPLOAD_BYTES, UPLOAD_URL_EXPIRES_SECONDS, Settings
from api.uploads import checksum_header, presign_upload
from shared.jobs import parse_s3_key, s3_key_for
from tests.api.apphelpers import BUCKET, build_app, client, fake_s3, registration

TENANT = uuid.UUID("7d1e0000-0000-4000-8000-000000000001")


class UntouchablePool:
    """Fails the test if an endpoint reaches the database."""

    def acquire(self):
        raise AssertionError("the request reached the database")


def app(role_level: int = 3):
    return build_app(UntouchablePool(), TENANT, role_level)


# --- request validation (rejected before the database) ------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_sha256", "abc"),
        ("content_sha256", "g" * 64),
        ("size_bytes", 0),
        ("size_bytes", MAX_UPLOAD_BYTES + 1),
        ("size_bytes", "123"),  # strict: no string-to-int coercion
        ("min_role_level", 0),
        ("min_role_level", 4),
        ("min_role_level", True),  # strict: a bool is not a level
        ("external_id", ""),
        ("external_id", "x" * 257),
        ("filename", ""),
    ],
)
async def test_invalid_registrations_get_422(field, value):
    async with client(app()) as c:
        resp = await c.post("/v1/documents", json=registration() | {field: value})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", field]


async def test_missing_field_gets_422():
    body = registration()
    del body["content_sha256"]
    async with client(app()) as c:
        resp = await c.post("/v1/documents", json=body)
    assert resp.status_code == 422


async def test_a_level_above_the_callers_role_gets_403():
    async with client(app(role_level=1)) as c:
        resp = await c.post("/v1/documents", json=registration(min_role_level=2))
    assert resp.status_code == 403


async def test_a_document_id_that_is_not_a_uuid_gets_422():
    async with client(app()) as c:
        resp = await c.get("/v1/documents/not-a-uuid")
    assert resp.status_code == 422


# --- presigned upload URL -----------------------------------------------------------------


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_presigned_url_signs_the_checksum_and_the_size():
    data = b"%PDF-1.7 hello"
    sha = hashlib.sha256(data).hexdigest()
    key = s3_key_for(TENANT, uuid.uuid4(), 3)
    before = datetime.now(UTC)
    upload = presign_upload(fake_s3(), bucket=BUCKET, key=key, sha256_hex=sha, size_bytes=len(data))

    parsed = urllib.parse.urlparse(upload.url)
    assert parsed.scheme == "https"
    assert parsed.netloc == f"{BUCKET}.s3.us-east-2.amazonaws.com"  # Regional endpoint
    assert parsed.path == f"/{key}"
    query = _query(upload.url)
    # Signed headers are what S3 enforces: change either and the signature no longer matches.
    assert query["X-Amz-SignedHeaders"].split(";") == [
        "content-length",
        "host",
        "x-amz-checksum-sha256",
    ]
    assert query["X-Amz-Expires"] == str(UPLOAD_URL_EXPIRES_SECONDS)
    assert upload.method == "PUT"
    assert upload.headers == {"x-amz-checksum-sha256": checksum_header(sha)}
    expected_expiry = before + timedelta(seconds=UPLOAD_URL_EXPIRES_SECONDS)
    assert expected_expiry <= upload.expires_at <= expected_expiry + timedelta(seconds=5)


def test_checksum_header_is_base64_of_the_raw_digest():
    data = b"some bytes"
    assert (
        checksum_header(hashlib.sha256(data).hexdigest())
        == base64.b64encode(hashlib.sha256(data).digest()).decode()
    )


def test_s3_key_for_round_trips_through_the_workers_parser():
    doc = uuid.uuid4()
    key = s3_key_for(TENANT, doc, 12)
    assert key == f"raw/{TENANT}/{doc}/v12/source.pdf"
    assert parse_s3_key(key) == (TENANT, doc, 12)
    with pytest.raises(ValueError):
        s3_key_for(TENANT, doc, 0)


# --- settings -----------------------------------------------------------------------------

_ENV = {
    "DATABASE_URL_API": "postgresql://app_api@host/db",
    "RAW_BUCKET": BUCKET,
    "AWS_REGION": "us-east-2",
    "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
    "JWT_ISSUER": "https://d111111abcdef8.cloudfront.net",
}


class FakeSsm:
    def __init__(self):
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        return {"Parameter": {"Value": "postgresql://app_api@from-ssm/db"}}


def _set_env(monkeypatch, env):
    monkeypatch.delenv("DATABASE_URL_API_PARAM", raising=False)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_settings_read_from_the_environment(monkeypatch):
    _set_env(monkeypatch, _ENV)
    ssm = FakeSsm()
    settings = Settings.from_env(ssm=ssm)
    assert settings.database_url == _ENV["DATABASE_URL_API"]
    assert ssm.calls == []  # a URL in the environment never touches SSM
    assert settings.jwt_issuer == "https://d111111abcdef8.cloudfront.net"


def test_jwt_issuer_is_optional_for_lambda(monkeypatch):
    # On Lambda API Gateway verifies tokens, so there's no issuer to verify against.
    _set_env(monkeypatch, {k: v for k, v in _ENV.items() if k != "JWT_ISSUER"})
    assert Settings.from_env(ssm=FakeSsm()).jwt_issuer is None


def test_the_database_url_can_come_from_ssm(monkeypatch):
    _set_env(monkeypatch, _ENV | {"DATABASE_URL_API": ""})
    monkeypatch.setenv("DATABASE_URL_API_PARAM", "/ragdp/prod/database_url_api")
    ssm = FakeSsm()
    settings = Settings.from_env(ssm=ssm)
    assert settings.database_url == "postgresql://app_api@from-ssm/db"
    assert ssm.calls == [{"Name": "/ragdp/prod/database_url_api", "WithDecryption": True}]


@pytest.mark.parametrize("param", ["", "/ragdp/prod/database_url_api"], ids=["neither", "both"])
def test_settings_need_exactly_one_database_url_source(monkeypatch, param):
    url = "" if param == "" else _ENV["DATABASE_URL_API"]
    _set_env(monkeypatch, _ENV | {"DATABASE_URL_API": url})
    monkeypatch.setenv("DATABASE_URL_API_PARAM", param)
    with pytest.raises(RuntimeError, match="exactly one"):
        Settings.from_env(ssm=FakeSsm())


@pytest.mark.parametrize(
    "issuer",
    ["http://d111111abcdef8.cloudfront.net", "https://d111111abcdef8.cloudfront.net/"],
    ids=["plain_http", "trailing_slash"],
)
def test_settings_fail_at_startup_on_a_bad_issuer(monkeypatch, issuer):
    _set_env(monkeypatch, _ENV | {"JWT_ISSUER": issuer})
    with pytest.raises(ValueError, match="JWT_ISSUER"):
        Settings.from_env(ssm=FakeSsm())
