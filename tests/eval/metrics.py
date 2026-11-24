"""Step 18's metrics and quality gate, as pure functions (DECISIONS 2026-11-24, "Step 18
eval baseline"; SPEC: Proof, "Quality gate").

Each question's outcome is a QuestionResult; summarize() turns them into the numbers that
go in eval/results/*.json, and gate_failures() compares a run with the stored baseline.
"""

import math
from collections import Counter
from dataclasses import dataclass, field
from statistics import fmean

from api.answering import CITATION_CHECK_FAILED, LLM_OUTPUT_INVALID, LLM_UNAVAILABLE, normalize

ANSWERABLE, UNANSWERABLE = "answerable", "unanswerable"

# SPEC's gate. Scores are 0 to 1, so "3 points" is 0.03.
MAX_DROP = 0.03
CITATION_VALIDITY_MIN = 0.98
JUDGE_COVERAGE_MIN = 0.95  # below this, faithfulness and relevance aren't trustworthy
# Gated against the baseline. primary_citation_validity is the primary model's own quote
# accuracy, before the citation retry: the retry hides a regression from the end result,
# so this is what catches a worse prompt or model (SPEC: "a rising rate means the prompt or
# model regressed").
GATED_SCORES = ("context_recall", "faithfulness", "answer_relevance", "primary_citation_validity")
# A request that lost these measured another system (lexical-only recall, RRF order).
INVALIDATING_DEGRADATIONS = ("embedding", "reranker")
# Abstains that mean no model gave an answer: an outage or a permissions error, not a
# decision. Counted as correct abstains they would inflate abstain precision and recall.
LLM_FAILURES = (LLM_UNAVAILABLE, LLM_OUTPUT_INVALID)
# What must match the baseline for a point-for-point comparison. Models, prompts and
# settings may differ: measuring such a change is the point of the gate.
COMPARABLE_META = ("golden_sha256", "persona", "judge_model", "judge_prompt_version")

STAGES = (
    "query",
    "embed",
    "sql_recall",
    "rerank",
    "abstain_gate",
    "llm_generate",
    "citation_check",
)


@dataclass
class QuestionResult:
    id: str
    kind: str  # ANSWERABLE or UNANSWERABLE
    abstain_reason: str | None
    degraded: list[str]
    top_rerank_score: float | None
    citations: int
    llm_cost_usd: float
    quote_in_context: bool | None = None  # answerable only: in the 5 chunks the model saw
    quote_in_recall: bool | None = None  # answerable only: in the 30 recalled candidates
    faithfulness: float | None = None  # answered only; None if the judge failed
    claims: int = 0
    answer_relevance: float | None = None  # answered only; None if the judge failed
    # The primary's answer passed the citation check: None if it gave no checked answer
    # (an error, unusable output, or "not answerable").
    primary_citation_ok: bool | None = None
    stage_ms: dict[str, float] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.abstain_reason is None


def quote_found(quote: str, texts: list[str]) -> bool:
    """The golden quote inside one of the texts, normalized as the citation check does
    (whitespace and case only), so "found" means verbatim."""
    needle = normalize(quote)
    return bool(needle) and any(needle in normalize(text) for text in texts)


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100): a value that actually occurred."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return fmean(present) if present else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(results: list[QuestionResult]) -> dict:
    answerable = [r for r in results if r.kind == ANSWERABLE]
    unanswerable = [r for r in results if r.kind == UNANSWERABLE]
    answered = [r for r in results if r.answered]
    answered_answerable = [r for r in answerable if r.answered]
    abstained = [r for r in results if not r.answered]
    citation_failed = sum(r.abstain_reason == CITATION_CHECK_FAILED for r in results)
    judged = [r for r in answered if r.faithfulness is not None and r.answer_relevance is not None]

    stage_ms: dict[str, dict] = {}
    for stage in STAGES:
        values = [r.stage_ms[stage] for r in results if stage in r.stage_ms]
        stage_ms[stage] = {
            "n": len(values),
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
        }

    return {
        "questions": {ANSWERABLE: len(answerable), UNANSWERABLE: len(unanswerable)},
        # Gated scores (0..1).
        "context_recall": _mean([float(r.quote_in_context) for r in answerable]),
        "faithfulness": _mean([r.faithfulness for r in answered_answerable]),
        "answer_relevance": _mean([r.answer_relevance for r in answered_answerable]),
        # What users get: answers that passed, after any citation retry.
        "citation_validity": _ratio(len(answered), len(answered) + citation_failed),
        "primary_citation_validity": _mean(
            [float(r.primary_citation_ok) for r in results if r.primary_citation_ok is not None]
        ),
        "abstain_precision": _ratio(
            len([r for r in abstained if r.kind == UNANSWERABLE]), len(abstained)
        ),
        # Reported, not gated.
        "recall_at_30": _mean([float(r.quote_in_recall) for r in answerable]),
        "answered_rate": _ratio(len(answered_answerable), len(answerable)),
        "abstain_recall": _ratio(
            len([r for r in unanswerable if not r.answered]), len(unanswerable)
        ),
        "abstain_reasons": {
            ANSWERABLE: dict(Counter(r.abstain_reason or "answered" for r in answerable)),
            UNANSWERABLE: dict(Counter(r.abstain_reason or "answered" for r in unanswerable)),
        },
        "unanswerable_answered": sorted(r.id for r in unanswerable if r.answered),
        "answered": len(answered),
        "llm_failures": sum(r.abstain_reason in LLM_FAILURES for r in results),
        "judge_coverage": _ratio(len(judged), len(answered)),
        "degraded": dict(Counter(d for r in results for d in r.degraded)),
        "llm_cost_usd": sum(r.llm_cost_usd for r in results),
        "stage_ms": stage_ms,
    }


def gate_failures(summary: dict, baseline: dict | None, floors: dict) -> list[str]:
    """Why this run fails the gate ([] = passes). Without a baseline (the first run), only
    the absolute rules apply."""
    failures = []
    if summary.get("request_errors"):
        failures.append(f"{summary['request_errors']} requests failed outright (see the results)")
    if summary["llm_failures"]:
        failures.append(f"{summary['llm_failures']} questions got no answer from either LLM")
    if not summary["answered"]:
        failures.append("nothing was answered, so there is nothing to score")
    for kind in INVALIDATING_DEGRADATIONS:
        if summary["degraded"].get(kind):
            failures.append(f"{summary['degraded'][kind]} requests lost the {kind}: not comparable")
    coverage = summary["judge_coverage"]
    if coverage is not None and coverage < JUDGE_COVERAGE_MIN:
        failures.append(
            f"judge scored only {coverage:.1%} of answers (minimum {JUDGE_COVERAGE_MIN:.0%})"
        )
    # SPEC's 98% unless gate.json sets a temporary floor (DECISIONS 2026-11-24, "Step 18").
    validity_min = floors.get("citation_validity_floor", CITATION_VALIDITY_MIN)
    validity = summary["citation_validity"]
    if validity is not None and validity < validity_min:
        failures.append(f"citation validity {validity:.2%} is below {validity_min:.2%}")
    floor = floors.get("abstain_precision_floor")
    precision = summary["abstain_precision"]
    if floor is not None and (precision is None or precision < floor):
        shown = "n/a" if precision is None else f"{precision:.2%}"
        failures.append(f"abstain precision {shown} is below its floor {floor:.2%}")
    if baseline is not None:
        for name in GATED_SCORES:
            now, then = summary.get(name), baseline.get(name)
            if then is not None and (now is None or now < then - MAX_DROP):
                failures.append(
                    f"{name} {now} dropped more than {MAX_DROP:.0%} points from {then:.3f}"
                )
    return failures


def baseline_mismatches(meta: dict, baseline_meta: dict) -> list[str]:
    """Why this run can't be compared with the baseline ([] = it can)."""
    return [
        f"{key} is {meta.get(key)!r} here but {baseline_meta.get(key)!r} in the baseline"
        for key in COMPARABLE_META
        if meta.get(key) != baseline_meta.get(key)
    ]


def render_report(summary: dict, meta: dict) -> str:
    """Markdown for the console, the CI job summary and the write-up."""

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    def ms(value: float | None) -> str:
        return "n/a" if value is None else f"{value:,.0f}"

    q = summary["questions"]
    lines = [
        f"Eval: {q[ANSWERABLE]} answerable + {q[UNANSWERABLE]} unanswerable questions, "
        f"{meta['models']['primary']} (failover {meta['models']['secondary']}), "
        f"judge {meta['models']['judge']}, git {meta['git_sha'][:12]}"
        + (" (dirty)" if meta["dirty"] else ""),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Context recall (quote in the model's chunks) | {pct(summary['context_recall'])} |",
        f"| Recall@30 (quote in the recalled candidates) | {pct(summary['recall_at_30'])} |",
        f"| Faithfulness | {pct(summary['faithfulness'])} |",
        f"| Answer relevance | {pct(summary['answer_relevance'])} |",
        f"| Answered rate (answerable) | {pct(summary['answered_rate'])} |",
        f"| Citation validity (after retry) | {pct(summary['citation_validity'])} |",
        f"| Primary's citation validity | {pct(summary['primary_citation_validity'])} |",
        f"| Abstain precision | {pct(summary['abstain_precision'])} |",
        f"| Abstain recall (unanswerable) | {pct(summary['abstain_recall'])} |",
        f"| Judge coverage | {pct(summary['judge_coverage'])} |",
        f"| LLM cost (answers) | ${summary['llm_cost_usd']:.4f} |",
        f"| Judge cost | ${meta['judge_cost_usd']:.4f} |",
        "",
        "| Stage | p50 ms | p95 ms |",
        "| --- | ---: | ---: |",
    ]
    for stage, values in summary["stage_ms"].items():
        lines.append(f"| {stage} | {ms(values['p50'])} | {ms(values['p95'])} |")
    lines += [
        "",
        f"Abstain reasons: {summary['abstain_reasons']}",
        f"Degraded: {summary['degraded'] or 'none'}",
    ]
    return "\n".join(lines)
