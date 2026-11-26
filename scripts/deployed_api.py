"""A client for the deployed API, used by the scripts that act exactly as a real client would:
tokens minted with scripts/issuer.py's key (from SSM), JSON over HTTPS, uploads to presigned
S3 URLs. Used by scripts/chaos.py (step 23) and scripts/loadtest.py (step 24).

Not imported by anything that runs on Lambda (it needs issuer.py, so PyJWT).
"""

import hashlib
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import boto3
import httpx
import issuer  # scripts/issuer.py: the signing key in SSM
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from shared import tokens

TERMINAL = {"indexed", "failed", "superseded"}
POLL_SECONDS = 1.0
# A watch over many ingests can outlast one token, so a token this old is replaced before
# the next request: 5 minutes before it expires.
TOKEN_MAX_AGE_S = tokens.TOKEN_LIFETIME_SECONDS - 300


def jwt_issuer() -> str:
    """The issuer URL tokens are signed for (JWT_ISSUER, from .env)."""
    url = os.environ.get("JWT_ISSUER", "").strip()
    if not url:
        sys.exit("JWT_ISSUER is not set (.env)")
    return url


def signing_key() -> RSAPrivateKey:
    """The active signing key, from SSM (needs a human sign-in; see scripts/issuer.py)."""
    return issuer.load_signing_key(boto3.client("ssm"))


def mint_token(
    key: RSAPrivateKey,
    issuer_url: str,
    tenant: uuid.UUID,
    *,
    subject: str,
    scopes: Iterable[str],
    role_level: int,
) -> str:
    return tokens.mint(
        key,
        issuer=issuer_url,
        tenant_id=tenant,
        role_level=role_level,
        subject=subject,
        scopes=tuple(scopes),
    )


class Api:
    def __init__(
        self,
        url: str,
        tenant: uuid.UUID,
        *,
        subject: str,
        scopes: Iterable[str],
        role_level: int = 3,
        key: RSAPrivateKey | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer_url = jwt_issuer()  # fail before any request if it's missing
        self.tenant = tenant
        self.subject = subject
        self.scopes = tuple(scopes)
        self.role_level = role_level
        self.key = key if key is not None else signing_key()  # read from SSM once
        self.clock = clock
        self.minted_at: float | None = None
        self.token = ""
        self.http = httpx.Client(base_url=url, timeout=30)

    def _auth(self) -> dict[str, str]:
        now = self.clock()
        if self.minted_at is None or now - self.minted_at > TOKEN_MAX_AGE_S:
            self.token = mint_token(
                self.key,
                self.issuer_url,
                self.tenant,
                subject=self.subject,
                scopes=self.scopes,
                role_level=self.role_level,
            )
            self.minted_at = now
        return {"Authorization": f"Bearer {self.token}"}

    def register(
        self, data: bytes, external_id: str, filename: str, min_role_level: int = 1
    ) -> dict[str, Any]:
        body = {
            "external_id": external_id,
            "filename": filename,
            "content_sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "min_role_level": min_role_level,
        }
        resp = self.http.post("/v1/documents", json=body, headers=self._auth())
        resp.raise_for_status()
        return resp.json()

    def document(self, doc_id: str) -> dict[str, Any]:
        resp = self.http.get(f"/v1/documents/{doc_id}", headers=self._auth())
        resp.raise_for_status()
        return resp.json()

    def delete(self, doc_id: str) -> int:
        return self.http.delete(f"/v1/documents/{doc_id}", headers=self._auth()).status_code


def upload(registered: dict[str, Any], data: bytes) -> float:
    """PUT to the presigned URL; returns the time the upload finished (epoch seconds)."""
    up = registered["upload"]
    if up is None:
        sys.exit(f"version {registered['version']} needs no upload (duplicate?)")
    resp = httpx.put(up["url"], content=data, headers=up["headers"], timeout=60)
    resp.raise_for_status()
    return time.time()


def watch(
    api: Api, doc_id: str, version: int, uploaded_at: float, limit_s: float
) -> dict[str, Any]:
    """Poll the version once a second until it's terminal or limit_s passes. Returns when
    each status was first seen, in seconds after the upload. A 429, a 5xx or a network
    error on one poll (a cold start, a throttle) is counted and polled again, so it can't
    end a run; anything else (a 401, a 404) raises."""
    seen: dict[str, float] = {}
    deadline = uploaded_at + limit_s
    status = "?"
    transient_errors = 0
    while time.time() < deadline:
        try:
            document = api.document(doc_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 429 and error.response.status_code < 500:
                raise
            transient_errors += 1
            time.sleep(POLL_SECONDS)
            continue
        except httpx.TransportError:
            transient_errors += 1
            time.sleep(POLL_SECONDS)
            continue
        versions = {v["version"]: v for v in document["versions"]}
        current = versions[version]
        status = current["status"]
        seen.setdefault(status, round(time.time() - uploaded_at, 1))
        if status in TERMINAL:
            return {
                "status": status,
                "seen_at_s": seen,
                "version": current,
                "transient_errors": transient_errors,
            }
        time.sleep(POLL_SECONDS)
    return {
        "status": status,
        "seen_at_s": seen,
        "version": None,
        "timed_out": True,
        "transient_errors": transient_errors,
    }
