"""The FastAPI app, built by create_app() (an ASGI app factory).

Served two ways:
  - `api.local` (uvicorn, this laptop): resources are opened and closed by the lifespan.
  - Lambda (`api.handler`, Mangum): resources are opened once per container and reused by
    warm invocations (SPEC: Query path, "asyncpg details"). Mangum would run the lifespan
    around every invocation, so the handler turns it off and calls open_resources() itself.
"""

import functools
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import boto3
import httpx
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import documents, query, retrieve, whoami
from api.breaker import Breakers
from api.config import RETRY_AFTER_SECONDS, Settings
from api.llm import llm_generate
from api.rerank import CohereReranker, Reranker, cohere_api_key
from api.uploads import make_s3_client
from shared.db import DB_UNAVAILABLE, create_pool
from shared.embedding import embed_text, query_client
from shared.telemetry import setup_tracing

# A query that gets no answer in this long fails instead of hanging: above the 2 s
# statement_timeout, which Postgres enforces itself while the connection is alive.
DB_COMMAND_TIMEOUT_SECONDS = 3
# Long enough for Neon to wake a suspended compute, well inside the Lambda's 15 s.
DB_CONNECT_TIMEOUT_SECONDS = 10

log = logging.getLogger(__name__)


async def open_resources(app: FastAPI, service_name: str = "ragdp-api-local") -> None:
    """service_name is also the Honeycomb dataset: ragdp-api on Lambda, ragdp-api-local for
    api.local. Tracing stays off without a Honeycomb key (shared/telemetry.py)."""
    settings = Settings.from_env()
    app.state.settings = settings
    ssm = None
    if os.environ.get("HONEYCOMB_API_KEY_PARAM") or os.environ.get("COHERE_API_KEY_PARAM"):
        ssm = boto3.client("ssm", region_name=settings.aws_region)
    app.state.tracer_provider = setup_tracing(service_name, os.environ, ssm)
    app.state.pool = await create_pool(
        settings.database_url,
        command_timeout=DB_COMMAND_TIMEOUT_SECONDS,
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
    )
    app.state.s3 = make_s3_client(settings.aws_region)
    # The delete job. Short timeouts and two attempts in all (standard mode counts the
    # first), at most about 10 s: botocore's 60 s defaults would outlast the Lambda's 29 s,
    # and a failed send is a 503 that the client retries.
    app.state.sqs = boto3.client(
        "sqs",
        region_name=settings.aws_region,
        config=Config(
            connect_timeout=2, read_timeout=3, retries={"mode": "standard", "max_attempts": 2}
        ),
    )
    # The query's embedding, blocking (api/retrieval.py runs it on a worker thread).
    app.state.embed_query = functools.partial(
        embed_text, query_client(settings.aws_region), model_id=settings.embedding_model_id
    )
    # One HTTP connection pool for the reranker, reused by warm invocations.
    app.state.http = httpx.AsyncClient()
    app.state.reranker = _reranker(settings, app.state.http, ssm)
    # The answer, blocking (api/answering.py runs it on a worker thread), with each model's
    # own read timeout.
    app.state.generate = llm_generate(settings.aws_region)
    app.state.verifier = None
    if settings.jwt_issuer:
        # Imported only here: on Lambda JWT_ISSUER isn't set, API Gateway verifies tokens,
        # and PyJWT isn't installed (api/auth.py).
        from api.token_verifier import TokenVerifier

        app.state.verifier = TokenVerifier.from_issuer(settings.jwt_issuer)


def _reranker(settings: Settings, http: httpx.AsyncClient, ssm) -> Reranker | None:
    """Cohere, or None if no key is configured or it can't be read. Without a reranker the
    API still answers, flagged degraded: ["reranker"], rather than failing to start."""
    try:
        api_key = cohere_api_key(os.environ, ssm)
    except Exception:
        log.exception("reranker off: the Cohere key could not be read")
        return None
    if api_key is None:
        log.warning("reranker off: no Cohere key configured")
        return None
    return CohereReranker(http, api_key, settings.rerank_model_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await open_resources(app)
    try:
        yield
    finally:
        await app.state.http.aclose()
        await app.state.pool.close()


async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    log.warning("503 on %s: %s: %s", request.url.path, type(exc).__name__, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "database unavailable, retry shortly"},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def create_app() -> FastAPI:
    app = FastAPI(title="rag-data-plane", lifespan=lifespan)
    # Built with the app, not in open_resources: one set per container (on Lambda, per
    # instance, as SPEC says), and an app built for a test gets fresh, closed breakers.
    app.state.breakers = Breakers()
    app.include_router(documents.router)
    app.include_router(retrieve.router)
    app.include_router(query.router)
    app.include_router(whoami.router)
    # Postgres unreachable or too slow: 503, the client retries. Anything else stays a 500.
    for exc_class in DB_UNAVAILABLE:
        app.add_exception_handler(exc_class, _database_unavailable)
    return app
