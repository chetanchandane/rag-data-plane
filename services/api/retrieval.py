"""Hybrid recall (SPEC: Query path, "Hybrid recall SQL"; DECISIONS 2026-11-21, "Step 14").

One SQL round trip, under the caller's RLS context: dense (pgvector cosine distance) and
lexical (Postgres full-text) candidates, fused with reciprocal rank fusion (RRF). If the
query can't be embedded, the lexical branch runs alone and the result says so
(degraded: ["embedding"]), as SPEC's fallback table says.

Spans: retrieve > embed, sql_recall. Attributes are counts, weights and outcomes, never the
query text or chunk content.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry import trace

from shared.db import rls_transaction

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MAX_TOP_K = 30  # SPEC: top 30 after fusion
DEFAULT_LEXICAL_WEIGHT = 1.0
CODE_LIKE_LEXICAL_WEIGHT = 2.0

# SPEC's example of a code-like token: 1-4 capital letters, an optional hyphen, 2-6 digits,
# such as "SP800", "AC-12" or "ICH E10" (but not "AC-2" or "E6": one digit). Whole tokens
# only, so a longer word that merely contains such a run doesn't count. Tune on the golden
# set, not by feel.
_CODE_LIKE = re.compile(r"\b[A-Z]{1,4}-?\d{2,6}\b")
# A quoted phrase: "...". websearch_to_tsquery treats it as a phrase search.
_QUOTED_PHRASE = re.compile(r'"[^"]+"')

# Exactly the SPEC's SQL, with the final LIMIT as $4 (top_k, at most 30). The live-version
# join appears in both branches on purpose: a CTE referenced twice gets materialized and
# loses index use. There is no tenant or role predicate: RLS adds it.
HYBRID_SQL = """
WITH dense AS (
  SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> $1) AS rnk
  FROM chunks c
  JOIN documents d ON d.id = c.doc_id AND d.active_version = c.version AND d.deleted_at IS NULL
  ORDER BY c.embedding <=> $1
  LIMIT 50
),
lexical AS (
  SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.fts, q) DESC) AS rnk
  FROM chunks c
  JOIN documents d ON d.id = c.doc_id AND d.active_version = c.version AND d.deleted_at IS NULL,
       websearch_to_tsquery('english', $2) AS q
  WHERE c.fts @@ q
  ORDER BY ts_rank_cd(c.fts, q) DESC
  LIMIT 50
),
fused AS (
  SELECT id, SUM(w / (60 + rnk)) AS rrf
  FROM (SELECT id, rnk, 1.0 AS w FROM dense
        UNION ALL
        SELECT id, rnk, $3::float8 AS w FROM lexical) s
  GROUP BY id
)
SELECT c.id, c.doc_id, c.version, c.page_start, c.page_end, c.section_path, c.content, f.rrf
FROM fused f JOIN chunks c ON c.id = f.id
ORDER BY f.rrf DESC
LIMIT $4
"""

# The fallback when the query can't be embedded: the lexical branch alone, scored like its
# share of the fused score, so scores mean the same thing in both modes.
LEXICAL_ONLY_SQL = """
WITH lexical AS (
  SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.fts, q) DESC) AS rnk
  FROM chunks c
  JOIN documents d ON d.id = c.doc_id AND d.active_version = c.version AND d.deleted_at IS NULL,
       websearch_to_tsquery('english', $1) AS q
  WHERE c.fts @@ q
  ORDER BY ts_rank_cd(c.fts, q) DESC
  LIMIT 50
)
SELECT c.id, c.doc_id, c.version, c.page_start, c.page_end, c.section_path, c.content,
       $2::float8 / (60 + l.rnk) AS rrf
FROM lexical l JOIN chunks c ON c.id = l.id
ORDER BY rrf DESC
LIMIT $3
"""

EmbedQuery = Callable[[str], list[float]]  # blocking: called on a worker thread


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    doc_id: UUID
    version: int
    page_start: int | None
    page_end: int | None
    section_path: str | None
    content: str
    rrf_score: float


@dataclass(frozen=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    lexical_weight: float
    degraded: list[str]


def lexical_weight(query: str) -> float:
    """SPEC: 2.0 when the query contains code-like tokens or quoted phrases, else 1.0.
    Exact strings matter more to lexical search there than to embeddings."""
    if _CODE_LIKE.search(query) or _QUOTED_PHRASE.search(query):
        return CODE_LIKE_LEXICAL_WEIGHT
    return DEFAULT_LEXICAL_WEIGHT


async def retrieve(
    pool: asyncpg.Pool,
    embed_query: EmbedQuery,
    *,
    query: str,
    top_k: int,
    tenant_id: UUID,
    role_level: int,
) -> RetrievalResult:
    """Up to top_k live chunks the caller may see, best first. tenant_id and role_level must
    come from verified claims."""
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be 1 to {MAX_TOP_K}")
    weight = lexical_weight(query)
    degraded: list[str] = []

    with tracer.start_as_current_span("retrieve") as span:
        span.set_attributes({"retrieve.top_k": top_k, "retrieve.lexical_weight": weight})

        embedding = await _embed(embed_query, query)
        if embedding is None:
            degraded.append("embedding")

        with tracer.start_as_current_span("sql_recall") as sql_span:
            mode = "hybrid" if embedding is not None else "lexical_only"
            sql_span.set_attribute("retrieve.mode", mode)
            async with pool.acquire() as conn, rls_transaction(conn, tenant_id, role_level):
                if embedding is not None:
                    rows = await conn.fetch(HYBRID_SQL, embedding, query, weight, top_k)
                else:
                    rows = await conn.fetch(LEXICAL_ONLY_SQL, query, weight, top_k)
            sql_span.set_attribute("retrieve.chunk_count", len(rows))

        span.set_attribute("retrieve.degraded", ",".join(degraded))

    chunks = [
        RetrievedChunk(
            chunk_id=row["id"],
            doc_id=row["doc_id"],
            version=row["version"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            section_path=row["section_path"],
            content=row["content"],
            rrf_score=float(row["rrf"]),
        )
        for row in rows
    ]
    return RetrievalResult(chunks=chunks, lexical_weight=weight, degraded=degraded)


async def _embed(embed_query: EmbedQuery, query: str) -> list[float] | None:
    """The query's embedding, or None if Bedrock failed after its retry (the client's
    timeouts bound the wait). Any other error is a bug and is raised."""
    with tracer.start_as_current_span("embed") as span:
        try:
            # boto3 blocks, so it runs on a worker thread and the event loop stays free.
            return await asyncio.to_thread(embed_query, query)
        except (BotoCoreError, ClientError) as error:
            log.warning("query embedding failed, lexical-only recall: %s", type(error).__name__)
            span.set_attribute("embed.error", type(error).__name__)
            return None
