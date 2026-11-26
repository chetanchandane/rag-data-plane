"""Unit tests for the SQS handler's decisions: process, dead-letter, or raise for a retry.

No AWS, no database: ingest, the purge and the DLQ are fakes passed to process_message().
Spans go to the in-memory exporter (tests/conftest.py).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from shared.errors import PermanentError
from shared.jobs import DeleteJob, IngestFileJob
from tests.emf import dimensions, emitted, metric_names
from worker import handler as handler_module
from worker.handler import process_message
from worker.pipeline import IngestResult
from worker.purge import PurgeResult
from worker.store import PurgeDeferred

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
    def __init__(self, raises: Exception | None = None, result: IngestResult | None = None) -> None:
        self.jobs: list[IngestFileJob] = []
        self.raises = raises
        self.result = result or IngestResult(outcome="indexed", chunk_count=3, embedded_now=3)

    async def __call__(self, job: IngestFileJob) -> IngestResult:
        self.jobs.append(job)
        if self.raises:
            raise self.raises
        return self.result


class FakePurge:
    def __init__(self, raises: Exception | None = None) -> None:
        self.jobs: list[DeleteJob] = []
        self.raises = raises

    async def __call__(self, job: DeleteJob) -> PurgeResult:
        self.jobs.append(job)
        if self.raises:
            raise self.raises
        return PurgeResult(objects=2, versions=2, chunks=9)


async def run(body: str, ingest: FakeIngest, dlq: FakeDlq, purge: FakePurge | None = None) -> str:
    return await process_message(
        body,
        raw_bucket=RAW_BUCKET,
        run_ingest=ingest,
        run_purge=purge or FakePurge(),
        send_to_dlq=dlq,
    )


def api_message(job_type: str, **overrides: object) -> str:
    body = {
        "schema_version": 1,
        "job_id": "api-1",
        "job_type": job_type,
        "source": "api",
        "tenant_id": str(uuid.uuid4()),
        "doc_id": str(uuid.uuid4()),
        "emitted_at": "2026-11-24T12:00:00Z",
    }
    body.update(overrides)
    return json.dumps(body)


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


async def test_reindex_jobs_have_no_handler_yet_and_go_to_the_dlq() -> None:
    ingest, dlq, purge = FakeIngest(), FakeDlq(), FakePurge()
    assert await run(api_message("reindex", version=1), ingest, dlq, purge) == "dead_lettered"
    assert dlq.sent[0][1].startswith("unsupported_job_type: ")
    assert ingest.jobs == [] and purge.jobs == []


# --- delete jobs ---------------------------------------------------------------------------


async def test_a_delete_job_runs_the_purge() -> None:
    body = api_message("delete")
    ingest, dlq, purge = FakeIngest(), FakeDlq(), FakePurge()
    assert await run(body, ingest, dlq, purge) == "purged"
    [job] = purge.jobs
    assert str(job.doc_id) == json.loads(body)["doc_id"]
    assert str(job.tenant_id) == json.loads(body)["tenant_id"]
    assert ingest.jobs == [] and dlq.sent == []


async def test_a_permanent_purge_error_goes_to_the_dlq() -> None:
    # e.g. a delete job for a document that isn't deleted: never purged, kept for a look.
    body = api_message("delete")
    purge = FakePurge(raises=PermanentError("not_deleted", "refusing to purge"))
    dlq = FakeDlq()
    assert await run(body, FakeIngest(), dlq, purge) == "dead_lettered"
    assert dlq.sent == [(body, "not_deleted: refusing to purge")]


async def test_a_deferred_purge_is_raised_for_sqs_to_bring_back() -> None:
    purge = FakePurge(raises=PurgeDeferred("1 version still being ingested"))
    dlq = FakeDlq()
    with pytest.raises(PurgeDeferred):
        await run(api_message("delete"), FakeIngest(), dlq, purge)
    assert dlq.sent == []


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


# --- ingestion freshness (step 25) -----------------------------------------------------------


def uploaded(seconds_ago: float) -> str:
    """A message whose S3 event (emitted_at) is seconds_ago old."""
    at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return message(emitted_at=at.isoformat().replace("+00:00", "Z"))


async def test_an_indexed_document_emits_its_freshness(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-worker")
    result = IngestResult(outcome="indexed", chunk_count=14, embedded_now=14, pages=12)
    assert await run(uploaded(42), FakeIngest(result=result), FakeDlq()) == "indexed"
    [rec] = emitted(capsys)
    assert metric_names(rec) == {"IngestFreshness"}
    assert dimensions(rec) == [[]]  # one metric, no dimensions
    assert 42 <= rec["IngestFreshness"] < 52  # seconds since the upload, at least 42
    assert rec["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Seconds"
    assert (rec["pages"], rec["chunks"], rec["job_id"]) == (12, 14, "evt-1")


async def test_an_upload_time_without_a_timezone_is_read_as_utc(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-worker")
    at = (datetime.now(UTC) - timedelta(seconds=30)).replace(tzinfo=None)
    body = message(emitted_at=at.isoformat())  # no Z, no offset
    result = IngestResult(outcome="indexed", pages=2)
    assert await run(body, FakeIngest(result=result), FakeDlq()) == "indexed"
    [rec] = emitted(capsys)
    assert 30 <= rec["IngestFreshness"] < 40


async def test_freshness_includes_exactly_twenty_pages(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-worker")
    result = IngestResult(outcome="indexed", pages=handler_module.FRESHNESS_MAX_PAGES)
    await run(uploaded(5), FakeIngest(result=result), FakeDlq())
    assert len(emitted(capsys)) == 1


async def test_documents_over_twenty_pages_are_outside_the_slo(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-worker")
    result = IngestResult(outcome="indexed", pages=handler_module.FRESHNESS_MAX_PAGES + 1)
    assert await run(uploaded(5), FakeIngest(result=result), FakeDlq()) == "indexed"
    assert emitted(capsys) == []


@pytest.mark.parametrize("outcome", ["skipped", "superseded", "deleted"])
async def test_only_the_delivery_that_indexes_emits_freshness(outcome, monkeypatch, capsys) -> None:
    # skipped: a duplicate delivery of a version that's already done.
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-worker")
    result = IngestResult(outcome=outcome, pages=3)
    assert await run(uploaded(5), FakeIngest(result=result), FakeDlq()) == outcome
    assert emitted(capsys) == []


async def test_a_dead_lettered_job_emits_no_freshness(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-worker")
    ingest = FakeIngest(raises=PermanentError("hash_mismatch", "bytes differ"))
    assert await run(uploaded(5), ingest, FakeDlq()) == "dead_lettered"
    assert emitted(capsys) == []
