"""DB tests for the worker's claim, failure marking and a full run (Neon dev branch).

Embeddings are faked: these tests never call Bedrock. The four ingestion guarantees (run
twice, crash and resume, out-of-order versions, wrong hash) are in test_guarantees.py.
"""

import uuid

import asyncpg
import pytest

from shared.db import rls_transaction
from shared.errors import PermanentError
from shared.jobs import S3KeyParts
from tests.worker.dbhelpers import MODEL, fake_embed, key_for, registered, version_row
from tests.worker.pdfgen import Page, make_pdf
from worker import store
from worker.chunking import CHUNKER_VERSION
from worker.pipeline import ingest_version

pytestmark = pytest.mark.db


# --- claim ---------------------------------------------------------------------------------


async def test_claim_takes_an_uploaded_version(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    parts, key = await registered(pool, tenant)
    async with pool.acquire() as conn:
        claim = await store.claim(conn, parts, key)
    assert claim is not None
    assert (claim.attempts, claim.min_role_level) == (1, 2)
    row = await version_row(pool, parts)
    assert row["status"] == "parsing"
    assert row["lease_until"] is not None


async def test_a_held_version_is_skipped(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    parts, key = await registered(pool, tenant)
    async with pool.acquire() as conn:
        assert await store.claim(conn, parts, key) is not None
        # A duplicate delivery while the lease is live: delete the message, touch nothing.
        assert await store.claim(conn, parts, key) is None
    assert (await version_row(pool, parts))["attempts"] == 1


async def test_a_key_with_no_registered_version_is_permanent_and_touches_nothing(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    parts, key = await registered(pool, tenant)
    missing = S3KeyParts(tenant, parts.doc_id, 7)
    async with pool.acquire() as conn:
        with pytest.raises(PermanentError) as exc_info:
            await store.claim(conn, missing, key_for(tenant, parts.doc_id, 7))
    assert exc_info.value.code == "no_matching_version"
    row = await version_row(pool, parts)
    assert (row["status"], row["attempts"]) == ("uploaded", 0)


async def test_a_key_naming_another_tenant_matches_nothing(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    # A forged key that pairs tenant B with tenant A's document: RLS scopes the claim to
    # tenant B, so tenant A's version is invisible and untouched.
    parts, _ = await registered(pool, tenant)
    other = uuid.uuid4()
    async with pool.acquire() as conn:
        with pytest.raises(PermanentError, match="key matches no registered version"):
            await store.claim(
                conn, S3KeyParts(other, parts.doc_id, 1), key_for(other, parts.doc_id, 1)
            )
    assert (await version_row(pool, parts))["status"] == "uploaded"


# --- mark_failed ---------------------------------------------------------------------------


async def test_mark_failed_needs_the_current_claim(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    parts, key = await registered(pool, tenant)
    async with pool.acquire() as conn:
        claim = await store.claim(conn, parts, key)
        assert claim is not None
        stale = store.Claim(**{**claim.__dict__, "attempts": claim.attempts - 1})
        # A worker whose lease was taken over must not overwrite the newer attempt.
        assert await store.mark_failed(conn, stale, "late write") is False
        assert (await version_row(pool, parts))["status"] == "parsing"

        assert await store.mark_failed(conn, claim, "x" * 1000) is True
    row = await version_row(pool, parts)
    assert row["status"] == "failed"
    assert row["last_error"] == "x" * 256  # capped
    assert row["lease_until"] is None


# --- full run with a fake embedder ---------------------------------------------------------


async def test_a_pdf_reaches_indexed_with_its_chunks(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    data = make_pdf([Page([f"Paragraph {i} of page {p}." for i in range(5)]) for p in range(3)])
    parts, key = await registered(pool, tenant, data)

    result = await ingest_version(
        pool, key, load_source=lambda: data, embed=fake_embed, embedding_model=MODEL
    )

    assert result.outcome == "indexed"
    assert result.chunk_count == result.embedded_now > 0
    row = await version_row(pool, parts)
    assert row["status"] == "indexed"
    assert (row["chunker_version"], row["embedding_model"]) == (CHUNKER_VERSION, MODEL)
    assert row["chunk_count"] == result.chunk_count
    async with pool.acquire() as conn, rls_transaction(conn, tenant, None):
        assert (
            await conn.fetchval("SELECT active_version FROM documents WHERE id = $1", parts.doc_id)
            == 1
        )
        chunks = await conn.fetch(
            "SELECT chunk_index, min_role_level FROM chunks WHERE doc_id = $1 ORDER BY 1",
            parts.doc_id,
        )
        assert await conn.fetchval("SELECT epoch FROM tenant_index_state") == 1
    assert [c["chunk_index"] for c in chunks] == list(range(result.chunk_count))
    assert {c["min_role_level"] for c in chunks} == {2}  # copied from the document


async def test_a_retry_that_succeeds_clears_the_earlier_error(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    data = make_pdf([Page(["Some text on the only page."])])
    parts, key = await registered(pool, tenant, data)
    async with pool.acquire() as conn:
        claim = await store.claim(conn, parts, key)
        assert claim is not None
        assert await store.mark_failed(conn, claim, "transient: ThrottlingException") is True

    result = await ingest_version(
        pool, key, load_source=lambda: data, embed=fake_embed, embedding_model=MODEL
    )

    assert result.outcome == "indexed"
    row = await version_row(pool, parts)
    assert (row["status"], row["attempts"], row["last_error"]) == ("indexed", 2, None)
