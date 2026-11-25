"""Step 19's backup-model probe (DECISIONS 2026-11-24, "Step 19"). Paid: Bedrock for up
to 4 models, plus one Cohere rerank per question.

    PYTHONPATH=services uv run python scripts/probe_backup.py            # the 14 retry prompts
    PYTHONPATH=services uv run python scripts/probe_backup.py --extra 16 # plus 16 more
    ... --ids q011 q060 --models qwen.qwen3-235b-a22b-2507-v1:0       # a cheap subset

The question it answers: which backup model answers the prompts the primary misquoted,
fast enough and with quotes that pass the citation check? In the baseline, 3 of the 14
citation retries timed out on Qwen at 10 s.

Each question goes through the API's own path: fixture embedding, recall SQL under RLS as
persona a-r1 on the eval branch, Cohere rerank, the top 5 chunks, build_prompt() and
SYSTEM_PROMPT. The question is reranked once and the identical prompt is sent to every
candidate, so the models are compared on the same input. Qwen (today's backup) runs too,
as the control.

The read timeout here is 30 s, so the probe records how long each model really takes. The
summary counts answers usable within 10 s (production's limit for each model when the probe
was written) and within 14 s (the backup's limit since step 19); that comparison is what
moved the split to 6 s / 14 s.

Results go to eval/results/probes/<date>_backup_probe.json.
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # for tests.* helpers (fixture embeddings, personas, Cohere key)

import boto3  # noqa: E402
from tests.api.apphelpers import REGION, RERANK_MODEL_ID, cohere_key  # noqa: E402
from tests.leak import matrix  # noqa: E402

from api.answering import (  # noqa: E402
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_prompt,
    check_citations,
    normalize,
)
from api.breaker import Breakers  # noqa: E402
from api.llm import LLM_TIMEOUTS, PRICES_PER_MILLION, LlmModel, generate  # noqa: E402
from api.rerank import CohereReranker, rerank_chunks  # noqa: E402
from api.retrieval import MAX_TOP_K, retrieve  # noqa: E402
from shared.db import create_pool  # noqa: E402

BASELINE = ROOT / "eval" / "results" / "2026-11-24_94a08e6ee5e1.json"
QUESTIONS = ROOT / "eval" / "golden" / "questions.jsonl"
OUT_DIR = ROOT / "eval" / "results" / "probes"
PERSONA = "a-r1"
CONTEXT_CHUNKS = 5  # production's LLM_CONTEXT_CHUNKS
PROBE_READ_TIMEOUT_S = 30
PARALLEL_QUESTIONS = 3  # gentle on per-minute token quotas

CANDIDATES = [
    "qwen.qwen3-235b-a22b-2507-v1:0",  # today's backup: the control
    "zai.glm-4.7",
    "moonshotai.kimi-k2.5",
    "zai.glm-4.7-flash",
]
# USD per million tokens (input, output), AWS Price List API, us-east-2, 2026-11-24
# (DECISIONS 2026-11-24, "Step 18 baseline and temporary floors").
PROBE_PRICES = PRICES_PER_MILLION | {
    "zai.glm-4.7": (0.60, 2.20),
    "moonshotai.kimi-k2.5": (0.60, 3.00),
    "zai.glm-4.7-flash": (0.07, 0.40),
}


def golden_ids() -> list[str]:
    return [json.loads(line)["id"] for line in QUESTIONS.read_text().splitlines()]


def pick_questions(extra: int) -> list[dict]:
    """The baseline's citation-retry questions (the prompts the backup actually gets), then
    the first `extra` other answerable questions, for a larger sample of quote accuracy."""
    baseline = json.loads(BASELINE.read_text())
    retry_ids = [q["id"] for q in baseline["questions"] if "citation_retry" in q["degraded"]]
    golden = [json.loads(line) for line in QUESTIONS.read_text().splitlines()]
    retried = [q for q in golden if q["id"] in retry_ids]
    others = [q for q in golden if q["id"] not in retry_ids][:extra]
    return [{"id": q["id"], "question": q["question"], "retry": True} for q in retried] + [
        {"id": q["id"], "question": q["question"], "retry": False} for q in others
    ]


def call_model(client, model_id: str, prompt: str, chunks) -> dict:
    """One Converse call through the API's generate(), timed, then citation-checked."""
    started = time.perf_counter()
    try:
        generation = generate(
            client, LlmModel(model_id, read_timeout_s=PROBE_READ_TIMEOUT_S), SYSTEM_PROMPT, prompt
        )
    except (BotoCoreError, ClientError) as error:
        return {
            "model": model_id,
            "ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(error).__name__}: {error}"[:300],
        }
    ms = round((time.perf_counter() - started) * 1000)
    prices = PROBE_PRICES[model_id]
    row = {
        "model": model_id,
        "ms": ms,
        "error": None,
        "stop_reason": generation.stop_reason,
        "input_tokens": generation.input_tokens,
        "output_tokens": generation.output_tokens,
        "cost_usd": (generation.input_tokens * prices[0] + generation.output_tokens * prices[1])
        / 1_000_000,
        "valid_output": generation.answer is not None,
        "answerable": generation.answer.answerable if generation.answer else None,
        "citation_failure": None,
    }
    if generation.answer is not None and generation.answer.answerable:
        _, rule = check_citations(generation.answer, chunks)
        row["citation_failure"] = rule
        if rule == "quote_not_found":
            row["failed_quote"] = first_failed_quote(generation.answer, chunks)
    return row


def first_failed_quote(answer, chunks) -> dict:
    """The quote check_citations rejected and the chunk it cites, to see why it failed
    (typography, a footnote marker, drift). The corpus is public FDA/ICH guidance."""
    by_id = {str(chunk.chunk_id): chunk for chunk in chunks}
    for cited in answer.citations:
        chunk = by_id.get(cited.chunk_id.strip())
        if chunk is not None and normalize(cited.quote) not in normalize(chunk.content):
            return {"quote": cited.quote, "chunk_id": str(chunk.chunk_id), "chunk": chunk.content}
    return {}


def usable(row: dict, within_s: float) -> bool:
    """Would production have used this answer, given within_s seconds for the call?"""
    return (
        row["error"] is None
        and row["ms"] <= within_s * 1000
        and row["valid_output"]
        and row["answerable"] is True
        and row["citation_failure"] is None
    )


def percentile(values: list[int], p: float) -> int | None:
    """Nearest rank, like the eval's."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def summarize(rows: list[dict], models: list[str]) -> dict:
    summary = {}
    for model_id in models:
        mine = [r for r in rows if r["model"] == model_id]
        times = [r["ms"] for r in mine if r["error"] is None]
        summary[model_id] = {
            "calls": len(mine),
            "errors": sum(r["error"] is not None for r in mine),
            "ms_p50": percentile(times, 0.5),
            "ms_p95": percentile(times, 0.95),
            "ms_max": max(times) if times else None,
            "over_10s": sum(t > 10_000 for t in times),
            "over_14s": sum(t > 14_000 for t in times),
            "output_tokens_max": max(
                (r["output_tokens"] for r in mine if r["error"] is None), default=None
            ),
            "invalid_output": sum(r["error"] is None and not r["valid_output"] for r in mine),
            "said_not_answerable": sum(r.get("answerable") is False for r in mine),
            "citation_failures": sum(r.get("citation_failure") is not None for r in mine),
            "usable_within_10s": sum(usable(r, 10) for r in mine),
            "usable_within_14s": sum(usable(r, 14) for r in mine),
            "usable_on_retry_prompts_10s": sum(usable(r, 10) for r in mine if r["retry"]),
            "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in mine), 4),
        }
    return summary


def new_output_path() -> Path:
    """A fresh file (_2, _3, ...), chosen before any paid call so no result is overwritten
    or lost."""
    stem = f"{datetime.now(UTC):%Y-%m-%d}_backup_probe"
    out = OUT_DIR / f"{stem}.json"
    n = 2
    while out.exists():
        out = OUT_DIR / f"{stem}_{n}.json"
        n += 1
    return out


async def main(extra: int, only_ids: list[str], models: list[str]) -> None:
    load_dotenv()
    db = os.environ.get("EVAL_DATABASE_URL_API", "").strip()
    if not db:
        sys.exit("EVAL_DATABASE_URL_API is not set")
    unknown = set(models) - set(CANDIDATES)
    if unknown:
        sys.exit(f"unknown models {sorted(unknown)}; choose from {CANDIDATES}")
    out = new_output_path()
    questions = pick_questions(extra)
    if only_ids:
        # Any answerable question, not only the ones --extra picked; an unknown id is an error
        # rather than a silently empty (but written) probe.
        questions = [q for q in pick_questions(len(golden_ids())) if q["id"] in only_ids]
        missing = set(only_ids) - {q["id"] for q in questions}
        if missing:
            sys.exit(f"not answerable golden questions: {sorted(missing)}")
    persona = next(p for p in matrix.personas() if p.name == PERSONA)
    embed = matrix.fixture_embedder()
    # Production's client settings (no retry), with a longer read timeout to see true latency.
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=LLM_TIMEOUTS.merge(Config(read_timeout=PROBE_READ_TIMEOUT_S)),
    )
    rows: list[dict] = []
    gate = asyncio.Semaphore(PARALLEL_QUESTIONS)

    pool = await create_pool(db, max_size=PARALLEL_QUESTIONS)
    try:
        async with httpx.AsyncClient() as http:
            reranker = CohereReranker(http, cohere_key(), RERANK_MODEL_ID)
            # Like one API instance: if Cohere keeps failing, the reranker's breaker opens
            # and those questions are skipped below, as a failed rerank is.
            breakers = Breakers()

            async def probe(q: dict) -> None:
                async with gate:
                    recalled = await retrieve(
                        pool,
                        embed,
                        breaker=breakers.embedding,
                        query=q["question"],
                        top_k=MAX_TOP_K,
                        tenant_id=persona.tenant_id,
                        role_level=persona.role_level,
                    )
                    reranked = await rerank_chunks(
                        reranker,
                        q["question"],
                        recalled.chunks,
                        top_n=CONTEXT_CHUNKS,
                        breaker=breakers.reranker,
                    )
                    if reranked.degraded:
                        print(f"{q['id']}: rerank failed, skipped (not the prompt production saw)")
                        return
                    chunks = [r.chunk for r in reranked.chunks]
                    prompt = build_prompt(q["question"], chunks)
                    # The four models in parallel: separate endpoints, the same prompt.
                    results = await asyncio.gather(
                        *(asyncio.to_thread(call_model, bedrock, m, prompt, chunks) for m in models)
                    )
                    for row in results:
                        rows.append({"id": q["id"], "retry": q["retry"]} | row)
                    print(
                        q["id"],
                        "  ".join(
                            f"{r['model'].split('.')[1][:12]}={r['ms']}ms"
                            f"{'' if usable(r, PROBE_READ_TIMEOUT_S) else '(x)'}"
                            for r in results
                        ),
                        flush=True,
                    )

            await asyncio.gather(*(probe(q) for q in questions))
    finally:
        await pool.close()

    summary = summarize(rows, models)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "meta": {
                    "date": datetime.now(UTC).isoformat(timespec="seconds"),
                    "prompt_version": PROMPT_VERSION,
                    "persona": PERSONA,
                    "questions": len(questions),
                    "retry_questions": sum(q["retry"] for q in questions),
                    "read_timeout_s": PROBE_READ_TIMEOUT_S,
                    "baseline": BASELINE.name,
                    "models": models,
                },
                "summary": summary,
                "calls": sorted(rows, key=lambda r: (r["id"], r["model"])),
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(summary, indent=2))
    print(f"total ${sum(s['cost_usd'] for s in summary.values()):.4f}; written to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extra", type=int, default=0, help="other answerable questions to add")
    parser.add_argument("--ids", nargs="*", default=[], help="only these question ids")
    parser.add_argument("--models", nargs="*", default=CANDIDATES, help="a subset of CANDIDATES")
    args = parser.parse_args()
    asyncio.run(main(args.extra, args.ids, args.models))
