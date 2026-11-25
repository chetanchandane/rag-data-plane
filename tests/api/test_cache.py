"""The /v1/query cache against the Neon dev branch, as app_api (step 22; SPEC: Query path,
"Cache").

Recall is real; the embedder, reranker and LLM are fakes that count their calls, so a hit is
visible as a request that called none of them. Test tenants are random, so their cache rows
can't be read by anything else. Nothing deletes them: app_ingest has no grant on query_cache,
and the dev branch's compute empties unlogged tables when it suspends.
"""

import dataclasses

import pytest

from api import cache
from shared.db import BUMP_EPOCH_SQL, rls_transaction
from tests.api.apphelpers import build_app, client
from tests.api.seed import E0, FAR, fixed_embedding, seed_doc
from tests.api.test_query import CitingLlm, KeywordReranker
from worker import store

pytestmark = pytest.mark.db

QUESTION = "What does the telemetry say?"


def app_for(api_pool, tenant, role_level=1, *, reranker=None):
    app = build_app(api_pool, tenant, role_level)
    app.state.embed_query = fixed_embedding(E0)
    app.state.reranker = reranker or KeywordReranker("telemetry")
    app.state.generate = CitingLlm()
    return app


async def ask(app, query: str = QUESTION) -> dict:
    async with client(app) as c:
        resp = await c.post("/v1/query", json={"query": query})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def cache_rows(api_pool, tenant, role_level) -> list:
    """The cache entries this tenant and role level can see (RLS: exactly theirs)."""
    async with api_pool.acquire() as conn, rls_transaction(conn, tenant, role_level):
        return await conn.fetch("SELECT * FROM query_cache")


def llm_calls(app) -> int:
    return len(app.state.generate.prompts)


async def test_a_repeated_question_is_served_from_the_cache(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = app_for(api_pool, tenant)

    # Whitespace is normalized for the key and for everything after it.
    first = await ask(app, f"  {QUESTION.replace(' ', '   ')} \n")
    second = await ask(app)

    assert first["cached"] is False and second["cached"] is True
    for field in ("answer", "abstained", "abstain_reason", "citations", "top_rerank_score"):
        assert second[field] == first[field]
    assert second["degraded"] == []
    # Nothing was called for the hit, so nothing was spent.
    assert second["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    assert first["usage"]["input_tokens"] > 0
    assert llm_calls(app) == 1
    assert app.state.embed_query.calls == [QUESTION]  # the normalized text was embedded
    assert f"Question: {QUESTION}" in app.state.generate.prompts[0]
    assert len(app.state.reranker.documents) == 1
    # The hit gets its own trace id; the stored row holds only the answer.
    assert second["trace_id"] != first["trace_id"]
    [row] = await cache_rows(api_pool, tenant, 1)
    assert "trace_id" not in row["response"] and "usage" not in row["response"]
    assert row["role_level"] == 1 and row["tenant_id"] == tenant


async def test_a_different_question_is_a_miss(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = app_for(api_pool, tenant)

    await ask(app)
    other = await ask(app, QUESTION.upper())  # case is part of the key

    assert other["cached"] is False
    assert llm_calls(app) == 2
    assert len(await cache_rows(api_pool, tenant, 1)) == 2


async def test_an_activation_invalidates_cached_answers(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("telemetry, version one", E0)])
    app = app_for(api_pool, tenant)
    assert (await ask(app))["citations"][0]["quote"].startswith("telemetry, version one")
    assert (await ask(app))["cached"] is True

    # Version 2 goes live through the worker's own activation, which bumps the epoch.
    await seed_doc(
        ingest_pool, tenant, [("telemetry, version two", E0)], doc_id=doc, version=2, active=False
    )
    claim = store.Claim(tenant, doc, 2, 1, "0" * 64, 1, None, None)
    async with ingest_pool.acquire() as conn:
        assert await store.activate(conn, claim) == "activated"

    after = await ask(app)
    assert after["cached"] is False
    assert after["citations"][0]["quote"].startswith("telemetry, version two")
    assert llm_calls(app) == 2


async def test_a_deleted_document_never_comes_back_through_the_cache(
    api_pool, ingest_pool, new_tenant
):
    """SPEC: Ingestion, "Deletes": test that a deleted document never shows up in a query,
    including through the cache."""
    tenant = new_tenant()
    doomed = await seed_doc(ingest_pool, tenant, [("telemetry from the doomed file", E0)])
    await seed_doc(ingest_pool, tenant, [("telemetry kept elsewhere", FAR)])
    app = app_for(api_pool, tenant)

    before = await ask(app)
    assert before["citations"][0]["doc_id"] == str(doomed)
    assert (await ask(app))["cached"] is True  # the doomed answer really is in the cache

    async with client(app) as c:
        assert (await c.delete(f"/v1/documents/{doomed}")).status_code == 202
    after = await ask(app)

    assert after["cached"] is False
    assert after["abstained"] is False
    assert all(cit["doc_id"] != str(doomed) for cit in after["citations"])
    assert "doomed" not in app.state.generate.prompts[-1]


async def test_a_role_level_never_reads_another_levels_entry(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    level3, level2 = app_for(api_pool, tenant, 3), app_for(api_pool, tenant, 2)

    assert (await ask(level3))["cached"] is False
    assert (await ask(level2))["cached"] is False  # same question, same epoch: still a miss
    assert llm_calls(level2) == 1

    # Not only the key: RLS hides level 3's row from level 2, even asked for by exact key.
    [entry] = await cache_rows(api_pool, tenant, 3)
    async with api_pool.acquire() as conn, rls_transaction(conn, tenant, 2):
        found = await conn.fetchval(
            "SELECT count(*) FROM query_cache WHERE cache_key = $1", entry["cache_key"]
        )
    assert found == 0


async def test_another_tenant_never_reads_the_entry(api_pool, ingest_pool, new_tenant):
    tenant, other = new_tenant(), new_tenant()
    for t in (tenant, other):
        await seed_doc(ingest_pool, t, [("telemetry shows normal load", E0)])
    await ask(app_for(api_pool, tenant))

    other_app = app_for(api_pool, other)
    assert (await ask(other_app))["cached"] is False
    assert llm_calls(other_app) == 1


async def test_a_degraded_answer_is_never_cached(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = app_for(api_pool, tenant, reranker=KeywordReranker("telemetry", fail=True))

    first = await ask(app)
    assert first["abstained"] is False and first["degraded"] == ["reranker"]
    assert await cache_rows(api_pool, tenant, 1) == []

    app.state.reranker = KeywordReranker("telemetry")  # recovered
    second = await ask(app)
    assert second["cached"] is False and second["degraded"] == []
    assert llm_calls(app) == 2


async def test_an_abstain_is_never_cached(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("unrelated notes", E0)])
    app = app_for(api_pool, tenant)  # the reranker scores "unrelated notes" 0.1

    for _ in range(2):
        out = await ask(app)
        assert out["abstain_reason"] == "low_relevance" and out["cached"] is False
    assert await cache_rows(api_pool, tenant, 1) == []


async def test_an_expired_entry_is_not_served_and_gets_replaced(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = app_for(api_pool, tenant)
    await ask(app)
    async with api_pool.acquire() as conn, rls_transaction(conn, tenant, 1):
        await conn.execute("UPDATE query_cache SET expires_at = now() - interval '1 second'")

    assert (await ask(app))["cached"] is False
    assert llm_calls(app) == 2
    [row] = await cache_rows(api_pool, tenant, 1)  # the same key, refreshed
    async with api_pool.acquire() as conn, rls_transaction(conn, tenant, 1):
        assert await conn.fetchval(
            "SELECT expires_at > now() + interval '23 hours' FROM query_cache"
        )
    assert (await ask(app))["cached"] is True


async def test_a_store_removes_expired_rows(api_pool, ingest_pool, new_tenant):
    """Rows orphaned by an epoch bump are never read again; the next day's stores remove
    them, so the table stays bounded without a compute suspend."""
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = app_for(api_pool, tenant)
    await ask(app)
    async with api_pool.acquire() as conn, rls_transaction(conn, tenant, 1):
        await conn.execute("UPDATE query_cache SET expires_at = now() - interval '1 second'")

    await ask(app, "Is the telemetry normal?")  # another key

    # Only the new entry is left: the expired one went in the same transaction.
    async with api_pool.acquire() as conn, rls_transaction(conn, tenant, 1):
        assert await conn.fetchval("SELECT count(*) FROM query_cache") == 1
        assert await conn.fetchval("SELECT bool_and(expires_at > now()) FROM query_cache")


async def test_with_the_cache_off_nothing_is_read_or_written(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = app_for(api_pool, tenant)
    app.state.settings = dataclasses.replace(app.state.settings, query_cache=False)

    for _ in range(2):
        assert (await ask(app))["cached"] is False
    assert llm_calls(app) == 2
    assert await cache_rows(api_pool, tenant, 1) == []


async def test_an_answer_stored_after_an_epoch_bump_is_never_served(
    api_pool, ingest_pool, new_tenant
):
    """The store must reuse the key computed before recall. Here an epoch bump lands between
    lookup and store: the entry is written under the old epoch, where no later lookup looks."""
    tenant = new_tenant()
    version = cache.answer_version(build_app(api_pool, tenant, 1).state.settings)
    before = await cache.lookup(api_pool, tenant, 1, QUESTION, version)
    # What an activation or a delete does, between this request's lookup and its store.
    async with ingest_pool.acquire() as conn, rls_transaction(conn, tenant, None):
        await conn.execute(BUMP_EPOCH_SQL, tenant)
    await cache.store(api_pool, tenant, 1, before.key, {"answer": "stale"})

    after = await cache.lookup(api_pool, tenant, 1, QUESTION, version)
    assert (before.epoch, after.epoch) == (0, 1)
    assert after.response is None
