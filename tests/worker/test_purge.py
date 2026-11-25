"""Step 21: the delete job, and what a delete does to a version still being ingested.

Real purge_document() and ingest_version() against the Neon dev branch as app_ingest. S3 is
a fake bucket; embeddings are faked. "Deleting" a document here sets deleted_at directly,
as DELETE /v1/documents does (tests/api/test_delete.py covers the endpoint), except in the
lock test, which runs the API's UPDATE as app_api.

DECISIONS 2026-11-24, "Step 21": once deleted_at is set, no new ingest can start; one
already running finishes but never activates, and the purge waits for its lease.
"""

import asyncio
import uuid

import asyncpg
import pytest

from shared.db import create_pool, rls_transaction
from shared.errors import PermanentError
from tests.dbutil import db_url
from tests.worker.dbhelpers import MODEL, fake_embed, registered, version_row
from tests.worker.pdfgen import Page, make_pdf
from worker import purge as purge_module
from worker import store
from worker.pipeline import ingest_version
from worker.purge import purge_document

pytestmark = pytest.mark.db

PDF_V1 = make_pdf([Page([f"Version one, paragraph {i}." for i in range(6)]) for _ in range(2)])
PDF_V2 = make_pdf([Page([f"Version two, paragraph {i}." for i in range(6)]) for _ in range(2)])


class FakeBucket:
    """The raw bucket's objects for one test. fail_on_call=n: the n-th delete raises, as if
    the worker died or S3 was unreachable halfway through."""

    def __init__(self, keys: list[str], fail_on_call: int | None = None) -> None:
        self.objects = set(keys)
        self.calls: list[str] = []
        self.fail_on_call = fail_on_call

    def delete_object(self, key: str) -> None:
        self.calls.append(key)
        if len(self.calls) == self.fail_on_call:
            raise ConnectionError("S3 unreachable")
        self.objects.discard(key)  # deleting a missing key succeeds, as in S3


async def indexed_document(pool: asyncpg.Pool, tenant: uuid.UUID) -> tuple[uuid.UUID, list[str]]:
    """A document with v1 (superseded) and v2 (live), ingested for real. Returns its id and
    both versions' keys."""
    v1, key1 = await registered(pool, tenant, PDF_V1, 1)
    v2, key2 = await registered(pool, tenant, PDF_V2, 2, doc_id=v1.doc_id)
    for key, data in ((key1, PDF_V1), (key2, PDF_V2)):
        result = await ingest_version(
            pool, key, load_source=lambda d=data: d, embed=fake_embed, embedding_model=MODEL
        )
        assert result.outcome == "indexed"
    return v1.doc_id, [key1, key2]


async def mark_deleted(pool: asyncpg.Pool, tenant: uuid.UUID, doc_id: uuid.UUID) -> None:
    async with pool.acquire() as conn, rls_transaction(conn, tenant, None):
        await conn.execute("UPDATE documents SET deleted_at = now() WHERE id = $1", doc_id)


async def counts(pool: asyncpg.Pool, tenant: uuid.UUID, doc_id: uuid.UUID) -> tuple[int, int]:
    """(versions, chunks) stored for the document."""
    async with pool.acquire() as conn, rls_transaction(conn, tenant, None):
        versions = await conn.fetchval(
            "SELECT count(*) FROM document_versions WHERE doc_id = $1", doc_id
        )
        chunks = await conn.fetchval("SELECT count(*) FROM chunks WHERE doc_id = $1", doc_id)
    return versions, chunks


async def doc_row(pool: asyncpg.Pool, tenant: uuid.UUID, doc_id: uuid.UUID) -> asyncpg.Record:
    async with pool.acquire() as conn, rls_transaction(conn, tenant, None):
        return await conn.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id)


async def purge(pool, tenant, doc_id, bucket: FakeBucket):
    return await purge_document(pool, tenant, doc_id, delete_object=bucket.delete_object)


# --- the purge -----------------------------------------------------------------------------


async def test_the_purge_removes_chunks_versions_and_files_and_leaves_a_tombstone(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    versions, chunks = await counts(pool, tenant, doc_id)
    assert versions == 2 and chunks > 0  # v1's chunks were cleaned up at v2's activation
    await mark_deleted(pool, tenant, doc_id)
    bucket = FakeBucket(keys)

    result = await purge(pool, tenant, doc_id, bucket)

    assert (result.objects, result.versions, result.chunks) == (2, 2, chunks)
    assert bucket.objects == set() and sorted(bucket.calls) == sorted(keys)
    assert await counts(pool, tenant, doc_id) == (0, 0)
    tombstone = await doc_row(pool, tenant, doc_id)
    assert tombstone["deleted_at"] is not None
    assert (tombstone["active_version"], tombstone["title"]) == (None, None)
    assert tombstone["external_id"] == f"local:{doc_id}"


async def test_running_the_purge_twice_is_safe(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    await mark_deleted(pool, tenant, doc_id)
    await purge(pool, tenant, doc_id, FakeBucket(keys))

    # A duplicate delivery: the tombstone says "deleted", there's nothing left to remove.
    bucket = FakeBucket([])
    again = await purge(pool, tenant, doc_id, bucket)
    assert (again.objects, again.versions, again.chunks) == (0, 0, 0)
    assert bucket.calls == []


async def test_a_crash_halfway_through_the_s3_deletes_is_finished_by_a_rerun(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    await mark_deleted(pool, tenant, doc_id)
    stored = await counts(pool, tenant, doc_id)
    bucket = FakeBucket(keys, fail_on_call=2)

    with pytest.raises(ConnectionError):
        await purge(pool, tenant, doc_id, bucket)
    # One file gone, the other and every row still there: the rows are the list of keys.
    assert len(bucket.objects) == 1
    assert await counts(pool, tenant, doc_id) == stored

    bucket.fail_on_call = None
    await purge(pool, tenant, doc_id, bucket)
    assert bucket.objects == set()
    assert await counts(pool, tenant, doc_id) == (0, 0)


async def test_a_crash_between_s3_and_the_database_is_finished_by_a_rerun(
    pool: asyncpg.Pool, tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    await mark_deleted(pool, tenant, doc_id)
    stored = await counts(pool, tenant, doc_id)
    bucket = FakeBucket(keys)

    async def connection_lost(*args: object) -> None:
        raise ConnectionError("Neon unreachable")

    with monkeypatch.context() as patch:
        patch.setattr(purge_module.store, "purge_rows", connection_lost)
        with pytest.raises(ConnectionError):
            await purge(pool, tenant, doc_id, bucket)
    assert bucket.objects == set()
    assert await counts(pool, tenant, doc_id) == stored

    # The rerun deletes the (already missing) files again, then the rows.
    result = await purge(pool, tenant, doc_id, bucket)
    assert (result.objects, result.versions) == (2, 2)
    assert await counts(pool, tenant, doc_id) == (0, 0)


async def test_a_document_that_isnt_deleted_is_never_purged(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    stored = await counts(pool, tenant, doc_id)
    bucket = FakeBucket(keys)

    with pytest.raises(PermanentError) as exc_info:
        await purge(pool, tenant, doc_id, bucket)

    assert exc_info.value.code == "not_deleted"
    assert bucket.calls == []
    assert await counts(pool, tenant, doc_id) == stored


async def test_a_job_naming_another_tenant_finds_nothing_and_touches_nothing(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    await mark_deleted(pool, tenant, doc_id)
    stored = await counts(pool, tenant, doc_id)
    bucket = FakeBucket(keys)

    # RLS scopes the purge to the job's tenant, where this document doesn't exist.
    with pytest.raises(PermanentError) as exc_info:
        await purge(pool, uuid.uuid4(), doc_id, bucket)

    assert exc_info.value.code == "no_matching_document"
    assert bucket.calls == []
    assert await counts(pool, tenant, doc_id) == stored


# --- a delete while a version is being ingested --------------------------------------------


async def test_a_deleted_documents_version_is_never_claimed(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    parts, key = await registered(pool, tenant, PDF_V1)
    await mark_deleted(pool, tenant, parts.doc_id)
    removed: list[str] = []

    result = await ingest_version(
        pool,
        key,
        load_source=lambda: pytest.fail("a deleted document's file was read"),
        embed=fake_embed,
        embedding_model=MODEL,
        delete_source=lambda: removed.append(key),
    )

    assert result.outcome == "deleted"
    assert removed == [key]
    row = await version_row(pool, parts)
    assert (row["status"], row["attempts"]) == ("uploaded", 0)


async def test_a_file_uploaded_after_the_purge_is_removed_not_dead_lettered(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    # The presigned URL outlives the delete by up to 15 minutes. The file lands after the
    # purge, so no version row matches its key: without the tombstone this would look like
    # a forged key (no_matching_version, DLQ, alarm) and the file would stay forever.
    parts, key = await registered(pool, tenant, PDF_V1)
    await mark_deleted(pool, tenant, parts.doc_id)
    await purge(pool, tenant, parts.doc_id, FakeBucket([]))
    removed: list[str] = []

    result = await ingest_version(
        pool,
        key,
        load_source=lambda: PDF_V1,
        embed=fake_embed,
        embedding_model=MODEL,
        delete_source=lambda: removed.append(key),
    )

    assert result.outcome == "deleted"
    assert removed == [key]


async def test_an_ingest_running_at_the_delete_never_activates_and_the_purge_waits(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    parts, key = await registered(pool, tenant, PDF_V1)
    loop = asyncio.get_running_loop()
    purge_attempts: list[str] = []

    def load_source() -> bytes:
        # Runs on a worker thread after the claim: the document is deleted mid-ingest, and
        # the delete job arrives while this worker still holds the lease.
        async def delete_and_try_to_purge() -> None:
            await mark_deleted(pool, tenant, parts.doc_id)
            try:
                await purge(pool, tenant, parts.doc_id, FakeBucket([key]))
            except store.PurgeDeferred:
                purge_attempts.append("deferred")

        asyncio.run_coroutine_threadsafe(delete_and_try_to_purge(), loop).result()
        return PDF_V1

    result = await ingest_version(
        pool, key, load_source=load_source, embed=fake_embed, embedding_model=MODEL
    )

    assert purge_attempts == ["deferred"]
    assert result.outcome == "deleted"
    assert (await doc_row(pool, tenant, parts.doc_id))["active_version"] is None
    row = await version_row(pool, parts)
    assert (row["status"], row["last_error"]) == ("failed", "document_deleted")
    assert row["lease_until"] is None  # released

    # The redelivered delete job: no live lease now, so the purge completes.
    bucket = FakeBucket([key])
    done = await purge(pool, tenant, parts.doc_id, bucket)
    assert (done.objects, done.versions) == (1, 1) and done.chunks == result.chunk_count
    assert bucket.objects == set()
    assert await counts(pool, tenant, parts.doc_id) == (0, 0)


async def test_a_claim_waits_for_a_concurrent_delete_and_then_refuses(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    # The claim's FOR SHARE and the API's UPDATE of deleted_at take conflicting row locks,
    # so a claim can't slip in between the delete's start and its commit.
    parts, key = await registered(pool, tenant, PDF_V1)  # min_role_level 2
    api_pool = await create_pool(db_url("DATABASE_URL_API"))
    try:
        async with api_pool.acquire() as api_conn, pool.acquire() as worker_conn:
            async with rls_transaction(api_conn, tenant, 3):
                await api_conn.execute(
                    "UPDATE documents SET deleted_at = now() WHERE id = $1", parts.doc_id
                )
                claiming = asyncio.create_task(store.claim(worker_conn, parts, key))
                await asyncio.sleep(1.0)
                assert not claiming.done()  # blocked on the delete's row lock
            # The delete committed: the claim now sees deleted_at.
            with pytest.raises(store.DocumentDeleted):
                await asyncio.wait_for(claiming, timeout=10)
    finally:
        await api_pool.close()
    row = await version_row(pool, parts)
    assert (row["status"], row["attempts"]) == ("uploaded", 0)


async def test_a_version_claimed_before_the_delete_defers_the_purge(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    parts, key = await registered(pool, tenant, PDF_V1)
    async with pool.acquire() as conn:
        assert await store.claim(conn, parts, key) is not None
    await mark_deleted(pool, tenant, parts.doc_id)

    with pytest.raises(store.PurgeDeferred):
        await purge(pool, tenant, parts.doc_id, FakeBucket([key]))
    assert await counts(pool, tenant, parts.doc_id) == (1, 0)


async def test_a_bad_stored_key_stops_the_purge_before_any_delete(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    doc_id, keys = await indexed_document(pool, tenant)
    async with pool.acquire() as conn, rls_transaction(conn, tenant, None):
        # v2's row names an object of another tenant: deleting it would be irreversible.
        await conn.execute(
            "UPDATE document_versions SET s3_key = $2 WHERE doc_id = $1 AND version = 2",
            doc_id,
            f"raw/{uuid.uuid4()}/{doc_id}/v2/source.pdf",
        )
    await mark_deleted(pool, tenant, doc_id)
    stored = await counts(pool, tenant, doc_id)
    bucket = FakeBucket(keys)

    with pytest.raises(PermanentError) as exc_info:
        await purge(pool, tenant, doc_id, bucket)

    assert exc_info.value.code == "bad_stored_key"
    assert bucket.calls == []  # not even v1's key, which was fine
    assert await counts(pool, tenant, doc_id) == stored
