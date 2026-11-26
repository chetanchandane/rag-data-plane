"""scripts/deployed_api.py's token handling and scripts/loadtest.py's corpus checks.
A throwaway key and a fake clock: no SSM, no network."""

import hashlib
import json
import sys
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shared import tokens
from tests.leak.matrix import load_script

# The scripts import each other by name, which works when they run from scripts/. Registered
# only while they load, so nothing else in the session picks up these copies.
sys.modules["issuer"] = load_script("issuer")
try:
    deployed_api = load_script("deployed_api")
    sys.modules["deployed_api"] = deployed_api
    loadtest = load_script("loadtest")
finally:
    sys.modules.pop("deployed_api", None)
    del sys.modules["issuer"]

ISSUER = "https://issuer.example.test"
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def clock():
    class Clock:
        now = 1_000_000.0

        def __call__(self) -> float:
            return self.now

    return Clock()


def make_api(key, clock, monkeypatch, mints: list[int]):
    monkeypatch.setenv("JWT_ISSUER", ISSUER)
    real_mint = deployed_api.mint_token

    def counting_mint(*args, **kwargs):
        mints.append(1)
        return real_mint(*args, **kwargs)

    monkeypatch.setattr(deployed_api, "mint_token", counting_mint)
    return deployed_api.Api(
        "https://api.example.test",
        TENANT,
        subject="test",
        scopes=("retrieve",),
        role_level=1,
        key=key,
        clock=clock,
    )


def test_the_token_is_minted_once_then_reused_until_it_is_old(key, clock, monkeypatch):
    mints: list[int] = []
    api = make_api(key, clock, monkeypatch, mints)
    first = api._auth()
    clock.now += deployed_api.TOKEN_MAX_AGE_S  # exactly the max age: still reused
    assert api._auth() == first
    assert len(mints) == 1
    clock.now += 1
    api._auth()
    assert len(mints) == 2


def test_tokens_are_replaced_well_before_they_expire():
    assert deployed_api.TOKEN_MAX_AGE_S <= tokens.TOKEN_LIFETIME_SECONDS - 60


def test_a_minted_token_carries_the_tenant_level_subject_and_scopes(key):
    token = deployed_api.mint_token(
        key, ISSUER, TENANT, subject="loadtest", scopes=("retrieve", "query"), role_level=1
    )
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], audience=tokens.AUDIENCE)
    assert claims["iss"] == ISSUER
    assert claims["tenant_id"] == str(TENANT)
    assert claims["role_level"] == 1
    assert claims["sub"] == "loadtest"
    assert set(claims["scope"].split()) == {"retrieve", "query"}


def test_api_refuses_to_start_without_an_issuer(key, monkeypatch):
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    with pytest.raises(SystemExit, match="JWT_ISSUER"):
        deployed_api.Api("https://api.example.test", TENANT, subject="t", scopes=(), key=key)


# --- loadtest.corpus() ----------------------------------------------------------------------


def write_corpus(tmp_path, monkeypatch, *, data: bytes, sha256: str, level: int = 1) -> None:
    manifest = tmp_path / "corpus.json"
    entry = {
        "external_id": "doc-1",
        "filename": "doc-1.pdf",
        "sha256": sha256,
        "min_role_level": level,
    }
    manifest.write_text(json.dumps({"documents": [entry]}))
    (tmp_path / "doc-1.pdf").write_bytes(data)
    monkeypatch.setattr(loadtest, "MANIFEST", manifest)
    monkeypatch.setattr(loadtest, "CORPUS_DIR", tmp_path)


def test_corpus_returns_files_that_match_the_manifest(tmp_path, monkeypatch):
    data = b"%PDF-1.7 test"
    write_corpus(tmp_path, monkeypatch, data=data, sha256=hashlib.sha256(data).hexdigest())
    [(entry, read)] = loadtest.corpus()
    assert entry["external_id"] == "doc-1"
    assert read == data


def test_corpus_refuses_a_changed_file(tmp_path, monkeypatch):
    write_corpus(tmp_path, monkeypatch, data=b"changed", sha256="0" * 64)
    with pytest.raises(SystemExit, match="doesn't match"):
        loadtest.corpus()


def test_corpus_refuses_a_missing_file(tmp_path, monkeypatch):
    write_corpus(tmp_path, monkeypatch, data=b"x", sha256="0" * 64)
    (tmp_path / "doc-1.pdf").unlink()
    with pytest.raises(SystemExit, match="eval_corpus.py fetch"):
        loadtest.corpus()


def test_corpus_refuses_a_document_above_the_tokens_level(tmp_path, monkeypatch):
    data = b"%PDF-1.7 test"
    sha = hashlib.sha256(data).hexdigest()
    write_corpus(tmp_path, monkeypatch, data=data, sha256=sha, level=2)
    with pytest.raises(SystemExit, match="above level 1"):
        loadtest.corpus()
