"""Seeding helpers for the retrieval DB tests: fixed embeddings and documents written as
app_ingest, the way the worker writes them."""

import math
import uuid
from collections.abc import Sequence

import asyncpg

from shared.db import rls_transaction

DIMENSIONS = 1024


def unit(*weights: tuple[int, float]) -> list[float]:
    """A unit vector with the given (dimension, weight) components."""
    vector = [0.0] * DIMENSIONS
    for dim, weight in weights:
        vector[dim] = weight
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


E0 = unit((0, 1.0))  # the query's direction
NEAR = unit((0, 0.8), (1, 0.6))  # cosine distance 0.2 from E0
FAR = unit((1, 1.0))  # cosine distance 1 from E0


def rrf(dense_rank: int | None, lexical_rank: int | None, weight: float = 1.0) -> float:
    score = 0.0
    if dense_rank is not None:
        score += 1.0 / (60 + dense_rank)
    if lexical_rank is not None:
        score += weight / (60 + lexical_rank)
    return score


async def seed_doc(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    chunks: Sequence[tuple[str, list[float]]],
    *,
    min_role_level: int = 1,
    version: int = 1,
    active: bool = True,
    deleted: bool = False,
    doc_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One document version with these (content, embedding) chunks. active=False leaves the
    version not live (as while a newer one is still ingesting, or after it's superseded)."""
    async with pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
        if doc_id is None:
            doc_id = await conn.fetchval(
                "INSERT INTO documents (tenant_id, external_id, title, min_role_level)"
                " VALUES ($1, $2, 't', $3) RETURNING id",
                tenant_id,
                f"doc-{uuid.uuid4()}",
                min_role_level,
            )
        await conn.execute(
            "INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key, status)"
            " VALUES ($1, $2, $3, $4, 'raw/x', 'indexed')",
            doc_id,
            version,
            tenant_id,
            f"{version:064x}",
        )
        await conn.executemany(
            "INSERT INTO chunks (tenant_id, doc_id, version, chunk_index, min_role_level,"
            " page_start, page_end, section_path, content, embedding)"
            " VALUES ($1, $2, $3, $4, $5, 1, 2, '1 > 1.1', $6, $7)",
            [
                (tenant_id, doc_id, version, i, min_role_level, content, embedding)
                for i, (content, embedding) in enumerate(chunks)
            ],
        )
        if active:
            await conn.execute(
                "UPDATE documents SET active_version = $2 WHERE id = $1", doc_id, version
            )
        if deleted:
            await conn.execute("UPDATE documents SET deleted_at = now() WHERE id = $1", doc_id)
    return doc_id


def fixed_embedding(vector: list[float]):
    calls: list[str] = []

    def embed(query: str) -> list[float]:
        calls.append(query)
        return vector

    embed.calls = calls
    return embed
