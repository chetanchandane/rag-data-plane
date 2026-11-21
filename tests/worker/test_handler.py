"""Unit tests for the SQS handler's decisions: process, dead-letter, or raise for a retry.

No AWS, no database: ingest and the DLQ are fakes passed to process_message(). Spans go to
the in-memory exporter (tests/conftest.py).
"""

import json
import uuid

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from shared.errors import PermanentError
from shared.jobs import IngestFileJob
from worker import handler as handler_module
from worker.handler import process_message
from worker.pipeline import IngestResult

RAW_BUCKET = "ragdp-raw-prod-123456789012"
KEY = f"raw/{uuid.uuid4()}/{uuid.uuid4()}/v1/source.pdf"


def message(**overrides: object) -> str:
    """An ingest job shaped exactly like the EventBridge input transformer's output."""
    body = {
        "schema_version": 1,
        "job_id": "evt-1",
        "job_type": "ingest",
        "source": "s3_event",
        "bucket": RAW_BUCKET,
        "key": KEY,
        "size_bytes": 1234,
        "etag": "abc",
        "emitted_at": "2026-11-19T12:00:00Z",
    }
    body.update(overrides)
    return json.dumps(body)


class FakeDlq:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, body: str, reason: str) -> None:
        if self.fail:
            raise ConnectionError("SQS unreachable")
        self.sent.append((body, reason))


class FakeIngest:
    def __init__(self, raises: Exception | None = None) -> None:
        self.jobs: list[IngestFileJob] = []
        self.raises = raises

    async def __call__(self, job: IngestFileJob) -> IngestResult:
        self.jobs.append(job)
        if self.raises:
            raise self.raises
        return IngestResult(outcome="indexed", chunk_count=3, embedded_now=3)


async def run(body: str, ingest: FakeIngest, dlq: FakeDlq) -> str:
    return await process_message(body, raw_bucket=RAW_BUCKET, run_ingest=ingest, send_to_dlq=dlq)


async def test_a_valid_job_is_ingested() -> None:
    ingest, dlq = FakeIngest(), FakeDlq()
    assert await run(message(), ingest, dlq) == "indexed"
    assert [job.key for job in ingest.jobs] == [KEY]
    assert dlq.sent == []


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        message(schema_version=2),  # unknown version
        message(tenant_id=str(uuid.uuid4())),  # S3 job that also claims an identity: forged
        message(key="raw/../../etc/passwd"),  # key outside the layout
    ],
)
async def test_an_invalid_message_goes_to_the_dlq_without_being_ingested(body: str) -> None:
    ingest, dlq = FakeIngest(), FakeDlq()
    assert await run(body, ingest, dlq) == "dead_lettered"
    assert ingest.jobs == []
    [(sent_body, reason)] = dlq.sent
    assert sent_body == body  # the original message, untouched, for a later redrive
    assert reason.startswith("invalid_message: ")


async def test_the_dlq_reason_never_echoes_the_message() -> None:
    # Message bodies are untrusted: the reason says where validation failed, not what was sent.
    ingest, dlq = FakeIngest(), FakeDlq()
    await run(message(key="raw/SECRET-LOOKING-VALUE"), ingest, dlq)
    [(_, reason)] = dlq.sent
    assert "SECRET-LOOKING-VALUE" not in reason
    assert reason == "invalid_message: value_error at ingest.key"


async def test_a_job_for_another_bucket_goes_to_the_dlq() -> None:
    ingest, dlq = FakeIngest(), FakeDlq()
    assert await run(message(bucket="someone-elses-bucket"), ingest, dlq) == "dead_lettered"
    assert ingest.jobs == []
    assert dlq.sent[0][1].startswith("wrong_bucket: ")


async def test_job_types_without_a_handler_yet_go_to_the_dlq() -> None:
    body = json.dumps(
        {
            "schema_version": 1,
            "job_id": "api-1",
            "job_type": "delete",
            "source": "api",
            "tenant_id": str(uuid.uuid4()),
            "doc_id": str(uuid.uuid4()),
            "emitted_at": "2026-11-19T12:00:00Z",
        }
    )
    ingest, dlq = FakeIngest(), FakeDlq()
    assert await run(body, ingest, dlq) == "dead_lettered"
    assert dlq.sent[0][1].startswith("unsupported_job_type: ")


async def test_a_permanent_ingest_error_goes_to_the_dlq_with_its_reason() -> None:
    ingest = FakeIngest(raises=PermanentError("sha256_mismatch", "file does not match"))
    dlq = FakeDlq()
    assert await run(message(), ingest, dlq) == "dead_lettered"
    assert dlq.sent == [(message(), "sha256_mismatch: file does not match")]


async def test_a_transient_error_is_raised_for_sqs_to_retry() -> None:
    ingest, dlq = FakeIngest(raises=TimeoutError("Bedrock timed out")), FakeDlq()
    with pytest.raises(TimeoutError):
        await run(message(), ingest, dlq)
    assert dlq.sent == []  # SQS moves it to the DLQ itself after 3 receives


async def test_a_failed_dlq_send_raises_so_nothing_is_lost() -> None:
    ingest = FakeIngest(raises=PermanentError("unparseable_pdf", "not a PDF"))
    with pytest.raises(ConnectionError):
        await run(message(), ingest, FakeDlq(fail=True))


# --- Tracing ---------------------------------------------------------------------------------
# handler() with its AWS wiring (_deps, _tracing) and the per-record work replaced by fakes,
# so these check only the span around each message and the flush before returning.


class FakeProvider:
    def __init__(self) -> None:
        self.flushes = 0

    def force_flush(self, timeout_millis: int) -> bool:
        self.flushes += 1
        return True


def sqs_event(body: str) -> dict:
    return {
        "Records": [
            {"messageId": "msg-1", "body": body, "attributes": {"ApproximateReceiveCount": "2"}}
        ]
    }


@pytest.fixture
def lambda_handler(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(handler_module, "_tracing", lambda: provider)
    monkeypatch.setattr(handler_module, "_deps", lambda: object())
    return provider


def test_each_message_is_one_ingest_span_flushed_before_returning(
    lambda_handler: FakeProvider, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def fake_record(body: str, deps: object) -> str:
        seen.append(trace.get_current_span().name)  # the span reaches asyncio.run's task
        return await run(body, FakeIngest(), FakeDlq())

    monkeypatch.setattr(handler_module, "_process_record", fake_record)
    handler_module.handler(sqs_event(message()), None)

    [span] = spans.get_finished_spans()
    assert span.name == "ingest" and seen == ["ingest"]
    assert span.attributes["messaging.message.id"] == "msg-1"
    assert span.attributes["messaging.sqs.receive_count"] == 2
    assert span.attributes["ingest.job_id"] == "evt-1"
    assert span.attributes["ingest.outcome"] == "indexed"
    assert lambda_handler.flushes == 1


def test_a_dead_lettered_message_records_the_reason(
    lambda_handler: FakeProvider, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_record(body: str, deps: object) -> str:
        return await run(body, FakeIngest(), FakeDlq())

    monkeypatch.setattr(handler_module, "_process_record", fake_record)
    handler_module.handler(sqs_event(message(bucket="another-bucket")), None)

    [span] = spans.get_finished_spans()
    assert span.attributes["ingest.outcome"] == "dead_lettered"
    assert span.attributes["ingest.dlq_reason"].startswith("wrong_bucket:")


def test_a_transient_error_marks_the_span_and_still_flushes(
    lambda_handler: FakeProvider, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_record(body: str, deps: object) -> str:
        return await run(body, FakeIngest(raises=TimeoutError("Bedrock timed out")), FakeDlq())

    monkeypatch.setattr(handler_module, "_process_record", fake_record)
    with pytest.raises(TimeoutError):
        handler_module.handler(sqs_event(message()), None)

    [span] = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]
    assert lambda_handler.flushes == 1
