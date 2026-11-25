"""Step 6: the ingestion guarantees (SPEC: Ingestion; Proof, "Chaos tests").

Every job must be safe to deliver twice, safe to crash halfway and safe to arrive out of
order, and a file that doesn't match its registered hash must never be parsed.

Each test runs the real ingest_version() against the Neon dev branch as app_ingest. The
embedder is a fake that records what it was asked to embed: nothing here calls Bedrock.
"""

import asyncio
import functools
import hashlib
import uuid

import asyncpg
import pytest

from shared.db import rls_transaction
from shared.errors import PermanentError
from shared.jobs import S3KeyParts
from tests.worker.dbhelpers import MODEL, registered, version_row
from tests.worker.pdfgen import Page, make_pdf
from worker import pipeline
from worker.pipeline import IngestResult, ingest_version

pytestmark = pytest.mark.db


class RecordingEmbed:
    """Fake embedder that records every text it embeds. With fail_on_call=n, the n-th
    call raises `failure` instead of returning vectors (counting from 1)."""

    def __init__(
        self, fail_on_call: int | None = None, failure: BaseException | None = None
    ) -> None:
        self.texts: list[str] = []
        self.calls = 0
        self._fail_on_call = fail_on_call
        self._failure = failure

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == self._fail_on_call and self._failure is not None:
            raise self._failure
        self.texts.extend(texts)
        return [[0.5] * 1024 for _ in texts]


class WorkerKilled(BaseException):
    """Stands in for the process dying mid-embed (Lambda timeout, out of memory). It isn't
    an Exception, so the pipeline records nothing, exactly as in a real crash."""


@functools.cache
def long_pdf() -> bytes:
    """A 30-page file that chunks into about ten chunks: several batches of 4."""
    # 14 lines per page keeps every line above the bottom margin, where lines repeated
    # across pages are dropped as footers.
    return make_pdf(
        [
            Page(
                [
                    f"Page {p} line {i}: zero trust moves defenses to users and assets."
                    for i in range(14)
                ]
            )  # noqa: E501
            for p in range(30)
        ]
    )


async def ingest(pool: asyncpg.Pool, key: str, data: bytes, embed: RecordingEmbed) -> IngestResult:
    return await ingest_version(
        pool, key, load_source=lambda: data, embed=embed, embedding_model=MODEL
    )


async def doc_chunks(pool: asyncpg.Pool, parts: S3KeyParts) -> list[asyncpg.Record]:
    """Every chunk of the document, all versions, in order."""
    async with pool.acquire() as conn, rls_transaction(conn, parts.tenant_id, None):
        return await conn.fetch(
            "SELECT id, version, chunk_index, content FROM chunks WHERE doc_id = $1"
            " ORDER BY version, chunk_index",
            parts.doc_id,
        )


async def active_version(pool: asyncpg.Pool, parts: S3KeyParts) -> int | None:
    async with pool.acquire() as conn, rls_transaction(conn, parts.tenant_id, None):
        return await conn.fetchval(
            "SELECT active_version FROM documents WHERE id = $1", parts.doc_id
        )


async def epoch(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> int | None:
    async with pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
        return await conn.fetchval("SELECT epoch FROM tenant_index_state")


async def expire_lease(pool: asyncpg.Pool, parts: S3KeyParts) -> None:
    """What waiting out the 15-minute lease would do."""
    async with pool.acquire() as conn, rls_transaction(conn, parts.tenant_id, None):
        await conn.execute(
            "UPDATE document_versions SET lease_until = now() - interval '1 second'"
            " WHERE doc_id = $1 AND version = $2",
            parts.doc_id,
            parts.version,
        )


# --- 1. Run twice: no new work -------------------------------------------------------------


async def test_running_the_same_job_twice_does_no_new_work(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    data = make_pdf([Page([f"Paragraph {i} about policy engines." for i in range(5)])] * 2)
    parts, key = await registered(pool, tenant, data)
    embed = RecordingEmbed()
    assert (await ingest(pool, key, data, embed)).outcome == "indexed"
    embedded_once = list(embed.texts)
    row_before = dict(await version_row(pool, parts))
    chunks_before = await doc_chunks(pool, parts)

    second = await ingest(pool, key, data, embed)

    assert second.outcome == "skipped"
    assert embed.texts == embedded_once  # nothing embedded again
    assert dict(await version_row(pool, parts)) == row_before  # not even attempts moved
    assert await doc_chunks(pool, parts) == chunks_before  # same rows, same ids
    assert await epoch(pool, tenant) == 1  # cached answers weren't invalidated again


async def test_two_deliveries_at_the_same_time_give_one_set_of_chunks(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    # SQS can hand the same message to two workers at once: the claim lets only one in.
    data = make_pdf([Page([f"Paragraph {i} about policy engines." for i in range(5)])] * 2)
    parts, key = await registered(pool, tenant, data)
    embed = RecordingEmbed()

    results = await asyncio.gather(ingest(pool, key, data, embed), ingest(pool, key, data, embed))

    assert sorted(r.outcome for r in results) == ["indexed", "skipped"]
    chunks = await doc_chunks(pool, parts)
    assert len(chunks) == max(r.chunk_count for r in results)
    assert len(embed.texts) == len(chunks)  # each chunk embedded once
    assert (await version_row(pool, parts))["attempts"] == 1


# --- 2. Crash mid-embed, then rerun: same chunks, no duplicates ------------------------------


@pytest.mark.parametrize("crash", ["process_killed", "transient_error"])
async def test_a_crash_mid_embed_resumes_without_duplicates(
    pool: asyncpg.Pool, tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    """process_killed: nothing is recorded, so the retry must wait for the lease to expire.
    transient_error: the version is marked failed and the next delivery re-claims it."""
    monkeypatch.setattr(pipeline, "EMBED_BATCH_SIZE", 4)
    data = long_pdf()

    # A clean run of the same file as another document: what the resumed run must match.
    clean_parts, clean_key = await registered(pool, tenant, data)
    clean = await ingest(pool, clean_key, data, RecordingEmbed())
    clean_contents = [c["content"] for c in await doc_chunks(pool, clean_parts)]
    assert clean.chunk_count >= 9  # at least three batches

    parts, key = await registered(pool, tenant, data)
    if crash == "process_killed":
        failure: BaseException = WorkerKilled()
    else:
        failure = RuntimeError("connection reset by Bedrock")
    with pytest.raises(type(failure)):
        await ingest(pool, key, data, RecordingEmbed(fail_on_call=2, failure=failure))

    # The first batch was written before the second was embedded, so it survived.
    assert [c["chunk_index"] for c in await doc_chunks(pool, parts)] == [0, 1, 2, 3]
    row = await version_row(pool, parts)
    if crash == "process_killed":
        assert (row["status"], row["last_error"]) == ("embedding", None)
        # Until the lease runs out, a redelivery leaves the version to the "live" worker.
        assert (await ingest(pool, key, data, RecordingEmbed())).outcome == "skipped"
        await expire_lease(pool, parts)
    else:
        assert row["status"] == "failed"
        assert row["last_error"].startswith("transient: RuntimeError")

    retry = RecordingEmbed()
    result = await ingest(pool, key, data, retry)

    assert result.outcome == "indexed"
    assert result.chunk_count == clean.chunk_count
    assert retry.texts == clean_contents[4:]  # only the missing chunks were embedded
    chunks = await doc_chunks(pool, parts)
    assert [c["chunk_index"] for c in chunks] == list(range(clean.chunk_count))  # no dupes
    assert [c["content"] for c in chunks] == clean_contents
    row = await version_row(pool, parts)
    assert (row["status"], row["attempts"], row["last_error"]) == ("indexed", 2, None)


async def test_a_resume_after_a_parser_change_starts_the_chunks_over(
    pool: asyncpg.Pool, tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored chunks are reused by chunk_index only if the same parser made them: a new
    parser can change what chunk N contains (DECISIONS 2026-11-24, "Step 19")."""
    monkeypatch.setattr(pipeline, "EMBED_BATCH_SIZE", 4)
    data = long_pdf()
    parts, key = await registered(pool, tenant, data)

    # The first attempt runs an older parser and dies after one batch.
    monkeypatch.setattr(pipeline, "PARSER_VERSION", "pymupdf-old")
    with pytest.raises(RuntimeError):
        await ingest(pool, key, data, RecordingEmbed(fail_on_call=2, failure=RuntimeError("x")))
    assert [c["chunk_index"] for c in await doc_chunks(pool, parts)] == [0, 1, 2, 3]
    monkeypatch.undo()
    monkeypatch.setattr(pipeline, "EMBED_BATCH_SIZE", 4)

    retry = RecordingEmbed()
    result = await ingest(pool, key, data, retry)

    assert result.outcome == "indexed"
    chunks = await doc_chunks(pool, parts)
    assert retry.texts == [c["content"] for c in chunks]  # every chunk embedded again
    assert [c["chunk_index"] for c in chunks] == list(range(result.chunk_count))
    assert (await version_row(pool, parts))["parser"] == pipeline.PARSER_VERSION


# --- 3. v2 then v1: v2 stays live ----------------------------------------------------------


async def test_v1_finishing_after_v2_never_replaces_it(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    doc_id = uuid.uuid4()
    v1_data = make_pdf([Page(["Version one: the policy engine is optional."])])
    v2_data = make_pdf([Page(["Version two: the policy engine is required."])])
    v1, v1_key = await registered(pool, tenant, v1_data, 1, doc_id=doc_id)
    v2, v2_key = await registered(pool, tenant, v2_data, 2, doc_id=doc_id)

    assert (await ingest(pool, v2_key, v2_data, RecordingEmbed())).outcome == "indexed"
    late = await ingest(pool, v1_key, v1_data, RecordingEmbed())

    assert late.outcome == "superseded"
    assert await active_version(pool, v2) == 2
    assert (await version_row(pool, v1))["status"] == "superseded"
    assert (await version_row(pool, v2))["status"] == "indexed"
    chunks = await doc_chunks(pool, v2)
    assert {c["version"] for c in chunks} == {2}  # v1's chunks were cleaned up
    assert "required" in chunks[0]["content"]
    assert await epoch(pool, tenant) == 1  # only v2's activation invalidated the cache


# --- 4. Wrong hash: failed, never parsed ----------------------------------------------------


async def test_a_file_that_does_not_match_its_hash_fails_without_being_parsed(
    pool: asyncpg.Pool, tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_pdf([Page(["The bytes that actually arrived."])])
    registered_hash = hashlib.sha256(b"the bytes the API registered").hexdigest()
    parts, key = await registered(pool, tenant, data, sha256=registered_hash)
    parsed: list[bytes] = []
    monkeypatch.setattr(pipeline, "parse", lambda pdf: parsed.append(pdf) or [])
    embed = RecordingEmbed()

    with pytest.raises(PermanentError) as exc_info:
        await ingest(pool, key, data, embed)

    assert exc_info.value.code == "sha256_mismatch"
    row = await version_row(pool, parts)
    assert (row["status"], row["attempts"]) == ("failed", 1)
    assert row["last_error"].startswith("sha256_mismatch")
    assert parsed == []  # the hash is checked before parsing
    assert embed.calls == 0
    assert await doc_chunks(pool, parts) == []
    assert await active_version(pool, parts) is None
