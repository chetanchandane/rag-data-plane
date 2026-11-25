"""One ingest job, start to finish (SPEC: Ingestion, "Worker steps" and "Failure handling").

ingest_version() is shared by the local entry point (step 5) and the SQS handler (step 8).
The caller supplies where the bytes come from (a local file or S3) and how to embed, so tests
can pass fakes and nothing here knows about Lambda or SQS.

On failure after a successful claim, the version is marked 'failed' with a short reason and
the exception is re-raised. The caller decides what that means: PermanentError goes to the
DLQ at once, anything else is transient and left for SQS to retry.

Each stage is an OpenTelemetry span (SPEC: Architecture, Telemetry) and is also timed into
IngestResult.seconds. Span attributes are ids and counts, never document text.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import batched
from typing import Literal

import asyncpg
from opentelemetry import trace

from shared.errors import PermanentError
from shared.jobs import parse_s3_key
from worker import store
from worker.chunking import CHUNKER_VERSION, chunk
from worker.pdf import PARSER_VERSION, parse, verify_hash

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

EMBED_BATCH_SIZE = 32

LoadSource = Callable[[], bytes]
DeleteSource = Callable[[], None]
Embed = Callable[[list[str]], list[list[float]]]


@dataclass
class IngestResult:
    outcome: Literal["indexed", "superseded", "skipped", "deleted"]
    chunk_count: int = 0
    embedded_now: int = 0  # chunks embedded in this run (fewer than chunk_count on a resume)
    seconds: dict[str, float] = field(default_factory=dict)  # per stage


@contextmanager
def _stage(
    seconds: dict[str, float], name: str, attributes: dict[str, int | str] | None = None
) -> Iterator[trace.Span]:
    """One pipeline stage: a span with that name, and its duration in `seconds`. A stage
    that raises gets no duration; its span records the exception."""
    start = time.perf_counter()
    with tracer.start_as_current_span(name, attributes=attributes) as span:
        yield span
    seconds[name] = round(time.perf_counter() - start, 2)


async def ingest_version(
    pool: asyncpg.Pool,
    s3_key: str,
    *,
    load_source: LoadSource,
    embed: Embed,
    embedding_model: str,
    delete_source: DeleteSource | None = None,
) -> IngestResult:
    """Claim, verify, parse, chunk, embed, insert and activate one version.

    delete_source removes the uploaded file. It's called only when the document is deleted:
    the file may have been uploaded after the purge ran (a presigned URL lives 15 minutes),
    and nothing else would ever remove it. None where there is no stored file (local runs).
    """
    try:
        parts = parse_s3_key(s3_key)
    except ValueError as exc:
        raise PermanentError("bad_key", str(exc)) from exc

    # On the caller's span (the handler's `ingest`), so every trace can be found by document.
    trace.get_current_span().set_attributes(
        {
            "ingest.tenant_id": str(parts.tenant_id),
            "ingest.doc_id": str(parts.doc_id),
            "ingest.version": parts.version,
        }
    )
    seconds: dict[str, float] = {}
    deleted = False
    with _stage(seconds, "claim") as span:
        try:
            async with pool.acquire() as conn:
                claim = await store.claim(conn, parts, s3_key)
        except store.DocumentDeleted:
            claim, deleted = None, True
        span.set_attributes(
            {"ingest.claimed": claim is not None, "ingest.document_deleted": deleted}
        )
    if deleted:
        log.info("document %s is deleted: not ingesting v%s", parts.doc_id, parts.version)
        if delete_source is not None:
            await asyncio.to_thread(delete_source)
        return IngestResult(outcome="deleted")
    if claim is None:
        log.info("version %s v%s is done or held by another worker", parts.doc_id, parts.version)
        return IngestResult(outcome="skipped")

    try:
        return await _process(pool, claim, load_source, embed, embedding_model, seconds)
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
    seconds: dict[str, float],
) -> IngestResult:
    with _stage(seconds, "load_and_verify") as span:
        data = await asyncio.to_thread(load_source)
        span.set_attribute("ingest.bytes", len(data))
        verify_hash(data, claim.sha256)

    # Parsing is CPU-bound; a thread keeps the event loop free.
    with _stage(seconds, "parse") as span:
        blocks = await asyncio.to_thread(parse, data)
        span.set_attributes(
            {
                "ingest.pages": max((b.page for b in blocks), default=0),
                "ingest.blocks": len(blocks),
            }
        )

    with _stage(seconds, "chunk") as span:
        chunks = chunk(blocks)
        span.set_attribute("ingest.chunks", len(chunks))

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
    batches = list(batched(missing, EMBED_BATCH_SIZE))
    embed_attributes = {"ingest.chunks_to_embed": len(missing), "ingest.batches": len(batches)}
    with _stage(seconds, "embed", embed_attributes):
        for batch in batches:
            with tracer.start_as_current_span(
                "embed_batch", attributes={"ingest.texts": len(batch)}
            ):
                vectors = await asyncio.to_thread(embed, [c.content for c in batch])
            with tracer.start_as_current_span("insert_chunks"):
                async with pool.acquire() as conn:
                    await store.insert_chunks(conn, claim, batch, vectors)

    with _stage(seconds, "activate") as span:
        async with pool.acquire() as conn:
            activation = await store.activate(conn, claim)
            if activation != "deleted":  # a deleted document's chunks are the purge's job
                await store.cleanup_superseded(conn, claim)
        span.set_attribute("ingest.activated", activation == "activated")
        span.set_attribute("ingest.activation", activation)

    outcomes = {"activated": "indexed", "superseded": "superseded", "deleted": "deleted"}
    return IngestResult(
        outcome=outcomes[activation],
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
