"""POST /v1/retrieve against the Neon dev branch, as app_api (step 14's "done when").

Fixed embeddings, no Bedrock: the query's vector is chosen by the test, so every chunk's
dense rank is known, and the expected RRF scores can be computed by hand. Rows are seeded as
app_ingest, the way the worker writes them.
"""

import uuid

import pytest
from botocore.exceptions import ReadTimeoutError

from tests.api.apphelpers import build_app, client
from tests.api.seed import E0, FAR, NEAR, fixed_embedding, rrf, seed_doc

pytestmark = pytest.mark.db


def failing_embedding(query: str) -> list[float]:
    raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-2.amazonaws.com")


async def retrieve(app, body: dict) -> dict:
    async with client(app) as c:
        resp = await c.post("/v1/retrieve", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def app_for(api_pool, tenant, role_level=3, embed=None):
    app = build_app(api_pool, tenant, role_level)
    app.state.embed_query = embed or fixed_embedding(E0)
    return app


def contents(out: dict) -> list[str]:
    return [chunk["content"] for chunk in out["chunks"]]


# --- fusion -------------------------------------------------------------------------------


async def test_rrf_fuses_dense_and_lexical_ranks(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    # Dense ranks: closest first (E0, NEAR, FAR). Lexical: only "telemetry" matches.
    await seed_doc(
        ingest_pool,
        tenant,
        [
            ("alpha section", E0),  # dense 1, no lexical match
            ("beacon section", NEAR),  # dense 2
            ("gamma telemetry section", FAR),  # dense 3, lexical 1
        ],
    )
    out = await retrieve(app_for(api_pool, tenant), {"query": "telemetry"})

    assert out["lexical_weight"] == 1.0
    assert out["degraded"] == []
    assert contents(out) == ["gamma telemetry section", "alpha section", "beacon section"]
    scores = [chunk["rrf_score"] for chunk in out["chunks"]]
    assert scores == pytest.approx([rrf(3, 1), rrf(1, None), rrf(2, None)])


async def test_a_quoted_phrase_doubles_the_lexical_weight(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("alpha", E0), ("gamma telemetry", FAR)])
    out = await retrieve(app_for(api_pool, tenant), {"query": '"telemetry"'})

    assert out["lexical_weight"] == 2.0
    by_content = {c["content"]: c["rrf_score"] for c in out["chunks"]}
    assert by_content["gamma telemetry"] == pytest.approx(rrf(2, 1, weight=2.0))
    assert by_content["alpha"] == pytest.approx(rrf(1, None))


async def test_top_k_limits_the_result(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [(f"chunk {i}", E0) for i in range(5)])
    out = await retrieve(app_for(api_pool, tenant), {"query": "chunk", "top_k": 2})
    assert len(out["chunks"]) == 2


async def test_chunks_carry_their_location(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("located", E0)], version=1)
    (chunk,) = (await retrieve(app_for(api_pool, tenant), {"query": "located"}))["chunks"]
    assert chunk["doc_id"] == str(doc)
    assert (chunk["version"], chunk["page_start"], chunk["page_end"]) == (1, 1, 2)
    assert chunk["section_path"] == "1 > 1.1"
    uuid.UUID(chunk["chunk_id"])


# --- only live versions -----------------------------------------------------------------


async def test_only_the_active_version_of_a_document_is_returned(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    doc = await seed_doc(ingest_pool, tenant, [("old telemetry", E0)], version=1)
    await seed_doc(ingest_pool, tenant, [("new telemetry", NEAR)], version=2, doc_id=doc)

    out = await retrieve(app_for(api_pool, tenant), {"query": "telemetry"})
    assert contents(out) == ["new telemetry"]  # v1's chunk is closer, but no longer live


async def test_a_version_not_yet_activated_is_not_returned(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("pending telemetry", E0)], active=False)
    out = await retrieve(app_for(api_pool, tenant), {"query": "telemetry"})
    assert out["chunks"] == []


async def test_a_deleted_document_is_not_returned(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("deleted telemetry", E0)], deleted=True)
    await seed_doc(ingest_pool, tenant, [("kept telemetry", FAR)])
    out = await retrieve(app_for(api_pool, tenant), {"query": "telemetry"})
    assert contents(out) == ["kept telemetry"]


# --- isolation ----------------------------------------------------------------------------


async def test_nothing_from_another_tenant(api_pool, ingest_pool, new_tenant):
    tenant, other = new_tenant(), new_tenant()
    # The other tenant's chunk is a perfect match on both branches.
    await seed_doc(ingest_pool, other, [("telemetry telemetry", E0)])
    await seed_doc(ingest_pool, tenant, [("unrelated", FAR)])

    out = await retrieve(app_for(api_pool, tenant), {"query": "telemetry"})
    assert contents(out) == ["unrelated"]  # dense recall still finds the caller's own chunk


@pytest.mark.parametrize(
    ("role_level", "visible"),
    [(1, ["public chunk"]), (2, ["public chunk"]), (3, ["restricted chunk", "public chunk"])],
)
async def test_nothing_above_the_callers_role_level(
    api_pool, ingest_pool, new_tenant, role_level, visible
):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("restricted chunk", E0)], min_role_level=3)
    await seed_doc(ingest_pool, tenant, [("public chunk", NEAR)], min_role_level=1)

    # No lexical match, so the order is the dense order alone.
    out = await retrieve(app_for(api_pool, tenant, role_level), {"query": "zebra"})
    assert contents(out) == visible


async def test_a_tenant_id_in_the_body_is_ignored(api_pool, ingest_pool, new_tenant):
    tenant, other = new_tenant(), new_tenant()
    await seed_doc(ingest_pool, other, [("other tenant's telemetry", E0)])
    body = {"query": "telemetry", "tenant_id": str(other), "role_level": 3}
    out = await retrieve(app_for(api_pool, tenant, role_level=1), body)
    assert out["chunks"] == []


# --- embedding fallback -------------------------------------------------------------------


async def test_without_an_embedding_recall_is_lexical_only_and_degraded(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("alpha", E0), ("gamma telemetry", FAR)])
    out = await retrieve(app_for(api_pool, tenant, embed=failing_embedding), {"query": "telemetry"})

    assert out["degraded"] == ["embedding"]
    assert contents(out) == ["gamma telemetry"]  # "alpha" only matched on the dense side
    assert out["chunks"][0]["rrf_score"] == pytest.approx(rrf(None, 1))


async def test_the_fallback_still_respects_role_levels(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("restricted telemetry", E0)], min_role_level=3)
    out = await retrieve(
        app_for(api_pool, tenant, role_level=1, embed=failing_embedding), {"query": "telemetry"}
    )
    assert out == {"chunks": [], "lexical_weight": 1.0, "degraded": ["embedding"]}


# --- spans --------------------------------------------------------------------------------


async def test_a_retrieve_is_one_trace_with_embed_and_sql_recall_spans(
    api_pool, ingest_pool, new_tenant, spans
):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("secret telemetry text", E0)])
    await retrieve(app_for(api_pool, tenant), {"query": "secret telemetry"})

    finished = {span.name: span for span in spans.get_finished_spans()}
    root, embed, sql = finished["retrieve"], finished["embed"], finished["sql_recall"]
    assert embed.parent.span_id == root.context.span_id
    assert sql.parent.span_id == root.context.span_id
    assert sql.attributes["retrieve.mode"] == "hybrid"
    assert sql.attributes["retrieve.chunk_count"] == 1
    # Counts and weights only: neither the query nor any chunk text is in a span.
    for span in finished.values():
        assert not any("secret" in str(value) for value in span.attributes.values())
