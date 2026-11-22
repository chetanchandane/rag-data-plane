"""POST /v1/query and POST /v1/retrieve with rerank, against the Neon dev branch as app_api.

Recall is real (SQL, RLS, fixed embeddings); the reranker and the LLM are fakes that record
what they were given. So these tests show that only the caller's chunks ever reach Cohere or
Bedrock, and that the answer's citations point at them.
"""

import re

import pytest
from botocore.exceptions import ClientError

from api.llm import Generation, LlmModel, ModelAnswer, ModelCitation
from api.rerank import RerankError
from tests.api.apphelpers import build_app, client
from tests.api.seed import E0, FAR, NEAR, fixed_embedding, seed_doc

pytestmark = pytest.mark.db

_DOCUMENT = re.compile(r'<document id="([^"]+)" page="[^"]*">\n(.*?)\n</document>', re.DOTALL)


class KeywordReranker:
    """0.9 for documents containing the keyword, 0.1 otherwise. Records every call."""

    def __init__(self, keyword: str = "", fail: bool = False) -> None:
        self.keyword = keyword
        self.fail = fail
        self.documents: list[str] = []

    async def rerank(self, query, documents, top_n):
        self.documents.extend(documents)
        if self.fail:
            raise RerankError("http_503")
        scores = [0.9 if self.keyword and self.keyword in d else 0.1 for d in documents]
        ranked = sorted(enumerate(scores), key=lambda pair: -pair[1])
        return ranked[:top_n]


class CitingLlm:
    """Answers by quoting the first document in the prompt. Records every prompt."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.prompts: list[str] = []

    def __call__(self, model: LlmModel, system: str, prompt: str) -> Generation:
        self.prompts.append(prompt)
        if self.fail:
            raise ClientError({"Error": {"Code": "ServiceUnavailableException"}}, "Converse")
        chunk_id, content = _DOCUMENT.findall(prompt)[0]
        citation = ModelCitation(chunk_id=chunk_id, quote=content[:24])
        answer = ModelAnswer(answerable=True, answer="From the documents.", citations=[citation])
        return Generation(model.model_id, answer, 1200, 90, "tool_use")


def app_for(api_pool, tenant, role_level=3, *, reranker=None, llm=None):
    app = build_app(api_pool, tenant, role_level)
    app.state.embed_query = fixed_embedding(E0)
    app.state.reranker = reranker if reranker is not None else KeywordReranker()
    if llm is not None:
        app.state.generate = llm
    return app


async def post(app, path: str, body: dict) -> dict:
    async with client(app) as c:
        resp = await c.post(path, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- /v1/query ----------------------------------------------------------------------------


async def test_an_answer_cites_the_callers_own_chunk(api_pool, ingest_pool, new_tenant, spans):
    tenant = new_tenant()
    doc = await seed_doc(
        ingest_pool,
        tenant,
        [("The policy enforcement point enables connections.", FAR), ("Filler text.", E0)],
    )
    llm = CitingLlm()
    out = await post(
        app_for(api_pool, tenant, reranker=KeywordReranker("enforcement"), llm=llm),
        "/v1/query",
        {"query": "What does the policy enforcement point do?"},
    )

    assert out["abstained"] is False and out["abstain_reason"] is None
    assert out["answer"] == "From the documents."
    (citation,) = out["citations"]
    assert citation["doc_id"] == str(doc) and citation["page_start"] == 1
    assert citation["quote"] == "The policy enforcement p"
    assert out["degraded"] == [] and out["top_rerank_score"] == pytest.approx(0.9)
    assert out["usage"]["input_tokens"] == 1200 and out["usage"]["cost_usd"] > 0
    # The reranked chunk is first in the prompt although its embedding is the farthest.
    assert "policy enforcement" in _DOCUMENT.findall(llm.prompts[0])[0][1]
    # The response's trace id is the trace's, so it can be found in Honeycomb.
    root = next(s for s in spans.get_finished_spans() if s.name == "query")
    assert out["trace_id"] == format(root.context.trace_id, "032x")
    names = {s.name for s in spans.get_finished_spans()}
    assert {"retrieve", "embed", "sql_recall", "rerank", "abstain_gate"} <= names
    assert {"llm_generate", "citation_check"} <= names


async def test_another_tenants_chunks_never_reach_the_reranker_or_the_llm(
    api_pool, ingest_pool, new_tenant
):
    tenant, other = new_tenant(), new_tenant()
    # A perfect match on both branches, in the other tenant.
    await seed_doc(ingest_pool, other, [("secret telemetry secret", E0)])
    await seed_doc(ingest_pool, tenant, [("ordinary telemetry notes", FAR)])
    reranker, llm = KeywordReranker("telemetry"), CitingLlm()
    body = {"query": "secret telemetry", "tenant_id": str(other), "role_level": 3}

    out = await post(app_for(api_pool, tenant, 1, reranker=reranker, llm=llm), "/v1/query", body)

    assert reranker.documents == ["ordinary telemetry notes"]
    assert all("secret telemetry secret" not in prompt for prompt in llm.prompts)
    assert out["citations"][0]["quote"] == "ordinary telemetry notes"


async def test_chunks_above_the_callers_role_level_never_reach_the_llm(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("restricted zebra plan", E0)], min_role_level=3)
    await seed_doc(ingest_pool, tenant, [("public zebra notice", NEAR)], min_role_level=1)
    reranker, llm = KeywordReranker("zebra"), CitingLlm()

    await post(
        app_for(api_pool, tenant, 1, reranker=reranker, llm=llm), "/v1/query", {"query": "zebra"}
    )

    assert reranker.documents == ["public zebra notice"]
    assert "restricted" not in llm.prompts[0]


async def test_low_relevance_abstains_without_calling_the_llm(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("nothing relevant here", E0)])
    # KeywordReranker scores 0.1 below the 0.3 threshold; build_app's LLM fails if called.
    out = await post(
        app_for(api_pool, tenant, reranker=KeywordReranker("absent")), "/v1/query", {"query": "q"}
    )
    assert out["abstained"] is True and out["abstain_reason"] == "low_relevance"
    assert out["answer"] is None and out["citations"] == []
    assert out["top_rerank_score"] == pytest.approx(0.1)
    assert out["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


async def test_nothing_visible_abstains_with_no_results(api_pool, new_tenant):
    out = await post(app_for(api_pool, new_tenant()), "/v1/query", {"query": "anything"})
    assert out["abstain_reason"] == "no_results"


async def test_both_llms_down_is_a_200_abstain(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("telemetry", E0)])
    app = app_for(api_pool, tenant, reranker=KeywordReranker("telemetry"), llm=CitingLlm(fail=True))
    out = await post(app, "/v1/query", {"query": "telemetry"})
    assert out["abstain_reason"] == "llm_unavailable"
    assert out["degraded"] == ["llm_primary"]


# --- /v1/retrieve with rerank -------------------------------------------------------------


async def test_retrieve_reranks_only_when_asked(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("alpha", E0), ("beta", NEAR), ("gamma target", FAR)])
    reranker = KeywordReranker("target")
    app = app_for(api_pool, tenant, reranker=reranker)

    plain = await post(app, "/v1/retrieve", {"query": "alpha"})
    assert reranker.documents == []  # off by default: no Cohere call, no cost
    assert [c["rerank_score"] for c in plain["chunks"]] == [None, None, None]

    reranked = await post(app, "/v1/retrieve", {"query": "alpha", "rerank": True, "top_k": 2})
    assert [c["content"] for c in reranked["chunks"]][0] == "gamma target"
    assert len(reranked["chunks"]) == 2
    assert reranked["chunks"][0]["rerank_score"] == pytest.approx(0.9)
    assert reranked["degraded"] == []


async def test_retrieve_with_a_failed_rerank_keeps_rrf_order(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    await seed_doc(ingest_pool, tenant, [("alpha", E0), ("beta", NEAR)])
    app = app_for(api_pool, tenant, reranker=KeywordReranker(fail=True))
    out = await post(app, "/v1/retrieve", {"query": "zzz", "rerank": True})
    assert [c["content"] for c in out["chunks"]] == ["alpha", "beta"]
    assert out["degraded"] == ["reranker"]


async def test_reranked_retrieve_still_sees_only_the_callers_chunks(
    api_pool, ingest_pool, new_tenant
):
    tenant, other = new_tenant(), new_tenant()
    await seed_doc(ingest_pool, other, [("target", E0)])
    reranker = KeywordReranker("target")
    out = await post(
        app_for(api_pool, tenant, reranker=reranker),
        "/v1/retrieve",
        {"query": "target", "rerank": True, "tenant_id": str(other)},
    )
    assert out["chunks"] == [] and reranker.documents == []
