"""POST /v1/retrieve: hybrid recall only, no reranking or answer (SPEC: Query path; DECISIONS
2026-11-21, "Step 14"). Used by evals, the leak matrix and load tests.

Tenant and role level come only from get_claims(), and the SQL runs under them with RLS.
"""

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.auth import Claims, get_claims
from api.deps import get_pool
from api.retrieval import MAX_TOP_K, EmbedQuery, retrieve

router = APIRouter()

MAX_QUERY_CHARS = 2000


class RetrieveRequest(BaseModel):
    # Like RegisterRequest: unknown fields (tenant_id, role_level) are ignored, never read,
    # and values aren't coerced ("10" is not 10).
    model_config = ConfigDict(extra="ignore", strict=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_k: int = Field(default=MAX_TOP_K, ge=1, le=MAX_TOP_K)

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


class RetrieveResponse(BaseModel):
    chunks: list[ChunkOut]
    lexical_weight: float
    degraded: list[str]  # ["embedding"] when recall fell back to lexical only


def get_embed_query(request: Request) -> EmbedQuery:
    return request.app.state.embed_query


@router.post("/v1/retrieve")
async def retrieve_chunks(
    body: RetrieveRequest,
    claims: Annotated[Claims, Depends(get_claims)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    embed_query: Annotated[EmbedQuery, Depends(get_embed_query)],
) -> RetrieveResponse:
    result = await retrieve(
        pool,
        embed_query,
        query=body.query,
        top_k=body.top_k,
        tenant_id=claims.tenant_id,
        role_level=claims.role_level,
    )
    return RetrieveResponse(
        chunks=[ChunkOut(**vars(chunk)) for chunk in result.chunks],
        lexical_weight=result.lexical_weight,
        degraded=result.degraded,
    )
