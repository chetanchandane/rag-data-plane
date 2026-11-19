"""Unit tests for the SQS handler's decisions: process, dead-letter, or raise for a retry.

No AWS, no database: ingest and the DLQ are fakes passed to process_message().
"""

import json
import uuid

import pytest

from shared.errors import PermanentError
from shared.jobs import IngestFileJob
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
