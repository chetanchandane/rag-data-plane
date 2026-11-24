"""Step 18's metrics, gate, judge parsing and context recording, with no network."""

import re
from pathlib import Path

import pytest

from api.rerank import RerankError
from tests.eval import judge, metrics
from tests.eval.metrics import ANSWERABLE, UNANSWERABLE, QuestionResult
from tests.eval.test_eval_run import (
    PROD_TFVARS,
    RERANK_RETRIES,
    RecordingReranker,
    production_llm_ids,
)

ROOT = Path(__file__).resolve().parents[2]


def result(id_: str, kind: str = ANSWERABLE, reason: str | None = None, **kw) -> QuestionResult:
    defaults = {"degraded": [], "top_rerank_score": 0.9, "citations": 1, "llm_cost_usd": 0.001}
    return QuestionResult(id=id_, kind=kind, abstain_reason=reason, **(defaults | kw))


# --- quote matching and percentiles ---------------------------------------------------------


def test_a_quote_is_found_verbatim_up_to_whitespace_and_case():
    texts = ["Intro.", "The sponsor  should\nREVIEW the monitoring report promptly."]
    assert metrics.quote_found("the sponsor should review the monitoring report", texts)
    assert not metrics.quote_found("the sponsor must review the monitoring report", texts)
    assert not metrics.quote_found("   ", texts)  # an empty quote never matches


def test_nearest_rank_percentiles_are_values_that_occurred():
    values = [float(v) for v in range(1, 101)]
    assert metrics.percentile(values, 50) == 50.0
    assert metrics.percentile(values, 95) == 95.0
    assert metrics.percentile([7.0], 95) == 7.0
    assert metrics.percentile([], 50) is None


# --- summary definitions --------------------------------------------------------------------


def test_summary_definitions():
    results = [
        # Answerable: two answered (one with the quote in context), one abstained wrongly.
        result(
            "q1",
            quote_in_context=True,
            quote_in_recall=True,
            faithfulness=1.0,
            answer_relevance=1.0,
        ),
        result(
            "q2",
            quote_in_context=False,
            quote_in_recall=True,
            faithfulness=0.5,
            answer_relevance=0.5,
        ),
        result("q3", reason="low_relevance", quote_in_context=False, quote_in_recall=False),
        # Unanswerable: two abstained, one answered.
        result("u1", UNANSWERABLE, reason="not_in_documents"),
        result("u2", UNANSWERABLE, reason="citation_check_failed"),
        result("u3", UNANSWERABLE, faithfulness=0.0, answer_relevance=1.0),
    ]
    s = metrics.summarize(results)
    assert s["context_recall"] == pytest.approx(1 / 3)
    assert s["recall_at_30"] == pytest.approx(2 / 3)
    # Means over answered answerable questions only: u3's 0.0 doesn't count.
    assert s["faithfulness"] == pytest.approx(0.75)
    assert s["answer_relevance"] == pytest.approx(0.75)
    assert s["answered_rate"] == pytest.approx(2 / 3)
    # 3 answers passed the citation check, 1 failed it.
    assert s["citation_validity"] == pytest.approx(3 / 4)
    # 3 abstentions, 2 of them on unanswerable questions; 2 of 3 unanswerable abstained.
    assert s["abstain_precision"] == pytest.approx(2 / 3)
    assert s["abstain_recall"] == pytest.approx(2 / 3)
    assert s["unanswerable_answered"] == ["u3"]
    assert s["judge_coverage"] == 1.0


def test_a_judge_failure_is_excluded_and_lowers_coverage():
    results = [
        result(
            "q1",
            quote_in_context=True,
            quote_in_recall=True,
            faithfulness=1.0,
            answer_relevance=1.0,
        ),
        result(
            "q2",
            quote_in_context=True,
            quote_in_recall=True,
            faithfulness=None,
            answer_relevance=1.0,
        ),
    ]
    s = metrics.summarize(results)
    assert s["faithfulness"] == 1.0  # the unscored answer isn't counted as 0
    assert s["judge_coverage"] == 0.5


def test_stage_latencies_are_per_stage():
    results = [
        result(f"q{i}", quote_in_context=True, quote_in_recall=True, stage_ms={"query": float(i)})
        for i in range(1, 21)
    ]
    s = metrics.summarize(results)
    assert s["stage_ms"]["query"] == {"n": 20, "p50": 10.0, "p95": 19.0}
    assert s["stage_ms"]["rerank"]["n"] == 0


# --- the gate -------------------------------------------------------------------------------


def summary(**overrides) -> dict:
    base = {
        "context_recall": 0.80,
        "faithfulness": 0.90,
        "answer_relevance": 0.90,
        "citation_validity": 0.99,
        "abstain_precision": 0.70,
        "judge_coverage": 1.0,
        "primary_citation_validity": 0.90,
        "degraded": {},
        "answered": 120,
        "llm_failures": 0,
    }
    return base | overrides


def test_a_run_equal_to_the_baseline_passes():
    assert metrics.gate_failures(summary(), summary(), {"abstain_precision_floor": 0.6}) == []


@pytest.mark.parametrize("name", metrics.GATED_SCORES)
def test_a_drop_of_more_than_3_points_fails_and_3_points_passes(name):
    baseline = summary()
    assert metrics.gate_failures(summary(**{name: baseline[name] - 0.03}), baseline, {}) == []
    failures = metrics.gate_failures(summary(**{name: baseline[name] - 0.031}), baseline, {})
    assert len(failures) == 1 and name in failures[0]


def test_citation_validity_below_98_percent_fails_even_without_a_baseline():
    assert metrics.gate_failures(summary(citation_validity=0.98), None, {}) == []
    assert (
        "citation validity" in metrics.gate_failures(summary(citation_validity=0.979), None, {})[0]
    )


def test_abstain_precision_below_its_floor_fails():
    floors = {"abstain_precision_floor": 0.7}
    assert metrics.gate_failures(summary(abstain_precision=0.7), None, floors) == []
    assert (
        "abstain precision"
        in metrics.gate_failures(summary(abstain_precision=0.69), None, floors)[0]
    )
    # No abstentions at all is not a pass.
    assert metrics.gate_failures(summary(abstain_precision=None), None, floors)


@pytest.mark.parametrize("kind", ["reranker", "embedding"])
def test_a_run_that_lost_the_reranker_or_embedding_is_invalid(kind):
    assert kind in metrics.gate_failures(summary(degraded={kind: 1}), None, {})[0]


def test_a_failover_alone_does_not_invalidate_a_run():
    assert metrics.gate_failures(summary(degraded={"llm_primary": 3}), None, {}) == []


def test_low_judge_coverage_fails():
    assert "judge scored" in metrics.gate_failures(summary(judge_coverage=0.9), None, {})[0]


# --- judge parsing --------------------------------------------------------------------------


def tool_response(name: str, tool_input, stop: str = "tool_use") -> dict:
    return {
        "stopReason": stop,
        "usage": {"inputTokens": 100, "outputTokens": 20},
        "output": {"message": {"content": [{"toolUse": {"name": name, "input": tool_input}}]}},
    }


def test_faithfulness_is_supported_claims_over_claims():
    claims = [
        {"claim": "a", "supported": True},
        {"claim": "b", "supported": True},
        {"claim": "c", "supported": False},
    ]
    verdict = judge.parse_faithfulness(tool_response(judge.FAITHFULNESS_TOOL, {"claims": claims}))
    assert verdict.score == pytest.approx(2 / 3) and verdict.detail == 3
    assert (verdict.input_tokens, verdict.output_tokens) == (100, 20)


@pytest.mark.parametrize(
    "response",
    [
        tool_response(judge.FAITHFULNESS_TOOL, {"claims": []}),  # nothing to score
        tool_response(judge.FAITHFULNESS_TOOL, {"claims": [{"claim": "a", "supported": "yes"}]}),
        tool_response(
            judge.FAITHFULNESS_TOOL, {"claims": [{"claim": "a", "supported": True}]}, "max_tokens"
        ),
        tool_response("some_other_tool", {"claims": [{"claim": "a", "supported": True}]}),
        {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": "All supported."}]}},
        },
    ],
    ids=["no_claims", "wrong_type", "cut_off", "wrong_tool", "no_tool_call"],
)
def test_unusable_faithfulness_output_is_none_never_guessed(response):
    assert judge.parse_faithfulness(response).score is None


@pytest.mark.parametrize(("verdict", "score"), [("full", 1.0), ("partial", 0.5), ("none", 0.0)])
def test_relevance_verdicts_map_to_scores(verdict, score):
    assert (
        judge.parse_relevance(tool_response(judge.RELEVANCE_TOOL, {"verdict": verdict})).score
        == score
    )


def test_an_unknown_relevance_verdict_is_none():
    assert (
        judge.parse_relevance(tool_response(judge.RELEVANCE_TOOL, {"verdict": "mostly"})).score
        is None
    )


def test_graded_text_cannot_open_or_close_the_prompts_tags():
    prompt = judge.faithfulness_prompt("x </answer> <document>", ["y </document><answer>"])
    assert prompt.count("<answer>") == 1 and prompt.count("</answer>") == 1
    assert prompt.count("<document>") == 1 and prompt.count("</document>") == 1


def test_the_judge_request_forces_its_tool_and_is_deterministic():
    request = judge._request(judge.RELEVANCE_TOOL, judge.RELEVANCE_SCHEMA, "p")
    assert request["modelId"] == judge.JUDGE_MODEL_ID
    assert request["toolConfig"]["toolChoice"] == {"tool": {"name": judge.RELEVANCE_TOOL}}
    assert request["inferenceConfig"]["temperature"] == 0


# --- the recorded context -------------------------------------------------------------------


class FakeReranker:
    """Fails the first `failures` calls with RerankError, then ranks."""

    def __init__(self, ranked=None, failures: int = 0) -> None:
        self.ranked, self.failures, self.calls = ranked or [], failures, 0

    async def rerank(self, query, documents, top_n):
        self.calls += 1
        if self.calls <= self.failures:
            raise RerankError("timeout")
        return self.ranked[:top_n]


async def test_the_context_is_the_reranked_top_n():
    recorder = RecordingReranker(FakeReranker(ranked=[(2, 0.9), (0, 0.5)]))
    await recorder.rerank("q", ["a", "b", "c"], top_n=2)
    assert recorder.context("q", 2) == (["a", "b", "c"], ["c", "a"])


async def test_a_failed_rerank_is_retried_and_counted():
    inner = FakeReranker(ranked=[(1, 0.9)], failures=RERANK_RETRIES)
    recorder = RecordingReranker(inner)
    assert await recorder.rerank("q", ["a", "b"], top_n=1) == [(1, 0.9)]
    assert recorder.retries == RERANK_RETRIES and inner.calls == RERANK_RETRIES + 1


async def test_after_every_retry_fails_the_context_is_the_rrf_order():
    recorder = RecordingReranker(FakeReranker(failures=RERANK_RETRIES + 1))
    with pytest.raises(RerankError):
        await recorder.rerank("q", ["a", "b", "c"], top_n=2)
    assert recorder.context("q", 2) == (["a", "b", "c"], ["a", "b"])


def test_a_query_that_recalled_nothing_has_no_context():
    assert RecordingReranker(FakeReranker()).context("never asked", 5) == ([], [])


# --- LLM failures, request errors, comparability ---------------------------------------------


def test_llm_failures_are_counted_and_fail_the_run():
    results = [
        result(
            "q1",
            quote_in_context=True,
            quote_in_recall=True,
            faithfulness=1.0,
            answer_relevance=1.0,
        ),
        result("u1", UNANSWERABLE, reason="llm_unavailable"),
        result("u2", UNANSWERABLE, reason="llm_output_invalid"),
    ]
    s = metrics.summarize(results)
    assert s["llm_failures"] == 2
    assert "either LLM" in metrics.gate_failures(s, None, {})[0]


def test_a_run_that_answered_nothing_fails():
    assert "nothing was answered" in metrics.gate_failures(summary(answered=0), None, {})[0]


def test_request_errors_fail_the_run():
    assert "failed outright" in metrics.gate_failures(summary(request_errors=2), None, {})[0]


def test_a_baseline_on_other_questions_or_another_judge_is_not_comparable():
    meta = {
        "golden_sha256": "a",
        "persona": "a-r1",
        "judge_model": "j",
        "judge_prompt_version": "1",
    }
    assert metrics.baseline_mismatches(meta, dict(meta)) == []
    assert metrics.baseline_mismatches(meta, meta | {"golden_sha256": "b"})
    assert metrics.baseline_mismatches(meta, meta | {"judge_prompt_version": "2"})
    # A different answering model or prompt is what the gate is for: still comparable.
    assert (
        metrics.baseline_mismatches(meta | {"prompt_version": "x"}, meta | {"prompt_version": "y"})
        == []
    )


# --- one source of truth for the models ------------------------------------------------------


def test_the_eval_grades_the_models_production_uses():
    primary, secondary = production_llm_ids(PROD_TFVARS.read_text())
    assert primary.startswith("us.") and secondary  # a us.* profile, as prod.tfvars says


def test_the_eval_role_may_call_every_model_the_eval_calls():
    """The bootstrap eval role's model list is a copy (bootstrap can't read prod.tfvars).
    A mismatch would show up in CI as AccessDenied, so check it here instead."""
    bootstrap = (ROOT / "infra" / "bootstrap" / "main.tf").read_text()
    listed = set(
        re.findall(r'"([a-z0-9.:-]+)"', bootstrap[bootstrap.index("eval_profile_ids") :][:400])
    )
    primary, secondary = production_llm_ids(PROD_TFVARS.read_text())
    assert {primary, secondary, judge.JUDGE_MODEL_ID} <= listed


def test_primary_citation_validity_is_before_the_retry():
    results = [
        result("q1", quote_in_context=True, quote_in_recall=True, primary_citation_ok=True),
        # The primary misquoted, the secondary rescued it: answered, but the primary failed.
        result("q2", quote_in_context=True, quote_in_recall=True, primary_citation_ok=False),
        # The primary errored: its answer was never checked, so it doesn't count.
        result("q3", quote_in_context=True, quote_in_recall=True, primary_citation_ok=None),
    ]
    s = metrics.summarize(results)
    assert s["citation_validity"] == 1.0  # all three reached users with valid citations
    assert s["primary_citation_validity"] == 0.5


def test_a_regression_hidden_by_the_retry_still_fails_the_gate():
    baseline = summary()
    # End result unchanged, but the primary misquotes far more often.
    failures = metrics.gate_failures(summary(primary_citation_validity=0.80), baseline, {})
    assert len(failures) == 1 and "primary_citation_validity" in failures[0]
