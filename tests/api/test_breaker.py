"""Circuit breakers (step 20's "done when"): the state machine with a fake clock, then each
dependency behind its breaker with a faulty fake. No network, no database.

SPEC: 5 failures in 30 s opens a breaker, it half-opens after 30 s, state per instance.
"""

import asyncio
import uuid

import pytest
from botocore.exceptions import ClientError

from api import retrieval
from api.breaker import Breakers, CircuitBreaker
from api.llm import ModelAnswer
from api.rerank import rerank_chunks
from tests.api.apphelpers import build_app, client, make_settings
from tests.api.test_answering import (
    GOOD,
    PEP,
    PRIMARY,
    SECONDARY,
    FakeLlm,
    FakeReranker,
    answered,
    ask,
    cite,
    server_error,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def breaker(clock: Clock) -> CircuitBreaker:
    return CircuitBreaker("test", clock=clock)


def fail(b: CircuitBreaker, times: int = 1) -> None:
    for _ in range(times):
        with b.attempt() as attempt:
            assert attempt.allowed
            attempt.failed()


def succeed(b: CircuitBreaker) -> None:
    with b.attempt() as attempt:
        assert attempt.allowed
        attempt.succeeded()


def opened(clock: Clock) -> CircuitBreaker:
    b = breaker(clock)
    fail(b, 5)
    assert b.state == "open"
    return b


# --- the state machine --------------------------------------------------------------------


def test_five_failures_within_30_seconds_open_it():
    clock = Clock()
    b = breaker(clock)
    for _ in range(4):
        fail(b)
        clock.advance(7)  # the 5th failure lands 28 s after the 1st
    assert b.state == "closed"
    fail(b)
    assert b.state == "open"
    with b.attempt() as attempt:
        assert not attempt.allowed and attempt.state == "open"


def test_failures_older_than_30_seconds_drop_out_of_the_window():
    clock = Clock()
    b = breaker(clock)
    fail(b, 4)
    clock.advance(31)
    fail(b)
    assert b.state == "closed"  # only 1 failure in the last 30 s
    fail(b, 3)
    assert b.state == "closed"  # 4
    fail(b)
    assert b.state == "open"  # 5


def test_a_success_while_closed_does_not_reset_the_count():
    # A dependency failing every other call must still trip it.
    clock = Clock()
    b = breaker(clock)
    for _ in range(4):
        fail(b)
        succeed(b)
    fail(b)
    assert b.state == "open"


def test_open_to_half_open_to_closed():
    clock = Clock()
    b = opened(clock)
    clock.advance(29.9)
    assert b.state == "open"
    clock.advance(0.1)
    assert b.state == "half_open"

    with b.attempt() as trial:
        assert trial.allowed and trial.state == "half_open"
        # While the trial runs, other calls skip the dependency.
        with b.attempt() as other:
            assert not other.allowed and other.state == "half_open"
        trial.succeeded()
    assert b.state == "closed"

    # Closing clears the window: it takes 5 new failures to open again.
    fail(b, 4)
    assert b.state == "closed"


def test_a_failed_trial_reopens_it_for_another_30_seconds():
    clock = Clock()
    b = opened(clock)
    clock.advance(30)
    fail(b)  # the trial
    assert b.state == "open"
    clock.advance(29.9)
    assert b.state == "open"
    clock.advance(0.1)
    assert b.state == "half_open"


def test_a_trial_that_ends_with_no_verdict_is_released():
    # Cancelled, or a bug raised something the caller doesn't classify: without the release
    # every later call would skip the dependency for good.
    clock = Clock()
    b = opened(clock)
    clock.advance(30)
    with pytest.raises(ValueError), b.attempt() as trial:
        assert trial.allowed
        raise ValueError("a bug")
    assert b.state == "half_open"
    with b.attempt() as next_trial:
        assert next_trial.allowed  # the next call is the trial
        next_trial.succeeded()
    assert b.state == "closed"


async def test_a_cancelled_trial_is_released():
    clock = Clock()
    b = opened(clock)
    clock.advance(30)
    started = asyncio.Event()

    async def trial_call() -> None:
        with b.attempt() as attempt:
            assert attempt.allowed
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(trial_call())
    await started.wait()
    assert not b.attempt().allowed  # the trial is running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert b.attempt().allowed


def test_a_late_result_from_before_it_opened_changes_nothing():
    # Calls let through while closed can end after the breaker opened (local concurrency).
    clock = Clock()
    b = breaker(clock)
    late_failures = [b.attempt() for _ in range(5)]
    late_success = b.attempt()
    fail(b, 5)
    clock.advance(10)
    for attempt in late_failures:
        attempt.failed()
    late_success.succeeded()
    assert b.state == "open"
    clock.advance(20)
    assert b.state == "half_open"  # 5 late failures didn't restart the 30 s


def test_a_skipped_call_has_no_outcome_and_an_outcome_is_recorded_once():
    clock = Clock()
    b = opened(clock)
    with pytest.raises(RuntimeError), b.attempt() as skipped:
        skipped.failed()
    closed = breaker(clock)
    with pytest.raises(RuntimeError), closed.attempt() as attempt:
        attempt.failed()
        attempt.failed()


def test_each_dependency_has_its_own_breaker():
    breakers = Breakers()
    names = [breakers.embedding.name, breakers.reranker.name, breakers.llm_primary.name]
    assert names == ["embedding", "reranker", "llm_primary"]
    fail(breakers.reranker, 5)
    assert (breakers.reranker.state, breakers.embedding.state) == ("open", "closed")


# --- the query embedding ------------------------------------------------------------------


class FailingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, query: str) -> list[float]:
        self.calls += 1
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeModel")


async def test_while_open_the_embedding_is_skipped_and_recall_is_lexical_only(spans):
    clock = Clock()
    b = CircuitBreaker("embedding", clock=clock)
    embed = FailingEmbedder()
    for _ in range(5):
        assert await retrieval._embed(embed, "q", b) is None
    assert (embed.calls, b.state) == (5, "open")

    assert await retrieval._embed(embed, "q", b) is None
    assert embed.calls == 5  # not called while open
    last = [s for s in spans.get_finished_spans() if s.name == "embed"][-1]
    assert last.attributes["breaker.state"] == "open"
    assert last.attributes["embed.error"] == "breaker_open"


async def test_an_embedding_bug_does_not_count_as_a_failure():
    b = CircuitBreaker("embedding")

    def broken(query: str) -> list[float]:
        raise ValueError("expected 1024 dimensions, got 256")

    for _ in range(5):
        with pytest.raises(ValueError):
            await retrieval._embed(broken, "q", b)
    assert b.state == "closed"


class FakeRows:
    """Records which SQL ran, without a database."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def is_in_transaction(self) -> bool:
        return False

    def transaction(self):
        return self

    async def execute(self, *args):
        return None

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []


async def test_retrieve_with_the_embedding_breaker_open_flags_degraded_embedding():
    b = CircuitBreaker("embedding")
    embed = FailingEmbedder()
    fail(b, 5)
    pool = FakeRows()
    result = await retrieval.retrieve(
        pool, embed, breaker=b, query="q", top_k=5, tenant_id=uuid.uuid4(), role_level=1
    )
    assert result.degraded == ["embedding"] and embed.calls == 0
    assert pool.sql == [retrieval.LEXICAL_ONLY_SQL]  # the same fallback as after a failure


# --- the reranker -------------------------------------------------------------------------


async def test_while_open_the_reranker_is_skipped_and_rrf_order_kept(spans):
    b = CircuitBreaker("reranker")
    reranker = FakeReranker(fail=True)
    for _ in range(5):
        assert (await rerank_chunks(reranker, "q", [PEP], top_n=5, breaker=b)).degraded
    assert (len(reranker.calls), b.state) == (5, "open")

    reranked = await rerank_chunks(reranker, "q", [PEP], top_n=5, breaker=b)
    assert reranked.degraded and [r.chunk for r in reranked.chunks] == [PEP]
    assert len(reranker.calls) == 5
    last = [s for s in spans.get_finished_spans() if s.name == "rerank"][-1]
    assert last.attributes["breaker.state"] == "open"
    assert last.attributes["rerank.error"] == "breaker_open"


async def test_no_reranker_configured_never_counts():
    b = CircuitBreaker("reranker")
    for _ in range(6):
        await rerank_chunks(None, "q", [PEP], top_n=5, breaker=b)
    assert b.state == "closed"


async def test_the_reranker_trial_closes_the_breaker_when_cohere_is_back():
    clock = Clock()
    b = CircuitBreaker("reranker", clock=clock)
    fail(b, 5)
    clock.advance(30)
    reranker = FakeReranker()
    reranked = await rerank_chunks(reranker, "q", [PEP], top_n=5, breaker=b)
    assert not reranked.degraded and len(reranker.calls) == 1
    assert b.state == "closed"


# --- the primary LLM ----------------------------------------------------------------------


@pytest.mark.parametrize("primary_failure", [server_error(), None], ids=["error", "malformed"])
async def test_while_open_the_primary_is_skipped_and_the_secondary_answers(primary_failure, spans):
    breakers = Breakers()
    llm = FakeLlm(primary=primary_failure)
    for _ in range(5):
        result, _ = await ask(PEP, llm=llm, breakers=breakers)
        assert result.degraded == ["llm_primary"]
    assert breakers.llm_primary.state == "open"
    assert llm.models_called.count(PRIMARY.model_id) == 5

    spans.clear()
    llm.calls.clear()
    result, _ = await ask(PEP, llm=llm, breakers=breakers)
    assert not result.abstained and result.answer == GOOD.answer
    assert result.degraded == ["llm_primary"]  # the same flag as after a failure
    assert llm.models_called == [SECONDARY.model_id]  # the primary isn't called
    primary_span = next(
        s
        for s in spans.get_finished_spans()
        if s.name == "llm_generate" and s.attributes["llm.role"] == "primary"
    )
    assert primary_span.attributes["breaker.state"] == "open"
    assert primary_span.attributes["llm.error"] == "breaker_open"


async def test_with_the_primary_skipped_and_the_secondary_down_it_abstains():
    breakers = Breakers()
    fail(breakers.llm_primary, 5)
    llm = FakeLlm(secondary=server_error())
    result, _ = await ask(PEP, llm=llm, breakers=breakers)
    assert result.abstain_reason == "llm_unavailable"
    assert llm.models_called == [SECONDARY.model_id]


async def test_citation_retries_never_trip_the_primarys_breaker():
    breakers = Breakers()
    misquoted = answered("The PEP does things.", cite(PEP, "a sentence that isn't there"))
    llm = FakeLlm(primary=misquoted)
    for _ in range(10):
        result, _ = await ask(PEP, llm=llm, breakers=breakers)
        assert result.degraded == ["citation_retry"] and not result.abstained
    assert breakers.llm_primary.state == "closed"
    assert llm.models_called.count(PRIMARY.model_id) == 10  # asked every time


async def test_not_answerable_is_a_success_for_the_primarys_breaker():
    clock = Clock()
    breakers = Breakers(llm_primary=CircuitBreaker("llm_primary", clock=clock))
    fail(breakers.llm_primary, 5)
    clock.advance(30)
    unanswerable = ModelAnswer(answerable=False, answer="", citations=[])
    result, llm = await ask(PEP, llm=FakeLlm(primary=unanswerable), breakers=breakers)
    assert result.abstain_reason == "not_in_documents"
    assert llm.models_called == [PRIMARY.model_id]  # the trial
    assert breakers.llm_primary.state == "closed"


async def test_the_primarys_trial_failing_reopens_and_the_secondary_answers():
    clock = Clock()
    breakers = Breakers(llm_primary=CircuitBreaker("llm_primary", clock=clock))
    fail(breakers.llm_primary, 5)
    clock.advance(30)
    result, llm = await ask(PEP, llm=FakeLlm(primary=server_error()), breakers=breakers)
    assert llm.models_called == [PRIMARY.model_id, SECONDARY.model_id]
    assert result.degraded == ["llm_primary"] and not result.abstained
    assert breakers.llm_primary.state == "open"


# --- the app ------------------------------------------------------------------------------


async def test_the_endpoints_share_one_set_of_breakers_per_app():
    # Per dependency, not per endpoint: /v1/retrieve's embedding failures open the breaker
    # that /v1/query uses too. Each app (on Lambda, each instance) has its own.
    app = build_app(FakeRows(), uuid.uuid4(), 1)
    app.state.settings = make_settings(query_cache=False)  # FakeRows only answers recall
    embed = FailingEmbedder()
    app.state.embed_query = embed
    async with client(app) as c:
        for _ in range(5):
            resp = await c.post("/v1/retrieve", json={"query": "q"})
            assert resp.json()["degraded"] == ["embedding"]
        assert app.state.breakers.embedding.state == "open"
        resp = await c.post("/v1/query", json={"query": "q"})
    assert resp.status_code == 200 and "embedding" in resp.json()["degraded"]
    assert embed.calls == 5
    assert build_app(FakeRows(), uuid.uuid4(), 1).state.breakers.embedding.state == "closed"
