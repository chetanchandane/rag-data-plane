"""One ingest job, start to finish (SPEC: Ingestion, "Worker steps" and "Failure handling").

ingest_version() is shared by the local entry point (step 5) and the SQS handler (step 8).
The caller supplies where the bytes come from (a local file or S3) and how to embed, so tests
can pass fakes and nothing here knows about Lambda or SQS.

On failure after a successful claim, the version is marked 'failed' with a short reason and
the exception is re-raised. The caller decides what that means: PermanentError goes to the
DLQ at once, anything else is transient and left for SQS to retry.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import batched
from typing import Literal

import asyncpg

from shared.errors import PermanentError
from shared.jobs import parse_s3_key
from worker import store
from worker.chunking import CHUNKER_VERSION, chunk
from worker.pdf import PARSER_VERSION, parse, verify_hash

log = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32

LoadSource = Callable[[], bytes]
Embed = Callable[[list[str]], list[list[float]]]


@dataclass
class IngestResult:
    outcome: Literal["indexed", "superseded", "skipped"]
    chunk_count: int = 0
    embedded_now: int = 0  # chunks embedded in this run (fewer than chunk_count on a resume)
    seconds: dict[str, float] = field(default_factory=dict)  # per stage


async def ingest_version(
    pool: asyncpg.Pool,
    s3_key: str,
    *,
    load_source: LoadSource,
    embed: Embed,
    embedding_model: str,
) -> IngestResult:
    """Claim, verify, parse, chunk, embed, insert and activate one version."""
    try:
        parts = parse_s3_key(s3_key)
    except ValueError as exc:
        raise PermanentError("bad_key", str(exc)) from exc

    async with pool.acquire() as conn:
        claim = await store.claim(conn, parts, s3_key)
    if claim is None:
        log.info("version %s v%s is done or held by another worker", parts.doc_id, parts.version)
        return IngestResult(outcome="skipped")

    try:
        return await _process(pool, claim, load_source, embed, embedding_model)
    except Exception as exc:
        # Exception, not BaseException: a killed process (Ctrl-C, Lambda timeout) records
        # nothing, like a real crash. Its lease expires and the next delivery resumes.
        await _record_failure(pool, claim, exc)
        raise


async def _process(
    pool: asyncpg.Pool,
    claim: store.Claim,
    load_source: LoadSource,
    embed: Embed,
    embedding_model: str,
) -> IngestResult:
    seconds: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(stage: str) -> None:
        nonlocal clock
        now = time.perf_counter()
        seconds[stage] = round(now - clock, 2)
        clock = now

    data = await asyncio.to_thread(load_source)
    verify_hash(data, claim.sha256)
    lap("load_and_verify")

    # Parsing is CPU-bound; a thread keeps the event loop free.
    chunks = chunk(await asyncio.to_thread(parse, data))
    lap("parse_and_chunk")

    async with pool.acquire() as conn:
        await store.start_embedding(
            conn,
            claim,
            parser=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model=embedding_model,
            chunk_count=len(chunks),
        )
        done = await store.existing_chunk_indexes(conn, claim)

    # Resume: embed only what an earlier attempt didn't store. Each batch is written before
    # the next is embedded, so a crash loses at most one batch.
    missing = [c for c in chunks if c.index not in done]
    for batch in batched(missing, EMBED_BATCH_SIZE):
        vectors = await asyncio.to_thread(embed, [c.content for c in batch])
        async with pool.acquire() as conn:
            await store.insert_chunks(conn, claim, batch, vectors)
    lap("embed_and_insert")

    async with pool.acquire() as conn:
        activated = await store.activate(conn, claim)
        await store.cleanup_superseded(conn, claim)
    lap("activate_and_cleanup")

    return IngestResult(
        outcome="indexed" if activated else "superseded",
        chunk_count=len(chunks),
        embedded_now=len(missing),
        seconds=seconds,
    )


async def _record_failure(pool: asyncpg.Pool, claim: store.Claim, exc: Exception) -> None:
    """Mark the claimed version failed. Never hides the original error."""
    if isinstance(exc, PermanentError):
        reason = exc.reason
    else:
        # Exception type and message only: never document content.
        reason = f"transient: {type(exc).__name__}: {exc}"
    try:
        async with pool.acquire() as conn:
            if not await store.mark_failed(conn, claim, reason):
                log.warning("not marking %s v%s failed: claim lost", claim.doc_id, claim.version)
    except Exception:
        # The DB may be the thing that failed. Log it; the caller still gets the original.
        log.exception("could not mark %s v%s failed", claim.doc_id, claim.version)
