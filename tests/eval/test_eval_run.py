"""Step 18's eval run: the golden set through /v1/query, scored, saved and gated (DECISIONS
2026-11-24, "Step 18 eval baseline"). Opt-in and paid, about $1 a run:

    uv run pytest -m eval -s

Needs EVAL_DATABASE_URL_API (the Neon eval branch, as app_api), an `aws login` session
(Nova, Qwen, DeepSeek) and a Cohere key (COHERE_API_KEY, or SSM). Query embeddings come from
the committed fixture, so there are no Titan calls and the embed stage measures nothing.

EVAL_LIMIT=N runs only the first N questions of each kind, as a cheap smoke test: the file
is marked _sample and the gate is skipped, so a sample can never become or fail a baseline.

The app runs in-process with its real route, token check, recall SQL under RLS, reranker,
LLMs and citation check, as persona a-r1 (every FDA document is level 1), with the models
production uses (infra/envs/prod/prod.tfvars). /v1/query returns no context, so the reranker
is wrapped to record the 30 candidates it got and the order it returned: the first 5 are
what the model saw.

Nothing a single question does can lose the run: request errors and judge errors are
recorded per question, the results file is written, and only then does the gate decide.
"""

import asyncio
import functools
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import BotoCoreError, ClientError

from api.answering import PROMPT_VERSION
from api.llm import generate, llm_client
from api.rerank import CohereReranker, Reranker, RerankError
from shared.db import create_pool
from tests.api.apphelpers import REGION, build_app, client, cohere_key, make_settings, mint
from tests.dbutil import db_url
from tests.eval import judge, metrics
from tests.leak import matrix

# Both markers: `paid` because it calls Bedrock and Cohere (CLAUDE.md), `eval` to select it.
pytestmark = [pytest.mark.eval, pytest.mark.paid]

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "eval" / "golden"
RESULTS = ROOT / "eval" / "results"
GATE_FILE = ROOT / "eval" / "gate.json"
PROD_TFVARS = ROOT / "infra" / "envs" / "prod" / "prod.tfvars"
PERSONA = "a-r1"
PARALLEL = 3  # gentle on Bedrock's per-minute token quotas
RERANK_RETRIES = 2


class RecordingReranker:
    """The real reranker, plus what it was given and what it returned, by query.

    A failed rerank is retried: one slow Cohere reply would otherwise mark the whole run
    invalid (degraded "reranker"), and the eval measures answer quality, not the
    reranker's uptime (that's Slice 3's chaos table). Retries are counted in the results."""

    def __init__(self, inner: Reranker) -> None:
        self.inner = inner
        self.seen: dict[str, tuple[list[str], list[tuple[int, float]] | None]] = {}
        self.retries = 0

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        self.seen[query] = (documents, None)  # stays None if every attempt fails
        for attempt in range(RERANK_RETRIES + 1):
            try:
                ranked = await self.inner.rerank(query, documents, top_n)
                break
            except RerankError:
                if attempt == RERANK_RETRIES:
                    raise
                self.retries += 1
        self.seen[query] = (documents, ranked)
        return ranked

    def context(self, query: str, top_n: int) -> tuple[list[str], list[str]]:
        """(the recalled candidates, the chunks the model saw). With a failed rerank the
        app falls back to RRF order, so the model saw the first top_n candidates."""
        if query not in self.seen:
            return [], []  # nothing recalled: the reranker was never called
        documents, ranked = self.seen[query]
        if ranked is None:
            return documents, documents[:top_n]
        return documents, [documents[index] for index, _ in ranked]


def production_llm_ids(tfvars: str) -> tuple[str, str]:
    """(primary, secondary) from prod.tfvars, so the eval grades the models production uses."""

    def value(name: str) -> str:
        match = re.search(rf'^{name}\s*=\s*"([^"]+)"', tfvars, re.MULTILINE)
        assert match, f"{name} not found in prod.tfvars"
        return match.group(1)

    return value("llm_primary_model_id"), value("llm_secondary_model_id")


def load_questions() -> list[dict]:
    """[{id, kind, question, quote}]: the golden set's answerable and unanswerable questions
    (not the extraction questions, which are leak tests)."""
    questions = []
    for line in (GOLDEN / "questions.jsonl").read_text().splitlines():
        q = json.loads(line)
        questions.append(
            {
                "id": q["id"],
                "kind": metrics.ANSWERABLE,
                "question": q["question"],
                "quote": q["source"]["quote"],
            }
        )
    for line in (GOLDEN / "unanswerable.jsonl").read_text().splitlines():
        q = json.loads(line)
        questions.append(
            {"id": q["id"], "kind": metrics.UNANSWERABLE, "question": q["question"], "quote": None}
        )
    return questions


def golden_hash() -> str:
    digest = hashlib.sha256()
    for name in ("questions.jsonl", "unanswerable.jsonl"):
        digest.update((GOLDEN / name).read_bytes())
    return digest.hexdigest()[:16]


def git_state() -> tuple[str, bool]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".", ":!eval/results"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sha, bool(status.strip())


def new_results_path(stem: str) -> Path:
    """Never overwrite: a rerun on the same commit and day gets _2, _3, ... (else it could
    replace the very baseline it is then compared with)."""
    path, n = RESULTS / f"{stem}.json", 1
    while path.exists():
        n += 1
        path = RESULTS / f"{stem}_{n}.json"
    return path


def stage_durations(finished_spans) -> dict[str, dict[str, float]]:
    """trace id (hex, as the response's trace_id) -> {span name: ms}."""
    by_trace: dict[str, dict[str, float]] = {}
    for span in finished_spans:
        if span.name in metrics.STAGES:
            trace_id = format(span.context.trace_id, "032x")
            by_trace.setdefault(trace_id, {})[span.name] = (span.end_time - span.start_time) / 1e6
    return by_trace


async def test_eval_run(spans):
    questions = load_questions()
    limit = int(os.environ.get("EVAL_LIMIT", "0"))
    if limit:
        questions = [
            q
            for kind in (metrics.ANSWERABLE, metrics.UNANSWERABLE)
            for q in [q for q in questions if q["kind"] == kind][:limit]
        ]
    persona = next(p for p in matrix.personas() if p.name == PERSONA)
    primary, secondary = production_llm_ids(PROD_TFVARS.read_text())
    judge_llm = judge.judge_client(REGION)
    responses: dict[str, dict] = {}
    request_errors: dict[str, str] = {}
    gate = asyncio.Semaphore(PARALLEL)

    pool = await create_pool(db_url("EVAL_DATABASE_URL_API"), max_size=PARALLEL)
    try:
        app = build_app(pool, persona.tenant_id, persona.role_level)
        app.state.settings = settings = make_settings(
            llm_primary_model_id=primary, llm_secondary_model_id=secondary
        )
        app.state.embed_query = matrix.fixture_embedder()
        app.state.generate = functools.partial(generate, llm_client(REGION))
        async with httpx.AsyncClient() as http:
            recorder = RecordingReranker(
                CohereReranker(http, cohere_key(), settings.rerank_model_id)
            )
            app.state.reranker = recorder

            # Errors are recorded, never raised: a raise inside gather() would leave the
            # other requests running (and billing) while the pool closes under them.
            async def ask(c, q: dict) -> None:
                try:
                    async with gate:
                        resp = await c.post("/v1/query", json={"query": q["question"]})
                    if resp.status_code == 200:
                        responses[q["id"]] = resp.json()
                    else:
                        request_errors[q["id"]] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as error:  # noqa: BLE001 - recorded, then fails the gate
                    request_errors[q["id"]] = f"{type(error).__name__}: {error}"[:200]

            async with client(app, token=mint(persona.tenant_id, persona.role_level)) as c:
                await asyncio.gather(*(ask(c, q) for q in questions))
    finally:
        await pool.close()

    durations = stage_durations(spans.get_finished_spans())
    results: list[metrics.QuestionResult] = []
    contexts: dict[str, list[str]] = {}
    for q in questions:
        out = responses.get(q["id"])
        if out is None:
            continue  # in request_errors
        candidates, context = recorder.context(q["question"], settings.llm_context_chunks)
        contexts[q["id"]] = context
        result = metrics.QuestionResult(
            id=q["id"],
            kind=q["kind"],
            abstain_reason=out["abstain_reason"],
            degraded=out["degraded"],
            top_rerank_score=out["top_rerank_score"],
            citations=len(out["citations"]),
            llm_cost_usd=out["usage"]["cost_usd"] or 0.0,
            stage_ms=durations.get(out["trace_id"], {}),
        )
        if q["kind"] == metrics.ANSWERABLE:
            result.quote_in_context = metrics.quote_found(q["quote"], context)
            result.quote_in_recall = metrics.quote_found(q["quote"], candidates)
        results.append(result)

    # The judge scores every answer (unanswerable ones too, for the record); only answers
    # to answerable questions count toward the gated means. A judge error leaves that
    # score None, which the coverage rule counts.
    judge_tokens = [0, 0]
    judge_errors: dict[str, str] = {}

    async def grade(q: dict, result: metrics.QuestionResult) -> None:
        answer = responses[q["id"]]["answer"]
        try:
            async with gate:
                faithful = await asyncio.to_thread(
                    judge.judge_faithfulness, judge_llm, answer, contexts[q["id"]]
                )
                relevant = await asyncio.to_thread(
                    judge.judge_relevance, judge_llm, q["question"], answer
                )
        except (BotoCoreError, ClientError) as error:
            judge_errors[q["id"]] = type(error).__name__
            return
        result.faithfulness, result.claims = faithful.score, faithful.detail
        result.answer_relevance = relevant.score
        for verdict in (faithful, relevant):
            judge_tokens[0] += verdict.input_tokens
            judge_tokens[1] += verdict.output_tokens

    by_id = {q["id"]: q for q in questions}
    await asyncio.gather(*(grade(by_id[r.id], r) for r in results if r.answered))

    sha, dirty = git_state()
    summary = metrics.summarize(results)
    summary["request_errors"] = len(request_errors)
    meta = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": sha,
        "dirty": dirty,
        "where": "ci" if os.environ.get("GITHUB_ACTIONS") else "laptop",
        "golden_sha256": golden_hash(),
        "persona": PERSONA,
        "models": {
            "primary": settings.llm_primary_model_id,
            "secondary": settings.llm_secondary_model_id,
            "judge": judge.JUDGE_MODEL_ID,
            "rerank": settings.rerank_model_id,
        },
        "judge_model": judge.JUDGE_MODEL_ID,
        "prompt_version": PROMPT_VERSION,
        "judge_prompt_version": judge.JUDGE_PROMPT_VERSION,
        "abstain_threshold": settings.abstain_threshold,
        "context_chunks": settings.llm_context_chunks,
        "cohere_searches": len(recorder.seen),
        "rerank_retries": recorder.retries,
        "request_errors": request_errors,
        "judge_errors": judge_errors,
        "judge_cost_usd": judge.judge_cost(*judge_tokens),
        "llm_cost_per_1000_queries_usd": (
            summary["llm_cost_usd"] / len(results) * 1000 if results else None
        ),
    }

    RESULTS.mkdir(exist_ok=True)
    suffix = ("_dirty" if dirty else "") + ("_sample" if limit else "")
    path = new_results_path(f"{meta['date'][:10]}_{sha[:12]}{suffix}")
    payload = {"meta": meta, "summary": summary, "questions": [asdict(r) for r in results]}
    path.write_text(json.dumps(payload, indent=1) + "\n")

    report = metrics.render_report(summary, meta)
    print(f"\n{report}\n\nSaved {path.relative_to(ROOT)}")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(report + "\n")

    # Results are saved before the gate, so a failing run still leaves its numbers.
    if limit:
        return  # a sample is a smoke test, not a measurement
    floors, baseline = {}, None
    failures: list[str] = []
    if GATE_FILE.exists():
        floors = json.loads(GATE_FILE.read_text())
        stored = json.loads((ROOT / floors["baseline"]).read_text())
        baseline = stored["summary"]
        failures += metrics.baseline_mismatches(meta, stored["meta"])
    failures += metrics.gate_failures(summary, baseline, floors)
    assert not failures, failures
