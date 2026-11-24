"""The leak matrix (step 17; SPEC: Proof, "Leak matrix"). Every golden, unanswerable and
extraction question, as every persona, through /v1/retrieve: reranking off, top 30.

    uv run pytest -m leak          # needs EVAL_DATABASE_URL_API (app_api on the eval branch)

It calls no paid API: the query embeddings come from eval/golden/question_embeddings.json.
Everything else is real: the API's route, token verification, the hybrid SQL and Postgres
RLS, connected as app_api. CI runs it on a copy of the eval branch, and any leak fails the
build.
"""

import asyncio
import os

import pytest

from shared.db import create_pool
from tests.api.apphelpers import build_app, client, mint
from tests.dbutil import db_url
from tests.leak import matrix

pytestmark = pytest.mark.leak

PARALLEL_REQUESTS = 4  # the pool's size: enough to finish in a minute or two


async def run_matrix(app, personas, questions) -> list[matrix.PersonaResult]:
    docs, tokens = matrix.documents(), matrix.canaries()
    results = [matrix.PersonaResult(p) for p in personas]
    gate = asyncio.Semaphore(PARALLEL_REQUESTS)

    async def ask(http, result: matrix.PersonaResult, question: dict) -> None:
        async with gate:
            resp = await http.post("/v1/retrieve", json={"query": question["question"]})
        assert resp.status_code == 200, (result.persona.name, question["id"], resp.text)
        result.add_retrieval(question["id"], resp.json(), docs, tokens)

    for result in results:
        token = mint(result.persona.tenant_id, result.persona.role_level)
        async with client(app, token=token) as http:
            await asyncio.gather(*(ask(http, result, q) for q in questions))
    return results


def publish(report: str) -> None:
    print("\n" + report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report + "\n")


async def test_no_persona_sees_another_tenants_or_a_higher_levels_data():
    personas, questions = matrix.personas(), matrix.questions()
    pool = await create_pool(db_url("EVAL_DATABASE_URL_API"), max_size=PARALLEL_REQUESTS)
    try:
        # build_app's own token is unused: each persona gets its own below.
        first = personas[0]
        app = build_app(pool, first.tenant_id, first.role_level)
        app.state.embed_query = matrix.fixture_embedder()
        results = await run_matrix(app, personas, questions)
    finally:
        await pool.close()
    publish(matrix.render_report(results, len(questions)))

    leaks = [leak for r in results for leak in r.leaks]
    assert not leaks, f"{len(leaks)} leaks, first 20: {leaks[:20]}"
    # The fixture's vectors must really have been used, or this was a lexical-only run.
    assert all(r.degraded == 0 for r in results), {r.persona.name: r.degraded for r in results}
    # Positive control: 0 leaks means something only if each persona sees what it may see.
    missing = {r.persona.name: r.missing_canaries for r in results if r.missing_canaries}
    assert not missing, f"allowed canaries never retrieved: {missing}"
    assert all(r.chunks for r in results)
