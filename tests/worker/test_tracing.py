"""Step 10e: the pipeline's spans (SPEC: Architecture, Telemetry; Slices, "Traces show parse,
chunk and embed spans with durations").

Real ingest_version() against the Neon dev branch as app_ingest, a fake embedder (no
Bedrock), and spans captured in memory (tests/conftest.py): nothing is sent anywhere.
"""

import uuid

import asyncpg
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from shared.errors import PermanentError
from tests.worker.dbhelpers import registered
from tests.worker.test_guarantees import RecordingEmbed, ingest, long_pdf
from worker import pipeline

pytestmark = pytest.mark.db

STAGES = ["claim", "load_and_verify", "parse", "chunk", "embed", "activate"]


async def traced_ingest(
    pool: asyncpg.Pool, key: str, data: bytes, embed: RecordingEmbed
) -> pipeline.IngestResult:
    """ingest_version under a root span, the way the Lambda handler calls it."""
    with trace.get_tracer(__name__).start_as_current_span("ingest"):
        return await ingest(pool, key, data, embed)


def by_name(spans: InMemorySpanExporter) -> dict[str, list[ReadableSpan]]:
    named: dict[str, list[ReadableSpan]] = {}
    for span in spans.get_finished_spans():
        named.setdefault(span.name, []).append(span)
    return named


async def test_an_ingest_is_one_trace_with_a_span_per_stage(
    pool: asyncpg.Pool,
    tenant: uuid.UUID,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "EMBED_BATCH_SIZE", 4)
    parts, key = await registered(pool, tenant, long_pdf())
    embed = RecordingEmbed()

    result = await traced_ingest(pool, key, long_pdf(), embed)

    named = by_name(spans)
    [root] = named["ingest"]
    assert {span.context.trace_id for span in spans.get_finished_spans()} == {root.context.trace_id}
    # Each stage is a direct child of the root, in pipeline order, and has a duration.
    stages = sorted(
        (
            s
            for s in spans.get_finished_spans()
            if s.parent and s.parent.span_id == root.context.span_id
        ),
        key=lambda s: s.start_time,
    )
    assert [s.name for s in stages] == STAGES
    assert all(s.end_time > s.start_time for s in stages)
    assert list(result.seconds) == STAGES

    # The document is findable from the root span.
    assert root.attributes["ingest.doc_id"] == str(parts.doc_id)
    assert root.attributes["ingest.version"] == 1

    [parse], [chunk], [embed_span] = named["parse"], named["chunk"], named["embed"]
    assert parse.attributes["ingest.pages"] == 30
    assert chunk.attributes["ingest.chunks"] == result.chunk_count
    batches = -(-result.chunk_count // 4)  # ceiling division
    assert embed_span.attributes["ingest.batches"] == batches
    # One Bedrock call and one insert per batch, both under `embed`.
    for name in ("embed_batch", "insert_chunks"):
        assert len(named[name]) == batches
        assert {s.parent.span_id for s in named[name]} == {embed_span.context.span_id}


async def test_span_attributes_never_hold_document_text(
    pool: asyncpg.Pool, tenant: uuid.UUID, spans: InMemorySpanExporter
) -> None:
    _, key = await registered(pool, tenant, long_pdf())
    embed = RecordingEmbed()
    await traced_ingest(pool, key, long_pdf(), embed)

    values = [
        str(value) for span in spans.get_finished_spans() for value in span.attributes.values()
    ]
    assert embed.texts  # something was embedded
    assert not any("zero trust" in value for value in values)
    assert max(len(value) for value in values) < 100  # ids, counts and short outcomes


async def test_a_job_already_done_shows_an_unclaimed_claim_span(
    pool: asyncpg.Pool, tenant: uuid.UUID, spans: InMemorySpanExporter
) -> None:
    _, key = await registered(pool, tenant, long_pdf())
    await ingest(pool, key, long_pdf(), RecordingEmbed())
    spans.clear()

    result = await traced_ingest(pool, key, long_pdf(), RecordingEmbed())

    assert result.outcome == "skipped"
    named = by_name(spans)
    assert named["claim"][0].attributes["ingest.claimed"] is False
    assert "parse" not in named


async def test_a_failed_stage_is_an_error_span(
    pool: asyncpg.Pool, tenant: uuid.UUID, spans: InMemorySpanExporter
) -> None:
    _, key = await registered(pool, tenant, long_pdf(), sha256="0" * 64)

    with pytest.raises(PermanentError):
        await traced_ingest(pool, key, long_pdf(), RecordingEmbed())

    named = by_name(spans)
    [verify] = named["load_and_verify"]
    assert verify.status.status_code == StatusCode.ERROR
    assert "parse" not in named  # a wrong hash is never parsed
