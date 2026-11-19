"""Seeding test tenants for the RLS tests.

Seeding goes through app_ingest (app_owner can't write: FORCE RLS and no policy for it),
with role_level=None exactly like the worker: app_ingest's policies check the tenant only.
"""

import uuid
from dataclasses import dataclass

import asyncpg

from shared.db import rls_transaction


@dataclass(frozen=True)
class SeededDoc:
    doc_id: uuid.UUID
    min_role_level: int


@dataclass(frozen=True)
class Tenant:
    id: uuid.UUID
    docs: dict[int, SeededDoc]  # min_role_level -> document


async def seed_tenant(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> Tenant:
    """Two indexed documents (role level 1 and 3), one version and one chunk each."""
    docs: dict[int, SeededDoc] = {}
    async with rls_transaction(conn, tenant_id, None):
        for level in (1, 3):
            doc_id = await conn.fetchval(
                """
                INSERT INTO documents (tenant_id, external_id, title, min_role_level)
                VALUES ($1, $2, $3, $4) RETURNING id
                """,
                tenant_id,
                f"doc-level-{level}",
                f"Level {level} document",
                level,
            )
            await conn.execute(
                """
                INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key, status)
                VALUES ($1, 1, $2, $3, $4, 'indexed')
                """,
                doc_id,
                tenant_id,
                "0" * 64,
                f"raw/{tenant_id}/{doc_id}/v1/source.pdf",
            )
            # The embedding is built in SQL, so no vector codec is needed until step 4.
            await conn.execute(
                """
                INSERT INTO chunks (tenant_id, doc_id, version, chunk_index, min_role_level,
                                    content, embedding)
                VALUES ($1, $2, 1, 0, $3, $4, array_fill(0::real, ARRAY[1024])::vector)
                """,
                tenant_id,
                doc_id,
                level,
                f"Content for tenant {tenant_id} at level {level}",
            )
            await conn.execute("UPDATE documents SET active_version = 1 WHERE id = $1", doc_id)
            docs[level] = SeededDoc(doc_id=doc_id, min_role_level=level)
    return Tenant(id=tenant_id, docs=docs)


async def delete_tenant(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> None:
    # No WHERE clause needed: RLS limits the DELETE to this tenant. Versions and chunks
    # go with their documents (ON DELETE CASCADE).
    async with rls_transaction(conn, tenant_id, None):
        await conn.execute("DELETE FROM documents")
