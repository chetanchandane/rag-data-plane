"""The /v1/query answer cache (SPEC: Query path, "Cache"; DECISIONS 2026-11-25, "Step 22").

Exact match: key = SHA-256 of tenant, role level, normalized query, tenant epoch and answer
version (the prompt version plus the models and settings that shape an answer). Entries live
in the unlogged query_cache table for 24 hours. Only validated, non-degraded answers are
stored.

Why the epoch makes it safe: every change to what a tenant's queries can see (an activation,
a delete) bumps the epoch in the same transaction (shared.db.BUMP_EPOCH_SQL). The key uses the
epoch read *before* recall, and the store reuses that key instead of reading the epoch again.
Recall runs after the transaction that set that epoch committed, so an answer stored under
epoch E never contains anything hidden before E took effect. If a delete commits mid-request,
the answer lands under E, and lookups have already moved to E + 1.

Isolation is twofold: tenant and role level are in the key, and query_cache's RLS policy
matches only rows with this tenant and this exact role level.

Spans: cache_lookup (cache.found: a live row exists, cache.epoch) and cache_store
(cache.error if it failed). Whether the request was served from the cache is cache.hit on the
route's query span. Never the query or the answer.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from opentelemetry import trace

from api.answering import PROMPT_VERSION, QueryAnswer
from api.config import Settings
from shared.db import DB_UNAVAILABLE, rls_transaction

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# The tenant filter repeats what RLS does: if a policy were ever wrong, fetchval would take
# another tenant's epoch from an arbitrary row. No row yet (nothing ever activated) is 0.
_EPOCH_SQL = "SELECT epoch FROM tenant_index_state WHERE tenant_id = $1"
_LOOKUP_SQL = "SELECT response FROM query_cache WHERE cache_key = $1 AND expires_at > now()"
# DO UPDATE, not DO NOTHING: an expired entry under the same key (same epoch, a day later)
# is replaced. Two identical misses at once both store; the last write wins, and both
# answers were valid for this key.
_STORE_SQL = """
INSERT INTO query_cache (cache_key, tenant_id, role_level, response, expires_at)
VALUES ($1, $2, $3, $4::jsonb, now() + interval '24 hours')
ON CONFLICT (cache_key) DO UPDATE
SET response = EXCLUDED.response, expires_at = EXCLUDED.expires_at
"""
# Every epoch bump orphans a tenant's entries (no lookup asks for the old epoch again). The
# compute's suspend empties the table, but under steady traffic it doesn't suspend, so each
# store also removes this tenant and role level's expired rows (RLS scopes the DELETE). That
# bounds the table to about a day of entries with no scheduled job.
_EXPIRE_SQL = "DELETE FROM query_cache WHERE expires_at <= now()"


@dataclass(frozen=True)
class Lookup:
    key: str  # reused by store(): it carries the epoch read before recall
    epoch: int
    response: dict[str, Any] | None  # None: a miss


def normalize_query(query: str) -> str:
    """Whitespace only: trimmed, runs collapsed to one space. /v1/query sends this text, not
    the raw one, through recall and the prompt too, so two requests with the same key get
    exactly the same inputs. Case is kept because the lexical weight is case-sensitive
    ("ICH E10" and "ich e10" can recall differently)."""
    return " ".join(query.split())


def answer_version(settings: Settings) -> str:
    """The prompt version plus every setting that changes an answer, so a deploy that
    changes a model or a threshold never serves the old configuration's answers."""
    return json.dumps(
        {
            "prompt": PROMPT_VERSION,
            "embedding": settings.embedding_model_id,
            "rerank": settings.rerank_model_id,
            "primary": settings.llm_primary_model_id,
            "primary_effort": settings.llm_primary_effort,
            "secondary": settings.llm_secondary_model_id,
            "abstain_threshold": settings.abstain_threshold,
            "context_chunks": settings.llm_context_chunks,
        },
        sort_keys=True,
    )


def cache_key(tenant_id: UUID, role_level: int, query: str, epoch: int, version: str) -> str:
    """SHA-256 hex (64 chars, the column's width). The parts are JSON-encoded as a list, so
    no query text can make two different sets of parts produce the same string."""
    parts = [str(tenant_id), role_level, normalize_query(query), epoch, version]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def is_cacheable(result: QueryAnswer) -> bool:
    """SPEC: only validated, non-degraded answers. An answer is validated by the time it
    exists (answering.py returns none that failed the citation check). Abstains are never
    stored: a model's not_in_documents would be pinned for a day. Nor is citation_retry,
    which SPEC counts as degraded."""
    return not result.abstained and not result.degraded


async def lookup(
    pool: asyncpg.Pool, tenant_id: UUID, role_level: int, query: str, version: str
) -> Lookup:
    """The key for this request and, on a hit, the stored response. A database error is
    raised, so the request gets a 503 like any other Postgres failure."""
    with tracer.start_as_current_span("cache_lookup") as span:
        async with pool.acquire() as conn, rls_transaction(conn, tenant_id, role_level):
            epoch = await conn.fetchval(_EPOCH_SQL, tenant_id) or 0
            key = cache_key(tenant_id, role_level, query, epoch, version)
            raw = await conn.fetchval(_LOOKUP_SQL, key)
        span.set_attributes({"cache.found": raw is not None, "cache.epoch": epoch})
    return Lookup(key, epoch, json.loads(raw) if raw is not None else None)


async def store(
    pool: asyncpg.Pool, tenant_id: UUID, role_level: int, key: str, response: dict[str, Any]
) -> bool:
    """Store response under key, from lookup(). Best effort: the answer is already paid for,
    so any database error leaves it uncached instead of failing the request. Postgres being
    unavailable is a WARNING. Anything else Postgres rejects (an RLS violation, a value jsonb
    won't take) is a bug, so it's logged as an ERROR with its traceback. Errors that aren't
    the database's (a response json can't encode) still raise."""
    payload = json.dumps(response)
    with tracer.start_as_current_span("cache_store") as span:
        try:
            async with pool.acquire() as conn, rls_transaction(conn, tenant_id, role_level):
                await conn.execute(_EXPIRE_SQL)
                await conn.execute(_STORE_SQL, key, tenant_id, role_level, payload)
        except DB_UNAVAILABLE as error:
            log.warning("query cache store skipped: %s", type(error).__name__)
            span.set_attribute("cache.error", type(error).__name__)
            return False
        except asyncpg.PostgresError as error:
            log.exception("query cache store failed")
            span.set_attribute("cache.error", type(error).__name__)
            return False
    return True
