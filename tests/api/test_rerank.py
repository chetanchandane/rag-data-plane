"""api/rerank.py: the Cohere adapter against httpx.MockTransport (no network), and
rerank_chunks' fallback to RRF order."""

import asyncio
import json
import uuid

import httpx
import pytest

from api import rerank
from api.breaker import CircuitBreaker
from api.rerank import (
    COHERE_RERANK_URL,
    CohereReranker,
    RerankError,
    cohere_api_key,
    rerank_chunks,
)
from api.retrieval import RetrievedChunk

KEY = "test-cohere-key"


def cohere(handler) -> CohereReranker:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CohereReranker(http, KEY, "rerank-v5.0-fast")


def ok(results, search_units: float = 1.0):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": results, "meta": {"billed_units": {"search_units": search_units}}}
        )

    return handler


def chunk(content: str) -> RetrievedChunk:
    return RetrievedChunk(uuid.uuid4(), uuid.uuid4(), 1, 1, 1, None, content, 0.03)


# --- the Cohere adapter -------------------------------------------------------------------


async def test_sends_cohere_v2_rerank_and_returns_index_score_pairs():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        results = [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.12}]
        return httpx.Response(200, json={"results": results})

    ranked = await cohere(handler).rerank("what is a PEP?", ["doc a", "doc b"], top_n=2)

    assert ranked == [(1, 0.91), (0, 0.12)]
    (request,) = seen
    assert str(request.url) == COHERE_RERANK_URL
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "rerank-v5.0-fast",
        "query": "what is a PEP?",
        "documents": ["doc a", "doc b"],
        "top_n": 2,
    }


@pytest.mark.parametrize(
    ("status", "reason"), [(429, "http_429"), (500, "http_500"), (401, "http_401")]
)
async def test_an_error_status_is_a_rerank_error(status, reason):
    reranker = cohere(lambda request: httpx.Response(status, json={"message": "no"}))
    with pytest.raises(RerankError, match=reason):
        await reranker.rerank("q", ["a"], top_n=1)


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"results": [{"index": 0}]}', b'{"no_results": []}'],
    ids=["not_json", "missing_score", "missing_results"],
)
async def test_an_unexpected_body_is_a_rerank_error(body):
    reranker = cohere(lambda request: httpx.Response(200, content=body))
    with pytest.raises(RerankError, match="malformed_response"):
        await reranker.rerank("q", ["a"], top_n=1)


async def test_an_index_outside_the_documents_is_rejected():
    reranker = cohere(ok([{"index": 5, "relevance_score": 0.9}]))
    with pytest.raises(RerankError, match="index_out_of_range"):
        await reranker.rerank("q", ["a", "b"], top_n=2)


async def test_a_connection_error_is_a_rerank_error():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(RerankError, match="ConnectError"):
        await cohere(refuse).rerank("q", ["a"], top_n=1)


async def test_the_whole_call_has_one_deadline(monkeypatch):
    monkeypatch.setattr(rerank, "RERANK_TIMEOUT_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"results": []})

    with pytest.raises(RerankError, match="timeout"):
        await cohere(slow).rerank("q", ["a"], top_n=1)


async def test_the_key_never_appears_in_the_error():
    reranker = cohere(lambda request: httpx.Response(403, json={"message": f"bad key {KEY}"}))
    with pytest.raises(RerankError) as raised:
        await reranker.rerank("q", ["a"], top_n=1)
    assert KEY not in str(raised.value)


# --- rerank_chunks ------------------------------------------------------------------------


async def test_chunks_come_back_in_rerank_order_with_scores(spans):
    a, b, c = chunk("a"), chunk("b"), chunk("c")
    results = [{"index": 2, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.4}]
    reranked = await rerank_chunks(
        cohere(ok(results)), "q", [a, b, c], top_n=2, breaker=CircuitBreaker("reranker")
    )

    assert [r.chunk for r in reranked.chunks] == [c, a]
    assert [r.rerank_score for r in reranked.chunks] == [0.8, 0.4]
    assert not reranked.degraded and reranked.top_score == 0.8
    (span,) = [s for s in spans.get_finished_spans() if s.name == "rerank"]
    assert span.attributes["rerank.top_score"] == 0.8
    assert span.attributes["rerank.search_units"] == 1.0


async def test_a_failed_rerank_keeps_the_rrf_order_and_says_so(spans):
    chunks = [chunk(str(i)) for i in range(8)]
    failing = cohere(lambda request: httpx.Response(503))
    reranked = await rerank_chunks(
        failing, "q", chunks, top_n=5, breaker=CircuitBreaker("reranker")
    )

    assert [r.chunk for r in reranked.chunks] == chunks[:5]
    assert all(r.rerank_score is None for r in reranked.chunks)
    assert reranked.degraded and reranked.top_score is None
    (span,) = [s for s in spans.get_finished_spans() if s.name == "rerank"]
    assert span.attributes["rerank.error"] == "http_503"


async def test_no_reranker_configured_keeps_the_rrf_order():
    reranked = await rerank_chunks(
        None, "q", [chunk("a")], top_n=5, breaker=CircuitBreaker("reranker")
    )
    assert reranked.degraded and reranked.top_score is None


async def test_nothing_recalled_makes_no_call():
    def fail(request):
        raise AssertionError("called Cohere with no documents")

    reranked = await rerank_chunks(
        cohere(fail), "q", [], top_n=5, breaker=CircuitBreaker("reranker")
    )
    assert reranked.chunks == [] and not reranked.degraded


# --- the key ------------------------------------------------------------------------------


class FakeSsm:
    def __init__(self):
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        return {"Parameter": {"Value": "key-from-ssm"}}


def test_the_key_comes_from_the_environment_or_ssm():
    assert cohere_api_key({"COHERE_API_KEY": "local-key"}) == "local-key"
    ssm = FakeSsm()
    param = "/ragdp/prod/cohere_api_key"
    assert cohere_api_key({"COHERE_API_KEY_PARAM": param}, ssm) == "key-from-ssm"
    assert ssm.calls == [{"Name": param, "WithDecryption": True}]
    assert cohere_api_key({}) is None


def test_both_key_sources_set_is_an_error():
    with pytest.raises(ValueError, match="not both"):
        cohere_api_key({"COHERE_API_KEY": "k", "COHERE_API_KEY_PARAM": "/p"}, FakeSsm())
