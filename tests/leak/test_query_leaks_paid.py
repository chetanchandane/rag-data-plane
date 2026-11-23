"""The sampled /v1/query leak check (step 17; DECISIONS 2026-11-22, "Step 17"). Opt-in and
paid, never in CI: the 10 extraction questions plus 10 seeded golden questions, as all 6
personas, through the real reranker (Cohere) and LLMs (Bedrock) on the eval branch.

    uv run pytest -m paid tests/leak/test_query_leaks_paid.py -s

Needs EVAL_DATABASE_URL_API, an `aws login` session and a Cohere key (COHERE_API_KEY, or
SSM). Query embeddings come from the committed fixture, so there are no Titan calls.
Cost: 120 Cohere searches and 120 to 240 LLM calls, about $0.35.

/v1/query returns no context, so this checks what it does return: canaries in the answer
and in each citation's quote, and the structural check on each cited document. The context
itself is the same recall SQL that the /v1/retrieve matrix checks in CI.
"""

import asyncio
import functools
import random
from collections import Counter

import httpx
import pytest

from api.llm import generate, llm_client
from api.rerank import CohereReranker
from shared.db import create_pool
from tests.api.apphelpers import REGION, build_app, client, cohere_key, mint
from tests.dbutil import db_url
from tests.leak import matrix
from tests.leak.test_leak_matrix import fixture_embedder

pytestmark = pytest.mark.paid

SEED = 20261122
GOLDEN_SAMPLE = 10
PARALLEL_REQUESTS = 2  # gentle on Bedrock's per-minute token quotas


def sample_questions() -> list[dict]:
    questions = matrix.questions()
    golden = sorted((q for q in questions if q["id"].startswith("q")), key=lambda q: q["id"])
    extraction = [q for q in questions if q["id"].startswith("x")]
    return extraction + random.Random(SEED).sample(golden, GOLDEN_SAMPLE)


async def test_answers_never_carry_another_tenants_or_a_higher_levels_data():
    personas, questions = matrix.personas(), sample_questions()
    docs, tokens = matrix.documents(), matrix.canaries()
    leaks: list[matrix.Leak] = []
    outcomes: Counter = Counter()
    cost = 0.0
    gate = asyncio.Semaphore(PARALLEL_REQUESTS)

    pool = await create_pool(db_url("EVAL_DATABASE_URL_API"), max_size=PARALLEL_REQUESTS)
    try:
        first = personas[0]
        app = build_app(pool, first.tenant_id, first.role_level)
        app.state.embed_query = fixture_embedder()
        app.state.generate = functools.partial(generate, llm_client(REGION))
        async with httpx.AsyncClient() as http:
            model = app.state.settings.rerank_model_id
            app.state.reranker = CohereReranker(http, cohere_key(), model)

            async def ask(c, persona: matrix.Persona, question: dict) -> None:
                nonlocal cost
                async with gate:
                    resp = await c.post("/v1/query", json={"query": question["question"]})
                assert resp.status_code == 200, (persona.name, question["id"], resp.text)
                out = resp.json()
                outcomes[out["abstain_reason"] or "answered"] += 1
                outcomes.update(f"degraded:{d}" for d in out["degraded"])
                cost += out["usage"]["cost_usd"] or 0.0
                found, _ = matrix.check_text(persona, question["id"], out["answer"] or "", tokens)
                leaks.extend(found)
                for citation in out["citations"]:
                    leaks.extend(
                        matrix.check_document(persona, question["id"], citation["doc_id"], docs)
                    )
                    found, _ = matrix.check_text(persona, question["id"], citation["quote"], tokens)
                    leaks.extend(found)

            for persona in personas:
                token = mint(persona.tenant_id, persona.role_level)
                async with client(app, token=token) as c:
                    await asyncio.gather(*(ask(c, persona, q) for q in questions))
    finally:
        await pool.close()

    print(f"\n{len(personas) * len(questions)} queries: {dict(outcomes)}; LLM cost ${cost:.4f}")
    assert not leaks, leaks
