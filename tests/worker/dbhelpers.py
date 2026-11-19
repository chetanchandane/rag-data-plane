"""Helpers for worker DB tests: register versions, read rows back, fake the embedder."""

import hashlib
import uuid

import asyncpg

from shared.db import rls_transaction
from shared.jobs import S3KeyParts
from worker.local import register

MODEL = "fake-embedding-model"


def key_for(tenant_id: uuid.UUID, doc_id: uuid.UUID, version: int) -> str:
    return f"raw/{tenant_id}/{doc_id}/v{version}/source.pdf"


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.5] * 1024 for _ in texts]


async def registered(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    data: bytes = b"pdf bytes",
    version: int = 1,
    *,
    doc_id: uuid.UUID | None = None,
    sha256: str | None = None,
) -> tuple[S3KeyParts, str]:
    """Register a version the way the API will (status 'uploaded'). A new document unless
    doc_id is given; sha256 defaults to the real hash of data."""
    doc_id = doc_id or uuid.uuid4()
    key = key_for(tenant_id, doc_id, version)
    async with pool.acquire() as conn:
        await register(
            conn,
            tenant_id,
            doc_id,
            version,
            sha256=sha256 or hashlib.sha256(data).hexdigest(),
            s3_key=key,
            title="t",
            min_role_level=2,
        )
    return S3KeyParts(tenant_id, doc_id, version), key


async def version_row(pool: asyncpg.Pool, parts: S3KeyParts) -> asyncpg.Record:
    async with pool.acquire() as conn, rls_transaction(conn, parts.tenant_id, None):
        return await conn.fetchrow(
            "SELECT * FROM document_versions WHERE doc_id = $1 AND version = $2",
            parts.doc_id,
            parts.version,
        )
