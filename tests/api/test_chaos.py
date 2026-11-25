"""SPEC: Proof, "Chaos tests": the dependency rows (step 23). The queue and worker rows run on
the deployed stack (docs/runbooks/chaos.md).

One test per row, named after it. Each fault goes through the real failure path rather than
a fake that returns the fallback: the real Cohere client over a transport that can't
connect, a Bedrock-shaped 500 from the primary, and real Postgres cancelling a statement
at the API's 2 s statement_timeout. Recall runs against the Neon dev branch as app_api.
"""

import re

import httpx
import pytest
from botocore.exceptions import ClientError

from api import retrieval
from api.config import RETRY_AFTER_SECONDS
from api.llm import Generation, LlmModel
from api.rerank import CohereReranker
from tests.api.apphelpers import PRIMARY_LLM, RERANK_MODEL_ID, build_app, client
from tests.api.seed import E0, fixed_embedding, seed_doc
from tests.api.test_query import CitingLlm, KeywordReranker

pytestmark = pytest.mark.db

QUESTION = "What does the telemetry say?"


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class PrimaryDown:
    """Bedrock's 500 for the primary; the secondary answers (or fails too, if both_down)."""

    def __init__(self, both_down: bool = False) -> None:
        self.secondary = CitingLlm(fail=both_down)
        self.calls: list[str] = []

    def __call__(self, model: LlmModel, system: str, prompt: str) -> Generation:
        self.calls.append(model.model_id)
        if model.model_id == PRIMARY_LLM:
            error = {
                "Error": {"Code": "InternalServerException", "Message": "internal error"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            }
            raise ClientError(error, "Converse")
        return self.secondary(model, system, prompt)


async def seeded_app(api_pool, ingest_pool, new_tenant, *, reranker, llm=None):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry shows normal load", E0)])
    app = build_app(api_pool, tenant, 1)
    app.state.embed_query = fixed_embedding(E0)
    app.state.reranker = reranker
    if llm is not None:
        app.state.generate = llm
    return app


# --- Reranker unreachable: 200 with degraded ["reranker"] ---------------------------------


async def test_reranker_unreachable(api_pool, ingest_pool, new_tenant):
    # The production client, over a transport where every connection is refused.
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        cohere = CohereReranker(http, "not-a-real-key", RERANK_MODEL_ID)
        app = await seeded_app(api_pool, ingest_pool, new_tenant, reranker=cohere, llm=CitingLlm())
        async with client(app) as c:
            query = await c.post("/v1/query", json={"query": QUESTION})
            retrieve = await c.post("/v1/retrieve", json={"query": QUESTION, "rerank": True})

    assert query.status_code == 200
    out = query.json()
    # Still answered, in RRF order: no rerank score, so the abstain gate is skipped.
    assert out["degraded"] == ["reranker"] and out["abstained"] is False
    assert out["top_rerank_score"] is None and out["cached"] is False
    assert retrieve.status_code == 200 and retrieve.json()["degraded"] == ["reranker"]
    assert [ch["rerank_score"] for ch in retrieve.json()["chunks"]] == [None]


# --- Primary LLM returns 5xx: the secondary answers; both down is an abstain, not a 500 ----


async def test_primary_llm_returns_5xx(api_pool, ingest_pool, new_tenant):
    llm = PrimaryDown()
    app = await seeded_app(
        api_pool, ingest_pool, new_tenant, reranker=KeywordReranker("telemetry"), llm=llm
    )
    async with client(app) as c:
        resp = await c.post("/v1/query", json={"query": QUESTION})

    assert resp.status_code == 200
    out = resp.json()
    assert out["abstained"] is False and out["answer"] == "From the documents."
    assert out["degraded"] == ["llm_primary"]
    assert llm.calls == [PRIMARY_LLM, app.state.settings.llm_secondary_model_id]


async def test_primary_and_secondary_llm_both_down(api_pool, ingest_pool, new_tenant):
    llm = PrimaryDown(both_down=True)
    app = await seeded_app(
        api_pool, ingest_pool, new_tenant, reranker=KeywordReranker("telemetry"), llm=llm
    )
    async with client(app) as c:
        resp = await c.post("/v1/query", json={"query": QUESTION})

    assert resp.status_code == 200
    out = resp.json()
    assert out["abstained"] is True and out["abstain_reason"] == "llm_unavailable"
    assert out["answer"] is None and out["citations"] == []
    assert out["degraded"] == ["llm_primary"]
    assert len(llm.calls) == 2  # never more than two LLM calls


# --- DB statement timeout: 503 with Retry-After --------------------------------------------

# Recall replaced by a statement that outlasts the API's 2 s statement_timeout. It takes the
# same four parameters, so the route calls it exactly as it calls the real SQL.
SLOW_RECALL_SQL = """
SELECT pg_sleep(3)
WHERE $1::vector IS NOT NULL AND $2::text IS NOT NULL
  AND $3::float8 IS NOT NULL AND $4::int IS NOT NULL
"""


@pytest.mark.parametrize(
    "path, body",
    [("/v1/query", {"query": QUESTION}), ("/v1/retrieve", {"query": QUESTION})],
)
async def test_db_statement_timeout(
    api_pool, ingest_pool, new_tenant, monkeypatch, caplog, path, body
):
    monkeypatch.setattr(retrieval, "HYBRID_SQL", SLOW_RECALL_SQL)
    app = await seeded_app(
        api_pool, ingest_pool, new_tenant, reranker=KeywordReranker("telemetry"), llm=CitingLlm()
    )
    async with client(app) as c:
        resp = await c.post(path, json=body)

    # Postgres itself cancelled the statement (QueryCanceledError), and the API said so.
    assert "QueryCanceledError" in caplog.text
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    assert re.search("database unavailable", resp.json()["detail"])
    # Nothing after recall ran: the request stopped at the database.
    assert app.state.generate.prompts == []
