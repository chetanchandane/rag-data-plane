"""SPEC: Proof, "Chaos tests", reranker row: "quality delta recorded" (step 23).

With the reranker down, the model sees the top 5 of recall in RRF order instead of the
reranked top 5. Context recall (the golden quote is in the chunks the model saw) is a plain
string match, so the delta is measured without any paid call: every answerable golden
question goes through /v1/retrieve with reranking off, on the Neon eval branch, with the
committed query embeddings. It's compared with the stored baseline's reranked figure, which
came from the same recall over the same corpus.

    uv run pytest -m chaos -s

Free, opt-in (needs EVAL_DATABASE_URL_API). Saves eval/results/<date>_<sha>_no_rerank.json.
It records the delta and asserts nothing about its size: the degraded mode is meant to be
worse, and this says how much.
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from shared.db import create_pool
from tests.api.apphelpers import build_app, client
from tests.dbutil import db_url
from tests.eval import metrics
from tests.eval.test_eval_run import (
    GATE_FILE,
    PERSONA,
    ROOT,
    git_state,
    load_questions,
    new_results_path,
)
from tests.leak import matrix

pytestmark = pytest.mark.chaos

PARALLEL = 4


async def test_context_recall_without_the_reranker():
    baseline_path = ROOT / json.loads(GATE_FILE.read_text())["baseline"]
    baseline = json.loads(baseline_path.read_text())
    baseline_in_context = {q["id"]: q["quote_in_context"] for q in baseline["questions"]}
    # As many chunks as the baseline's model saw, so both sides compare the same top N.
    context_chunks = baseline["meta"]["context_chunks"]
    questions = [q for q in load_questions() if q["kind"] == metrics.ANSWERABLE]
    persona = next(p for p in matrix.personas() if p.name == PERSONA)
    found: dict[str, tuple[bool, bool]] = {}  # id -> (in RRF top N, in recall's top 30)
    errors: dict[str, str] = {}
    gate = asyncio.Semaphore(PARALLEL)

    pool = await create_pool(db_url("EVAL_DATABASE_URL_API"), max_size=PARALLEL)
    try:
        app = build_app(pool, persona.tenant_id, persona.role_level)
        app.state.embed_query = matrix.fixture_embedder()

        # Errors are recorded, never raised: a raise inside gather() would leave the other
        # requests running while the pool closes under them (as in test_eval_run).
        async def ask(c, q: dict) -> None:
            try:
                async with gate:
                    resp = await c.post("/v1/retrieve", json={"query": q["question"]})
                out = resp.json()
                if resp.status_code != 200 or out["degraded"]:  # RRF only, nothing else lost
                    errors[q["id"]] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    return
                texts = [chunk["content"] for chunk in out["chunks"]]  # RRF order, top 30
                found[q["id"]] = (
                    metrics.quote_found(q["quote"], texts[:context_chunks]),
                    metrics.quote_found(q["quote"], texts),
                )
            except Exception as error:  # noqa: BLE001 - recorded, then fails the test
                errors[q["id"]] = f"{type(error).__name__}: {error}"[:200]

        async with client(app) as c:
            await asyncio.gather(*(ask(c, q) for q in questions))
    finally:
        await pool.close()
    assert not errors, errors

    ids = [q["id"] for q in questions]
    rrf_recall = sum(found[i][0] for i in ids) / len(ids)
    recall_at_30 = sum(found[i][1] for i in ids) / len(ids)
    reranked_recall = baseline["summary"]["context_recall"]
    lost = sorted(i for i in ids if baseline_in_context.get(i) and not found[i][0])
    gained = sorted(i for i in ids if baseline_in_context.get(i) is False and found[i][0])

    sha, dirty = git_state()
    summary = {
        "questions": len(ids),
        "context_chunks": context_chunks,
        "context_recall_rrf": rrf_recall,
        "context_recall_reranked": reranked_recall,
        "delta_points": round((rrf_recall - reranked_recall) * 100, 1),
        # Recall itself is unchanged by the reranker, so this should equal the baseline's
        # recall_at_30: a check that the same system was measured.
        "recall_at_30": recall_at_30,
        "baseline_recall_at_30": baseline["summary"]["recall_at_30"],
        "lost_vs_baseline": lost,
        "gained_vs_baseline": gained,
    }
    meta = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": sha,
        "dirty": dirty,
        "persona": PERSONA,
        "baseline": str(baseline_path.relative_to(ROOT)),
        "what": "context recall with the reranker down (RRF top N), vs the reranked baseline",
    }
    suffix = ("_dirty" if dirty else "") + "_no_rerank"
    path = new_results_path(f"{meta['date'][:10]}_{sha[:12]}{suffix}")
    path.write_text(json.dumps({"meta": meta, "summary": summary}, indent=1) + "\n")
    print(f"\n{json.dumps(summary, indent=1)}\n\nSaved {path.relative_to(ROOT)}")

    assert summary["recall_at_30"] == summary["baseline_recall_at_30"]
