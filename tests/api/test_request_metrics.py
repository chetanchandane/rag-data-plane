"""api/request_metrics.py: the EMF line each request emits (step 25). Recall, the answer and
the cache are fakes, so no database, no model. Each line is checked against the EMF spec
(tests/emf.py)."""

import uuid

import asyncpg
import pytest

import api.query as query_route
import api.retrieve as retrieve_route
from api import cache
from api.answering import QueryAnswer
from api.retrieval import RetrievalResult
from shared import metrics
from tests.api.apphelpers import build_app, client, make_settings
from tests.emf import dimensions, emitted, metric_names

TENANT = uuid.UUID("7d1e0000-0000-4000-8000-000000000025")


class UntouchablePool:
    def acquire(self):
        raise AssertionError("the request reached the database")


@pytest.fixture(autouse=True)
def on_lambda(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-api")


def app():
    return build_app(UntouchablePool(), TENANT, 2)


def recalled(degraded: list[str]):
    async def fake(*args, **kwargs) -> RetrievalResult:
        return RetrievalResult(chunks=[], lexical_weight=1.0, degraded=degraded)

    return fake


def raising(exc: BaseException):
    async def fake(*args, **kwargs):
        raise exc

    return fake


def answered(degraded: list[str]):
    async def fake(*args, **kwargs) -> QueryAnswer:
        return QueryAnswer(
            answer="An answer.",
            abstain_reason=None,
            citations=[],
            degraded=degraded,
            top_rerank_score=0.9,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0001,
        )

    return fake


async def test_retrieve_200_emits_latency_and_no_fault(monkeypatch, capsys):
    monkeypatch.setattr(retrieve_route, "retrieve", recalled(["embedding"]))
    async with client(app()) as c:
        resp = await c.post("/v1/retrieve", json={"query": "monitoring plan"})
    assert resp.status_code == 200
    [rec] = emitted(capsys)
    assert dimensions(rec) == [["Route"]]
    assert rec["Route"] == "retrieve"
    # Degraded is a metric only for /v1/query (8 metrics in all); here it's a property.
    assert metric_names(rec) == {"Latency", "Fault"}
    assert rec["Fault"] == 0
    assert rec["Latency"] > 0
    assert rec["degraded"] == ["embedding"]
    assert rec["status"] == 200
    assert rec["route_template"] == "/v1/retrieve"


async def test_a_503_is_a_fault_with_no_latency(monkeypatch, capsys):
    monkeypatch.setattr(retrieve_route, "retrieve", raising(asyncpg.QueryCanceledError("slow")))
    async with client(app()) as c:
        resp = await c.post("/v1/retrieve", json={"query": "monitoring plan"})
    assert resp.status_code == 503
    [rec] = emitted(capsys)
    assert metric_names(rec) == {"Fault"}  # latency counts only 2xx
    assert rec["Fault"] == 1
    assert rec["status"] == 503


async def test_an_unhandled_exception_is_counted_as_a_500(monkeypatch, capsys):
    monkeypatch.setattr(retrieve_route, "retrieve", raising(RuntimeError("bug")))
    async with client(app()) as c:
        with pytest.raises(RuntimeError):  # the test transport re-raises app errors
            await c.post("/v1/retrieve", json={"query": "monitoring plan"})
    [rec] = emitted(capsys)
    assert rec["Fault"] == 1
    assert rec["status"] == 500


async def test_a_4xx_is_not_a_fault_and_has_no_latency(capsys):
    async with client(app()) as c:
        resp = await c.post("/v1/retrieve", json={"query": ""})
    assert resp.status_code == 422
    [rec] = emitted(capsys)
    assert metric_names(rec) == {"Fault"}
    assert rec["Fault"] == 0


async def test_other_routes_emit_only_fault(capsys):
    async with client(app()) as c:
        resp = await c.get("/v1/whoami")
        missing = await c.get("/v1/nothing-here")
    assert (resp.status_code, missing.status_code) == (200, 404)
    whoami, unknown = emitted(capsys)
    assert whoami["Route"] == unknown["Route"] == "other"
    assert metric_names(whoami) == metric_names(unknown) == {"Fault"}
    assert whoami["route_template"] == "/v1/whoami"
    assert unknown["route_template"] is None  # no route matched: never the raw path


async def test_a_query_miss_emits_degraded_and_no_cache_hit(monkeypatch, capsys):
    a = app()
    a.state.settings = make_settings(query_cache=False)
    monkeypatch.setattr(query_route, "retrieve", recalled([]))
    monkeypatch.setattr(query_route, "answer_query", answered(["reranker"]))
    async with client(a) as c:
        resp = await c.post("/v1/query", json={"query": "What is a monitoring plan?"})
    assert resp.status_code == 200
    [rec] = emitted(capsys)
    assert rec["Route"] == "query"
    assert metric_names(rec) == {"Latency", "Fault", "Degraded", "CacheHit"}
    assert (rec["Degraded"], rec["CacheHit"], rec["Fault"]) == (1, 0, 0)
    assert rec["degraded"] == ["reranker"]
    assert rec["cached"] is False


async def test_a_query_cache_hit_emits_a_hit_and_not_degraded(monkeypatch, capsys):
    stored = {
        "answer": "An answer.",
        "abstained": False,
        "abstain_reason": None,
        "citations": [],
        "degraded": [],
        "top_rerank_score": 0.9,
    }

    async def hit(*args, **kwargs) -> cache.Lookup:
        return cache.Lookup(key="k", epoch=1, response=stored)

    monkeypatch.setattr(cache, "lookup", hit)
    monkeypatch.setattr(query_route, "retrieve", raising(AssertionError("a hit recalls nothing")))
    async with client(app()) as c:
        resp = await c.post("/v1/query", json={"query": "What is a monitoring plan?"})
    assert resp.status_code == 200 and resp.json()["cached"] is True
    [rec] = emitted(capsys)
    # A hit counts toward the hit rate only: its milliseconds would hide slow answers in
    # Latency's p95, and it can't be degraded.
    assert metric_names(rec) == {"Fault", "CacheHit"}
    assert rec["CacheHit"] == 1
    assert rec["cached"] is True


async def test_a_metric_that_cannot_be_built_never_fails_the_request(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise ValueError("names used twice")

    monkeypatch.setattr(metrics, "record", broken)
    async with client(app()) as c:
        resp = await c.get("/v1/whoami")
    assert resp.status_code == 200
    assert emitted(capsys) == []


async def test_no_line_is_printed_off_lambda(monkeypatch, capsys):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME")
    async with client(app()) as c:
        await c.get("/v1/whoami")
    assert emitted(capsys) == []
