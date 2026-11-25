"""Reranking (SPEC: Query path; DECISIONS 2026-11-22, "Step 15").

Recall's top 30 are reordered by a cross-encoder that reads the query and each chunk together.
The query logic sees only the Reranker interface, so a workload that can't send text to a
hosted API can swap in Bedrock Rerank or a self-hosted cross-encoder without touching it.
Today's implementation is Cohere's hosted /v2/rerank.

SPEC's budget: 1.5 s, no retry. On any failure, or while the reranker's circuit breaker is
open (api/breaker.py), the RRF order is kept and the caller adds degraded: ["reranker"].
Without a reranker there are no rerank scores, and the abstain gate must not compare RRF
scores against its threshold (api/answering.py).

Span: rerank (with breaker.state when there's a call to decide). Attributes are counts,
scores and outcomes, never the query or chunk text.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from opentelemetry import trace

from api.breaker import CircuitBreaker
from api.retrieval import RetrievedChunk

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"
# SPEC: Query path, "Dependencies, timeouts and fallbacks": reranker, 1.5 s, no retry. This
# is the whole call's deadline; httpx's own timeouts apply to each phase separately.
RERANK_TIMEOUT_SECONDS = 1.5


class RerankError(Exception):
    """The reranker gave no usable result. The message is safe to log: never the key."""


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """(index into documents, relevance score in [0, 1]) pairs, best first, at most top_n.
        Raises RerankError on any failure."""
        ...


class CohereReranker:
    """Cohere Rerank v2. The key is the tenant-wide Production key from SSM, never logged."""

    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str) -> None:
        self._http = http
        self._api_key = api_key
        self.model = model

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        try:
            async with asyncio.timeout(RERANK_TIMEOUT_SECONDS):
                response = await self._http.post(
                    COHERE_RERANK_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": documents,
                        "top_n": top_n,
                    },
                )
            response.raise_for_status()
            body = response.json()
            ranked = [(int(r["index"]), float(r["relevance_score"])) for r in body["results"]]
        except TimeoutError as error:
            raise RerankError("timeout") from error
        except httpx.HTTPStatusError as error:
            raise RerankError(f"http_{error.response.status_code}") from error
        except httpx.HTTPError as error:  # connection refused, reset, TLS ...
            raise RerankError(type(error).__name__) from error
        except (ValueError, KeyError, TypeError) as error:  # not the JSON we expect
            raise RerankError("malformed_response") from error

        # Indexes point into our list, so a bad one must not reach chunks[index].
        if any(not 0 <= index < len(documents) for index, _ in ranked):
            raise RerankError("index_out_of_range")
        # What Cohere bills: one search unit per query with up to 100 documents.
        billed = body.get("meta", {}).get("billed_units", {})
        trace.get_current_span().set_attribute(
            "rerank.search_units", float(billed.get("search_units", 0))
        )
        return ranked[:top_n]


def cohere_api_key(environ: Mapping[str, str], ssm: Any = None) -> str | None:
    """The key from COHERE_API_KEY (local .env) or from the SSM SecureString named by
    COHERE_API_KEY_PARAM (Lambda). None when neither is set: queries run without reranking."""
    key = environ.get("COHERE_API_KEY", "").strip()
    param = environ.get("COHERE_API_KEY_PARAM", "").strip()
    if key and param:
        raise ValueError("set COHERE_API_KEY or COHERE_API_KEY_PARAM, not both")
    if param:
        return ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    return key or None


@dataclass(frozen=True)
class RankedChunk:
    chunk: RetrievedChunk
    rerank_score: float | None  # None: the reranker didn't run, so the order is RRF's


@dataclass(frozen=True)
class Reranked:
    chunks: list[RankedChunk]
    degraded: bool  # True: the reranker failed or isn't configured; RRF order kept

    @property
    def top_score(self) -> float | None:
        """The best rerank score, or None if there is none to judge relevance by."""
        if self.degraded or not self.chunks:
            return None
        return self.chunks[0].rerank_score


async def rerank_chunks(
    reranker: Reranker | None,
    query: str,
    chunks: list[RetrievedChunk],
    *,
    top_n: int,
    breaker: CircuitBreaker,
) -> Reranked:
    """The top_n chunks in rerank order, or in RRF order if the reranker can't be used. Any
    RerankError counts toward the breaker; no key configured isn't a call, so it doesn't."""
    with tracer.start_as_current_span("rerank") as span:
        span.set_attributes({"rerank.candidates": len(chunks), "rerank.top_n": top_n})
        if not chunks:
            return Reranked(chunks=[], degraded=False)  # nothing to rank: no call, no cost
        if reranker is None:
            span.set_attribute("rerank.error", "not_configured")
            return _rrf_order(chunks, top_n)
        with breaker.attempt() as attempt:
            span.set_attribute("breaker.state", attempt.state)
            if not attempt.allowed:
                span.set_attribute("rerank.error", "breaker_open")
                return _rrf_order(chunks, top_n)
            try:
                ranked = await reranker.rerank(query, [c.content for c in chunks], top_n)
            except RerankError as error:
                attempt.failed()
                log.warning("rerank failed, keeping RRF order: %s", error)
                span.set_attribute("rerank.error", str(error))
                return _rrf_order(chunks, top_n)
            attempt.succeeded()

        result = Reranked(
            chunks=[RankedChunk(chunks[index], score) for index, score in ranked], degraded=False
        )
        if result.top_score is not None:
            span.set_attribute("rerank.top_score", result.top_score)
        return result


def _rrf_order(chunks: list[RetrievedChunk], top_n: int) -> Reranked:
    return Reranked(chunks=[RankedChunk(c, None) for c in chunks[:top_n]], degraded=True)
