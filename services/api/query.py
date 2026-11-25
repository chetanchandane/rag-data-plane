"""POST /v1/query: a cited answer, or an abstain with a reason (SPEC: Query path, "Response
schema"; DECISIONS 2026-11-22, "Step 15").

Tenant and role level come only from get_claims(). Recall runs under RLS with them, so the
reranker and the LLM only ever see chunks the caller may see.
"""

import uuid
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.answering import AnswerConfig, answer_query
from api.auth import Claims, get_claims
from api.breaker import Breakers
from api.config import Settings
from api.deps import get_breakers, get_pool, get_settings
from api.llm import PRIMARY_READ_TIMEOUT_S, SECONDARY_READ_TIMEOUT_S, Generate, LlmModel
from api.rerank import Reranker
from api.retrieval import MAX_TOP_K, EmbedQuery, retrieve
from api.retrieve import MAX_QUERY_CHARS, get_embed_query, get_reranker

router = APIRouter()
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
    with tracer.start_as_current_span("query") as span:
        recalled = await retrieve(
            pool,
            embed_query,
            breaker=breakers.embedding,
            query=body.query,
            top_k=MAX_TOP_K,
            tenant_id=claims.tenant_id,
            role_level=claims.role_level,
        )
        result = await answer_query(
            body.query,
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
        trace_id = _trace_id(span)

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
    )


def _trace_id(span: trace.Span) -> str:
    """The trace's id, as Honeycomb shows it. With tracing off there is no trace, so the
    response still gets a unique id to quote (POST /v1/feedback will tie to it)."""
    context = span.get_span_context()
    if context.is_valid:
        return format(context.trace_id, "032x")
    return uuid.uuid4().hex
