"""POST /v1/retrieve: hybrid recall, optionally reranked, no answer (SPEC: Query path;
DECISIONS 2026-11-21 "Step 14", 2026-11-22 "Step 15"). Used by evals, the leak matrix and
load tests. Reranking is off unless the request asks for it: the leak matrix runs without
it, and each reranked request costs a Cohere search.

Tenant and role level come only from get_claims(), and the SQL runs under them with RLS.
"""

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.auth import Claims, get_claims
from api.breaker import Breakers
from api.deps import get_breakers, get_pool
from api.rerank import Reranker, rerank_chunks
from api.retrieval import MAX_TOP_K, EmbedQuery, retrieve

router = APIRouter()

MAX_QUERY_CHARS = 2000


class RetrieveRequest(BaseModel):
    # Like RegisterRequest: unknown fields (tenant_id, role_level) are ignored, never read,
    # and values aren't coerced ("10" is not 10).
    model_config = ConfigDict(extra="ignore", strict=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_k: int = Field(default=MAX_TOP_K, ge=1, le=MAX_TOP_K)
    # True: recall the top 30, rerank them and return the best top_k in rerank order.
    rerank: bool = False

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class ChunkOut(BaseModel):
    chunk_id: UUID
    doc_id: UUID
    version: int
    page_start: int | None
    page_end: int | None
    section_path: str | None
    content: str
    rrf_score: float
    rerank_score: float | None = None  # null unless reranked


class RetrieveResponse(BaseModel):
    chunks: list[ChunkOut]
    lexical_weight: float
    # "embedding": recall fell back to lexical only. "reranker": rerank was asked for but
    # failed, so the chunks are in RRF order.
    degraded: list[str]


def get_embed_query(request: Request) -> EmbedQuery:
    return request.app.state.embed_query


def get_reranker(request: Request) -> Reranker | None:
    return request.app.state.reranker


@router.post("/v1/retrieve")
async def retrieve_chunks(
    body: RetrieveRequest,
    claims: Annotated[Claims, Depends(get_claims)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    embed_query: Annotated[EmbedQuery, Depends(get_embed_query)],
    reranker: Annotated[Reranker | None, Depends(get_reranker)],
    breakers: Annotated[Breakers, Depends(get_breakers)],
) -> RetrieveResponse:
    result = await retrieve(
        pool,
        embed_query,
        breaker=breakers.embedding,
        query=body.query,
        top_k=MAX_TOP_K if body.rerank else body.top_k,
        tenant_id=claims.tenant_id,
        role_level=claims.role_level,
    )
    if not body.rerank:
        chunks = [ChunkOut(**vars(chunk)) for chunk in result.chunks]
        return RetrieveResponse(
            chunks=chunks, lexical_weight=result.lexical_weight, degraded=result.degraded
        )

    reranked = await rerank_chunks(
        reranker, body.query, result.chunks, top_n=body.top_k, breaker=breakers.reranker
    )
    return RetrieveResponse(
        chunks=[
            ChunkOut(**vars(ranked.chunk), rerank_score=ranked.rerank_score)
            for ranked in reranked.chunks
        ],
        lexical_weight=result.lexical_weight,
        degraded=result.degraded + (["reranker"] if reranked.degraded else []),
    )
