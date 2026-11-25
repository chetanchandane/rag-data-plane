"""The worker's database steps (SPEC: Ingestion, worker steps 1, 5, 7, 8 and "Failure handling").

Every function runs one short transaction as app_ingest through rls_transaction(), with the
tenant taken from the job and no role level (app_ingest's policies check the tenant only).
No transaction stays open while the worker downloads, parses or calls Bedrock.

The claim's `attempts` value is used as a fencing token: status changes after the claim
include `AND attempts = <claimed value>`. If this worker's lease expired and another worker
re-claimed the version (which increments attempts), this worker's late writes match no row
instead of overwriting the newer attempt.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from shared.db import WORKER_STATEMENT_TIMEOUT_MS, rls_transaction
from shared.errors import MAX_REASON_LENGTH, PermanentError
from shared.jobs import S3KeyParts
from worker.chunking import Chunk


@dataclass(frozen=True)
class Claim:
    """A version this worker holds the lease on."""

    tenant_id: UUID
    doc_id: UUID
    version: int
    attempts: int  # value after this claim: the fencing token
    sha256: str  # stored at registration; the file must match it
    min_role_level: int  # copied onto every chunk
    parser: str | None  # what a previous attempt parsed with, if any
    chunker_version: str | None  # what a previous attempt chunked with, if any


class LeaseLostError(Exception):
    """Another worker re-claimed this version. Transient: stop and let SQS decide."""


def _tx(conn: asyncpg.Connection, tenant_id: UUID):
    return rls_transaction(conn, tenant_id, None, statement_timeout_ms=WORKER_STATEMENT_TIMEOUT_MS)


_CLAIM = """
UPDATE document_versions
SET status = 'parsing', attempts = attempts + 1, lease_until = now() + interval '15 minutes'
WHERE doc_id = $1 AND version = $2 AND s3_key = $3
  AND (status IN ('pending_upload', 'uploaded')
       OR (status = 'failed' AND attempts < 3)
       OR (status IN ('parsing', 'embedding') AND lease_until < now()))
RETURNING attempts, sha256, parser, chunker_version
"""


async def claim(conn: asyncpg.Connection, parts: S3KeyParts, s3_key: str) -> Claim | None:
    """Claim a version with a 15-minute lease.

    Returns the Claim, or None if the version exists but is done, superseded or held by
    another worker (the caller deletes the message and exits cleanly).
    Raises PermanentError if no version matches this doc_id, version and s3_key. The key's
    tenant/doc/version are only a claim until a stored row with the same s3_key confirms them.
    """
    async with _tx(conn, parts.tenant_id):
        row = await conn.fetchrow(_CLAIM, parts.doc_id, parts.version, s3_key)
        if row is None:
            status = await conn.fetchval(
                "SELECT status FROM document_versions"
                " WHERE doc_id = $1 AND version = $2 AND s3_key = $3",
                parts.doc_id,
                parts.version,
                s3_key,
            )
            if status is None:
                raise PermanentError("no_matching_version", "key matches no registered version")
            return None
        min_role_level = await conn.fetchval(
            "SELECT min_role_level FROM documents WHERE id = $1", parts.doc_id
        )
    return Claim(
        tenant_id=parts.tenant_id,
        doc_id=parts.doc_id,
        version=parts.version,
        attempts=row["attempts"],
        sha256=row["sha256"],
        min_role_level=min_role_level,
        parser=row["parser"],
        chunker_version=row["chunker_version"],
    )


async def start_embedding(
    conn: asyncpg.Connection,
    claim: Claim,
    *,
    parser: str,
    chunker_version: str,
    embedding_model: str,
    chunk_count: int,
) -> None:
    """Record how this version was parsed and chunked, and move it to 'embedding'."""
    async with _tx(conn, claim.tenant_id):
        updated = await conn.fetchval(
            """
            UPDATE document_versions
            SET status = 'embedding', parser = $4, chunker_version = $5,
                embedding_model = $6, chunk_count = $7
            WHERE doc_id = $1 AND version = $2 AND attempts = $3 AND status = 'parsing'
            RETURNING 1
            """,
            claim.doc_id,
            claim.version,
            claim.attempts,
            parser,
            chunker_version,
            embedding_model,
            chunk_count,
        )
        if updated is None:
            raise LeaseLostError(f"lost the lease on {claim.doc_id} v{claim.version}")
        # Resume reuses stored chunks by chunk_index. That is only valid if they came from
        # the same parser and chunker: either one changes what chunk N contains (moving
        # footnotes did, DECISIONS 2026-11-24 "Step 19"). After a change, start this
        # version's chunks over.
        before = (claim.parser, claim.chunker_version)
        if claim.chunker_version is not None and before != (parser, chunker_version):
            await conn.execute(
                "DELETE FROM chunks WHERE doc_id = $1 AND version = $2",
                claim.doc_id,
                claim.version,
            )


async def existing_chunk_indexes(conn: asyncpg.Connection, claim: Claim) -> set[int]:
    """chunk_index values already stored for this version (by an earlier attempt)."""
    async with _tx(conn, claim.tenant_id):
        rows = await conn.fetch(
            "SELECT chunk_index FROM chunks WHERE doc_id = $1 AND version = $2",
            claim.doc_id,
            claim.version,
        )
    return {row["chunk_index"] for row in rows}


async def insert_chunks(
    conn: asyncpg.Connection,
    claim: Claim,
    chunks: Sequence[Chunk],
    embeddings: Sequence[list[float]],
) -> None:
    """Insert one batch of embedded chunks. A chunk_index that already exists is skipped,
    so a duplicate delivery or a retry after a crash never creates duplicates."""
    rows = [
        (
            claim.tenant_id,
            claim.doc_id,
            claim.version,
            c.index,
            claim.min_role_level,
            c.page_start,
            c.page_end,
            c.section_path,
            c.content,
            embedding,
        )
        for c, embedding in zip(chunks, embeddings, strict=True)
    ]
    async with _tx(conn, claim.tenant_id):
        await conn.executemany(
            """
            INSERT INTO chunks (tenant_id, doc_id, version, chunk_index, min_role_level,
                                page_start, page_end, section_path, content, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (doc_id, version, chunk_index) DO NOTHING
            """,
            rows,
        )


async def activate(conn: asyncpg.Connection, claim: Claim) -> bool:
    """Make this version the live one, forward only (SPEC SQL). One transaction.

    Returns True if activated. Returns False if a newer version is already live: this
    version is then marked superseded instead.
    """
    async with _tx(conn, claim.tenant_id):
        activated = await conn.fetchval(
            """
            UPDATE documents SET active_version = $2
            WHERE id = $1 AND (active_version IS NULL OR active_version < $2)
            RETURNING id
            """,
            claim.doc_id,
            claim.version,
        )
        if activated is None:
            # Nothing has changed in this transaction yet, so marking superseded here is
            # the same as the SPEC's "ROLLBACK, mark this version superseded".
            await conn.execute(
                "UPDATE document_versions SET status = 'superseded'"
                " WHERE doc_id = $1 AND version = $2",
                claim.doc_id,
                claim.version,
            )
            return False
        await conn.execute(
            """
            UPDATE document_versions
            SET status = CASE WHEN version = $2 THEN 'indexed' ELSE 'superseded' END,
                indexed_at = CASE WHEN version = $2 THEN now() ELSE indexed_at END,
                -- An earlier attempt's error no longer describes a version that is live.
                last_error = CASE WHEN version = $2 THEN NULL ELSE last_error END
            WHERE doc_id = $1 AND (version = $2 OR status = 'indexed')
            """,
            claim.doc_id,
            claim.version,
        )
        # The epoch is part of the query cache key: bumping it invalidates cached answers.
        await conn.execute(
            """
            INSERT INTO tenant_index_state (tenant_id, epoch) VALUES ($1, 1)
            ON CONFLICT (tenant_id) DO UPDATE SET epoch = tenant_index_state.epoch + 1
            """,
            claim.tenant_id,
        )
    return True


async def cleanup_superseded(conn: asyncpg.Connection, claim: Claim) -> int:
    """Delete chunks of this document's superseded versions. Separate from activation so a
    slow delete never holds up the switch to the new version. Returns rows deleted."""
    async with _tx(conn, claim.tenant_id):
        result = await conn.execute(
            """
            DELETE FROM chunks
            WHERE doc_id = $1
              AND version IN (SELECT version FROM document_versions
                              WHERE doc_id = $1 AND status = 'superseded')
            """,
            claim.doc_id,
        )
    return int(result.split()[-1])  # "DELETE <n>"


async def mark_failed(conn: asyncpg.Connection, claim: Claim, reason: str) -> bool:
    """Record a failure on a version this worker claimed. Returns False if the claim was
    lost (another worker owns the version now), in which case nothing is changed."""
    async with _tx(conn, claim.tenant_id):
        updated = await conn.fetchval(
            """
            UPDATE document_versions
            SET status = 'failed', last_error = $4, lease_until = NULL
            WHERE doc_id = $1 AND version = $2 AND attempts = $3
              AND status IN ('parsing', 'embedding')
            RETURNING 1
            """,
            claim.doc_id,
            claim.version,
            claim.attempts,
            reason[:MAX_REASON_LENGTH],
        )
    return updated is not None
