"""Build the API app for tests without its lifespan: the test supplies pool, S3 and settings.

The S3 client has fake static credentials. Presigning is local computation, so no test here
ever calls AWS.
"""

import hashlib
import uuid

import boto3
import httpx
from fastapi import FastAPI

from api.app import create_app
from api.config import Settings
from api.uploads import make_s3_client

TOKEN = "test-token-not-a-secret"
BUCKET = "ragdp-raw-test"
REGION = "us-east-2"


def fake_s3():
    session = boto3.session.Session(
        aws_access_key_id="AKIATESTTESTTESTTEST",
        aws_secret_access_key="not-a-real-secret",
        region_name=REGION,
    )
    return make_s3_client(REGION, session=session)


def build_app(pool, tenant_id: uuid.UUID, role_level: int) -> FastAPI:
    app = create_app()
    app.state.settings = Settings(
        database_url="unused-in-tests",
        raw_bucket=BUCKET,
        aws_region=REGION,
        dev_auth_token=TOKEN,
        dev_tenant_id=tenant_id,
        dev_role_level=role_level,
    )
    app.state.pool = pool
    app.state.s3 = fake_s3()
    return app


def client(app: FastAPI, token: str | None = TOKEN) -> httpx.AsyncClient:
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
