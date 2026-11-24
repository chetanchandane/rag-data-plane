"""Real answers from a real document (step 15's "done when"), from each production LLM:
the primary, and the secondary after a forced failover. Opt-in: it calls Bedrock (Titan
embeddings, the LLMs) and Cohere, and writes to the Neon dev branch.

    uv run pytest -m paid tests/api/test_query_paid.py

Needs in .env: DATABASE_URL_API, DATABASE_URL_INGEST, AWS_PROFILE (an `aws login` session)
and a Cohere key: COHERE_API_KEY, or read from SSM (/ragdp/prod/cohere_api_key) when it's
unset. The PDF is samples/NIST.SP.800-207-p1-20.pdf (gitignored; see samples/README.md).
Cost per case: about 20 Titan embeddings, one Cohere search and one LLM answer, under $0.01.
"""

import functools
import hashlib
import uuid
from pathlib import Path

import httpx
import pytest

from api.llm import generate, llm_client
from api.rerank import CohereReranker
from shared.embedding import embed_batch, embed_text, make_bedrock_client, query_client
from tests.api.apphelpers import (
    MODEL_ID,
    PRIMARY_LLM,
    REGION,
    build_app,
    client,
    cohere_key,
    make_settings,
)
from worker.local import register
from worker.pipeline import ingest_version

# paid only, not db: `pytest -m db` (and CI) must never select it.
pytestmark = pytest.mark.paid

PDF = Path(__file__).parents[2] / "samples" / "NIST.SP.800-207-p1-20.pdf"


@pytest.fixture
async def nist_tenant(ingest_pool, new_tenant) -> uuid.UUID:
    """A fresh tenant with the NIST PDF indexed through the worker's real ingest path."""
    if not PDF.exists():
        pytest.skip(f"{PDF.name} is not in samples/")
    tenant, doc, data = new_tenant(), uuid.uuid4(), PDF.read_bytes()
    s3_key = f"raw/{tenant}/{doc}/v1/source.pdf"
    async with ingest_pool.acquire() as conn:
        await register(
            conn,
            tenant,
            doc,
            1,
            sha256=hashlib.sha256(data).hexdigest(),
            s3_key=s3_key,
            title="NIST SP 800-207",
            min_role_level=1,
        )
    result = await ingest_version(
        ingest_pool,
        s3_key,
        load_source=lambda: data,
        embed=functools.partial(embed_batch, make_bedrock_client(REGION), model_id=MODEL_ID),
        embedding_model=MODEL_ID,
    )
    assert result.outcome == "indexed", result
    return tenant


# A model id Bedrock rejects (ValidationException), so the primary always fails and the
# secondary must answer. Not Haiku: if the account's Anthropic access is ever granted, Haiku
# would answer and this case would stop testing the failover.
NO_SUCH_MODEL = "us.amazon.no-such-model-v1:0"


@pytest.mark.parametrize(
    ("primary", "degraded"),
    [(PRIMARY_LLM, []), (NO_SUCH_MODEL, ["llm_primary"])],
    ids=["primary-answers", "secondary-answers-after-failover"],
)
async def test_a_real_question_gets_a_cited_answer(api_pool, nist_tenant, primary, degraded):
    app = build_app(api_pool, nist_tenant, role_level=1)
    app.state.settings = make_settings(llm_primary_model_id=primary)
    app.state.embed_query = functools.partial(embed_text, query_client(REGION), model_id=MODEL_ID)
    app.state.generate = functools.partial(generate, llm_client(REGION))

    async with httpx.AsyncClient() as http, client(app) as c:
        model = app.state.settings.rerank_model_id
        app.state.reranker = CohereReranker(http, cohere_key(), model)
        resp = await c.post("/v1/query", json={"query": "What does a policy enforcement point do?"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    print("\n", {k: out[k] for k in ("abstain_reason", "degraded", "top_rerank_score", "usage")})

    assert out["abstained"] is False, out["abstain_reason"]
    assert out["citations"], "an answer always has citations"
    assert out["top_rerank_score"] >= app.state.settings.abstain_threshold
    # Which model answered. ["llm_primary"]: the primary failed (error or unusable output)
    # and the secondary answered. A citation_retry (the primary misquoted, about 1 answer in
    # 10, and the secondary answered) can happen in either case and isn't a failure here.
    assert [d for d in out["degraded"] if d != "citation_retry"] == degraded, out["degraded"]
    # Both production models have a known price, so a null cost means an unexpected model.
    assert out["usage"]["cost_usd"] is not None
