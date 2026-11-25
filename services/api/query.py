"""POST /v1/query: a cited answer, or an abstain with a reason (SPEC: Query path, "Response
schema"; DECISIONS 2026-11-22, "Step 15").

Tenant and role level come only from get_claims(). Recall runs under RLS with them, so the
reranker and the LLM only ever see chunks the caller may see.

The cache (api/cache.py) is checked before recall. A hit returns the stored answer with
`cached: true` and calls no embedding, reranker or LLM. A miss runs the pipeline and stores
the answer if it is validated and not degraded.
"""

import logging
import uuid
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from api import cache
from api.answering import AnswerConfig, QueryAnswer, answer_query
from api.auth import Claims, get_claims
from api.breaker import Breakers
from api.config import Settings
from api.deps import get_breakers, get_pool, get_settings
from api.llm import PRIMARY_READ_TIMEOUT_S, SECONDARY_READ_TIMEOUT_S, Generate, LlmModel
from api.rerank import Reranker
from api.retrieval import MAX_TOP_K, EmbedQuery, retrieve
from api.retrieve import MAX_QUERY_CHARS, get_embed_query, get_reranker

router = APIRouter()
log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class QueryRequest(BaseModel):
    # Like the other bodies: unknown fields (tenant_id, role_level) are ignored, never read.
    model_config = ConfigDict(extra="ignore", strict=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class CitationOut(BaseModel):
    chunk_id: UUID
    doc_id: UUID
    page_start: int | None
    quote: str


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # LLM tokens only; null if a model's price is unknown


class QueryResponse(BaseModel):
    answer: str | None
    abstained: bool
    abstain_reason: str | None
    citations: list[CitationOut]
    degraded: list[str]
    top_rerank_score: float | None  # null when the reranker didn't run
    trace_id: str
    usage: UsageOut
    # True: served from the cache. trace_id is this request's, and usage is zero, because
    # no model was called (cost per tenant counts only what was spent).
    cached: bool


# Not stored in the cache: they describe the request, not the answer.
_PER_REQUEST_FIELDS = {"trace_id", "usage", "cached"}
_NO_USAGE = UsageOut(input_tokens=0, output_tokens=0, cost_usd=0.0)


def get_generate(request: Request) -> Generate:
    return request.app.state.generate


def answer_config(settings: Settings) -> AnswerConfig:
    return AnswerConfig(
        primary=LlmModel(
            settings.llm_primary_model_id,
            settings.llm_primary_effort,
            read_timeout_s=PRIMARY_READ_TIMEOUT_S,
        ),
        secondary=LlmModel(
            settings.llm_secondary_model_id, read_timeout_s=SECONDARY_READ_TIMEOUT_S
        ),
        abstain_threshold=settings.abstain_threshold,
        context_chunks=settings.llm_context_chunks,
    )


@router.post("/v1/query")
async def query_documents(
    body: QueryRequest,
    claims: Annotated[Claims, Depends(get_claims)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    settings: Annotated[Settings, Depends(get_settings)],
    embed_query: Annotated[EmbedQuery, Depends(get_embed_query)],
    reranker: Annotated[Reranker | None, Depends(get_reranker)],
    generate: Annotated[Generate, Depends(get_generate)],
    breakers: Annotated[Breakers, Depends(get_breakers)],
) -> QueryResponse:
    # The same text for the key, recall and the prompt: one key, one set of inputs.
    query = cache.normalize_query(body.query)
    with tracer.start_as_current_span("query") as span:
        trace_id = _trace_id(span)
        span.set_attribute("cache.enabled", settings.query_cache)
        lookup = None
        if settings.query_cache:
            lookup = await cache.lookup(
                pool,
                claims.tenant_id,
                claims.role_level,
                query,
                cache.answer_version(settings),
            )
            hit = _from_cache(lookup.response, trace_id) if lookup.response else None
            span.set_attribute("cache.hit", hit is not None)
            if hit is not None:
                return hit

        recalled = await retrieve(
            pool,
            embed_query,
            breaker=breakers.embedding,
            query=query,
            top_k=MAX_TOP_K,
            tenant_id=claims.tenant_id,
            role_level=claims.role_level,
        )
        result = await answer_query(
            query,
            recalled,
            reranker=reranker,
            generate=generate,
            config=answer_config(settings),
            breakers=breakers,
        )
        span.set_attributes(
            {
                "query.abstain_reason": result.abstain_reason or "",
                "query.degraded": ",".join(result.degraded),
                "query.top_rerank_score": (
                    result.top_rerank_score if result.top_rerank_score is not None else -1.0
                ),
            }
        )
        response = _response(result, trace_id)
        if lookup is not None and cache.is_cacheable(result):
            # lookup.key, not a new key: it holds the epoch read before recall (api/cache.py).
            stored = response.model_dump(mode="json", exclude=_PER_REQUEST_FIELDS)
            await cache.store(pool, claims.tenant_id, claims.role_level, lookup.key, stored)
    return response


def _response(result: QueryAnswer, trace_id: str) -> QueryResponse:
    return QueryResponse(
        answer=result.answer,
        abstained=result.abstained,
        abstain_reason=result.abstain_reason,
        citations=[CitationOut(**vars(citation)) for citation in result.citations],
        degraded=result.degraded,
        top_rerank_score=result.top_rerank_score,
        trace_id=trace_id,
        usage=UsageOut(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
        ),
        cached=False,
    )


def _from_cache(stored: dict[str, Any], trace_id: str) -> QueryResponse | None:
    """The stored answer as this request's response, or None if it no longer fits the
    schema. PROMPT_VERSION should change with the schema; if a change forgets that, the
    entry is a miss (and gets overwritten), not a day of 500s."""
    try:
        return QueryResponse.model_validate(
            stored | {"trace_id": trace_id, "usage": _NO_USAGE, "cached": True}
        )
    except ValidationError:
        log.warning("cached query response no longer fits the schema; treated as a miss")
        return None


def _trace_id(span: trace.Span) -> str:
    """The trace's id, as Honeycomb shows it. With tracing off there is no trace, so the
    response still gets a unique id to quote (POST /v1/feedback will tie to it)."""
    context = span.get_span_context()
    if context.is_valid:
        return format(context.trace_id, "032x")
    return uuid.uuid4().hex
