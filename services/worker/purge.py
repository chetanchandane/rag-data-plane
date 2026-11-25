"""The delete job: purge a deleted document (SPEC: Ingestion, "Deletes"; DECISIONS 2026-11-24,
"Step 21").

DELETE /v1/documents/{id} has already hidden the document (deleted_at, epoch bump) before
this runs, so nothing here affects what a query can see. This only removes the data:

  1. Check: the document exists in the job's tenant and is deleted (else PermanentError),
     and no worker holds a live lease on a version (else PurgeDeferred, transient).
  2. Delete each version's S3 object.
  3. One transaction: clear active_version and the title, delete chunks, then versions.
     The documents row stays as a tombstone.

Every step is idempotent, so a duplicate delivery or a crash between steps is safe: a rerun
deletes the same S3 keys again (a no-op for a missing key) and then the rows, and a run
after a complete purge finds nothing to remove. S3 goes before the rows because the rows are
the list of keys to delete.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from opentelemetry import trace

from shared.errors import PermanentError
from shared.jobs import parse_s3_key
from worker import store

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

DeleteObject = Callable[[str], None]  # (s3 key) -> None; the bucket is the raw bucket


@dataclass(frozen=True)
class PurgeResult:
    objects: int  # S3 delete calls made (a key already gone counts too)
    versions: int
    chunks: int


async def purge_document(
    pool: asyncpg.Pool, tenant_id: UUID, doc_id: UUID, *, delete_object: DeleteObject
) -> PurgeResult:
    """Purge one deleted document. Raises PermanentError or store.PurgeDeferred (above)."""
    trace.get_current_span().set_attributes(
        {"ingest.tenant_id": str(tenant_id), "ingest.doc_id": str(doc_id)}
    )
    with tracer.start_as_current_span("purge") as span:
        async with pool.acquire() as conn:
            keys = await store.purge_targets(conn, tenant_id, doc_id)

        # A stored key is ours, but deleting is irreversible: refuse a key that doesn't
        # belong to this tenant and document rather than trust the row blindly. Every key is
        # checked before the first delete, so a bad row stops the purge with nothing removed.
        for key in keys:
            _check_key(key, tenant_id, doc_id)
        # One call per version: a document has a handful. DeleteObjects would batch them but
        # adds per-key partial failures to handle.
        for key in keys:
            await asyncio.to_thread(delete_object, key)

        async with pool.acquire() as conn:
            rows = await store.purge_rows(conn, tenant_id, doc_id)

        result = PurgeResult(objects=len(keys), versions=rows.versions, chunks=rows.chunks)
        span.set_attributes(
            {
                "purge.objects": result.objects,
                "purge.versions": result.versions,
                "purge.chunks": result.chunks,
            }
        )
    log.info("purged %s: %s", doc_id, result)
    return result


def _check_key(key: str, tenant_id: UUID, doc_id: UUID) -> None:
    try:
        parts = parse_s3_key(key)
    except ValueError as exc:
        raise PermanentError("bad_stored_key", "a version's s3_key is not in the layout") from exc
    if (parts.tenant_id, parts.doc_id) != (tenant_id, doc_id):
        raise PermanentError("bad_stored_key", "a version's s3_key names another document")
