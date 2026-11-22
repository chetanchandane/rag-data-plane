"""POST /v1/retrieve against the Neon dev branch, as app_api (step 14's "done when").

Fixed embeddings, no Bedrock: the query's vector is chosen by the test, so every chunk's
dense rank is known, and the expected RRF scores can be computed by hand. Rows are seeded as
app_ingest, the way the worker writes them.
"""

import math
import uuid
from collections.abc import Sequence

import asyncpg
import pytest
from botocore.exceptions import ReadTimeoutError

from shared.db import rls_transaction
from tests.api.apphelpers import build_app, client

pytestmark = pytest.mark.db

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
