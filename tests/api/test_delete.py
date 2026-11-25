"""DELETE /v1/documents/{id} against the Neon dev branch, as app_api (step 21).

The document must disappear from /v1/retrieve and /v1/query the moment the DELETE commits,
long before the worker purges it. Another tenant's document, or one above the caller's role
level, can't be deleted. SQS is a fake that records the job it would have queued.
"""

import asyncio
import uuid

import pytest

from shared.db import rls_transaction
from shared.jobs import DeleteJob, parse_ingest_job
from tests.api.apphelpers import QUEUE_URL, FakeSqs, build_app, client, registration
from tests.api.seed import E0, FAR, fixed_embedding, seed_doc
from tests.api.test_query import CitingLlm, KeywordReranker

pytestmark = pytest.mark.db


async def doc_row(pool, tenant_id: uuid.UUID, doc_id: uuid.UUID):
    async with pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
        return await conn.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id)


async def epoch(pool, tenant_id: uuid.UUID) -> int:
    async with pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
        return await conn.fetchval("SELECT epoch FROM tenant_index_state") or 0


def queued(app) -> list[DeleteJob]:
    jobs = []
    for call in app.state.sqs.sent:
        assert call["QueueUrl"] == QUEUE_URL
        job = parse_ingest_job(call["MessageBody"])  # what the worker will parse
        assert isinstance(job, DeleteJob)
        jobs.append(job)
    return jobs


async def test_delete_hides_the_document_and_queues_its_purge(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("some text", E0)])
    app = build_app(api_pool, tenant, 1)
    async with client(app) as c:
        resp = await c.delete(f"/v1/documents/{doc}")
        assert resp.status_code == 202
        assert resp.json()["doc_id"] == str(doc)
        assert (await c.get(f"/v1/documents/{doc}")).status_code == 404

    row = await doc_row(ingest_pool, tenant, doc)
    assert row["deleted_at"] is not None
    # Hidden, not yet purged: active_version is the worker's to clear (column grant).
    assert row["active_version"] == 1
    assert await epoch(ingest_pool, tenant) == 1  # bumped in the same transaction
    [job] = queued(app)
    assert (job.tenant_id, job.doc_id, job.version) == (tenant, doc, None)


async def test_a_deleted_document_never_appears_in_retrieve_or_query(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    # The doomed chunk is the better match on both branches.
    doomed = await seed_doc(ingest_pool, tenant, [("doomed telemetry telemetry", E0)])
    await seed_doc(ingest_pool, tenant, [("kept telemetry", FAR)])
    reranker, llm = KeywordReranker("telemetry"), CitingLlm()
    app = build_app(api_pool, tenant, 1)
    app.state.embed_query = fixed_embedding(E0)
    app.state.reranker = reranker
    app.state.generate = llm

    async with client(app) as c:
        before = (await c.post("/v1/retrieve", json={"query": "telemetry"})).json()
        assert [ch["content"] for ch in before["chunks"]][0] == "doomed telemetry telemetry"

        assert (await c.delete(f"/v1/documents/{doomed}")).status_code == 202

        after = (await c.post("/v1/retrieve", json={"query": "telemetry"})).json()
        answer = (await c.post("/v1/query", json={"query": "telemetry?"})).json()

    assert [ch["content"] for ch in after["chunks"]] == ["kept telemetry"]
    assert answer["abstained"] is False
    assert all(cit["doc_id"] != str(doomed) for cit in answer["citations"])
    # Neither the reranker nor the LLM was ever handed the deleted text by /v1/query.
    assert reranker.documents.count("doomed telemetry telemetry") == 0
    assert not any("doomed" in prompt for prompt in llm.prompts)


async def test_repeating_a_delete_queues_the_purge_again_without_a_new_epoch(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("text", E0)])
    app = build_app(api_pool, tenant, 1)
    async with client(app) as c:
        first = await c.delete(f"/v1/documents/{doc}")
        again = await c.delete(f"/v1/documents/{doc}")

    assert (first.status_code, again.status_code) == (202, 202)
    # The time it was hidden, both times: the second request changed nothing.
    assert again.json()["deleted_at"] == first.json()["deleted_at"]
    assert await epoch(ingest_pool, tenant) == 1
    jobs = queued(app)
    assert [j.doc_id for j in jobs] == [doc, doc]
    assert jobs[0].job_id != jobs[1].job_id


async def test_concurrent_deletes_hide_once(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("text", E0)])
    app = build_app(api_pool, tenant, 1)
    async with client(app) as c:
        responses = await asyncio.gather(*(c.delete(f"/v1/documents/{doc}") for _ in range(3)))

    assert [r.status_code for r in responses] == [202, 202, 202]
    assert len({r.json()["deleted_at"] for r in responses}) == 1
    assert await epoch(ingest_pool, tenant) == 1  # one transition, one bump
    assert len(queued(app)) == 3


async def test_another_tenants_document_cant_be_deleted(api_pool, ingest_pool, new_tenant):
    tenant, other = new_tenant(), new_tenant()
    theirs = await seed_doc(ingest_pool, other, [("theirs", E0)])
    app = build_app(api_pool, tenant, 3)
    async with client(app) as c:
        resp = await c.delete(f"/v1/documents/{theirs}")

    assert resp.status_code == 404
    assert (await doc_row(ingest_pool, other, theirs))["deleted_at"] is None
    assert await epoch(ingest_pool, other) == 0
    assert await epoch(ingest_pool, tenant) == 0
    assert app.state.sqs.sent == []


async def test_a_document_above_the_callers_role_level_cant_be_deleted(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    secret = await seed_doc(ingest_pool, tenant, [("level 3", E0)], min_role_level=3)
    app = build_app(api_pool, tenant, 2)
    async with client(app) as c:
        resp = await c.delete(f"/v1/documents/{secret}")
        unknown = await c.delete(f"/v1/documents/{uuid.uuid4()}")

    # The same 404 as a document that doesn't exist: the caller learns nothing.
    assert (resp.status_code, resp.json()) == (unknown.status_code, unknown.json())
    assert resp.status_code == 404
    assert (await doc_row(ingest_pool, tenant, secret))["deleted_at"] is None
    assert app.state.sqs.sent == []


async def test_a_failed_enqueue_is_a_503_and_a_retry_queues_the_purge(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("text", E0)])
    app = build_app(api_pool, tenant, 1)
    app.state.sqs = FakeSqs(fail=True)
    async with client(app) as c:
        failed = await c.delete(f"/v1/documents/{doc}")
        assert failed.status_code == 503
        assert failed.headers["Retry-After"] == "1"
        # Hidden all the same: the commit came first.
        assert (await c.get(f"/v1/documents/{doc}")).status_code == 404

        app.state.sqs = FakeSqs()
        retried = await c.delete(f"/v1/documents/{doc}")

    assert retried.status_code == 202
    assert [j.doc_id for j in queued(app)] == [doc]


async def test_a_deleted_documents_external_id_cant_be_registered_again(
    api_pool, ingest_pool, new_tenant
):
    # The tombstone keeps external_id reserved (DECISIONS 2026-11-24, "Step 21").
    tenant = new_tenant()
    app = build_app(api_pool, tenant, 1)
    async with client(app) as c:
        created = (await c.post("/v1/documents", json=registration(b"%PDF a"))).json()
        assert (await c.delete(f"/v1/documents/{created['doc_id']}")).status_code == 202
        again = await c.post("/v1/documents", json=registration(b"%PDF b"))
    assert (again.status_code, again.json()["detail"]) == (409, "document is deleted")
