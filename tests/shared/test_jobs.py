"""Unit tests for the IngestJob v1 message contract (no network)."""

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from shared.jobs import DeleteJob, IngestFileJob, ReindexJob, S3KeyParts, parse_ingest_job

TENANT = "7d1e0c4a-5b6f-4a8e-9c2d-1e3f5a7b9c0d"
DOC = "a3f2b1c4-d5e6-4f70-8192-a3b4c5d6e7f8"
GOOD_KEY = f"raw/{TENANT}/{DOC}/v2/source.pdf"

# The InputTemplate from SPEC "EventBridge input transformer", verbatim.
EVENTBRIDGE_TEMPLATE = (
    '{"schema_version":1,"job_id":"<id>","job_type":"ingest","source":"s3_event",'
    '"bucket":"<bucket>","key":"<key>","size_bytes":<size>,"etag":"<etag>",'
    '"emitted_at":"<time>"}'
)


def eventbridge_body(key: str = GOOD_KEY) -> str:
    """Render the template the way EventBridge does: plain text substitution."""
    values = {
        "<id>": "5f0c9a1e-2b7d-4c1a-9e3f-0a1b2c3d4e5f",
        "<bucket>": "ragdp-raw-prod",
        "<key>": key,
        "<size>": "482113",
        "<etag>": "9b2cf535f27731c974343645a3985328",
        "<time>": "2026-11-15T15:04:05Z",
    }
    body = EVENTBRIDGE_TEMPLATE
    for placeholder, value in values.items():
        body = body.replace(placeholder, value)
    return body


def ingest_message(**overrides: object) -> dict:
    return json.loads(eventbridge_body()) | overrides


def api_message(job_type: str, **overrides: object) -> dict:
    message = {
        "schema_version": 1,
        "job_id": "0b9f6a3e-8c1d-4e2f-a5b7-6c8d9e0f1a2b",
        "job_type": job_type,
        "source": "api",
        "emitted_at": "2026-11-15T15:04:05Z",
        "tenant_id": TENANT,
        "doc_id": DOC,
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    }
    if job_type == "reindex":
        message["version"] = 2
    return message | overrides


def without(message: dict, field: str) -> dict:
    return {k: v for k, v in message.items() if k != field}


def parse(message: dict):
    return parse_ingest_job(json.dumps(message))


def rejected_fields(message: dict) -> set[str]:
    """Parse a message that must be invalid; return the names of the fields at fault."""
    with pytest.raises(ValidationError) as exc_info:
        parse(message)
    # loc is (job_type tag, field) for field errors, or (tag,) / () for union-level errors.
    return {str(error["loc"][-1]) for error in exc_info.value.errors() if error["loc"]}


# --- valid messages ------------------------------------------------------------------------


def test_eventbridge_body_is_a_valid_ingest_job() -> None:
    job = parse_ingest_job(eventbridge_body())
    assert isinstance(job, IngestFileJob)
    assert job.size_bytes == 482113
    assert job.tenant_id is None  # identity comes only from the key
    assert job.key_parts() == S3KeyParts(UUID(TENANT), UUID(DOC), 2)


def test_spec_example_with_explicit_nulls_is_valid() -> None:
    job = parse(ingest_message(tenant_id=None, doc_id=None, version=None, traceparent=None))
    assert isinstance(job, IngestFileJob)


def test_reindex_job_is_valid() -> None:
    job = parse(api_message("reindex"))
    assert isinstance(job, ReindexJob)
    assert (job.tenant_id, job.doc_id, job.version) == (UUID(TENANT), UUID(DOC), 2)


@pytest.mark.parametrize("extra", [{}, {"version": None}])
def test_delete_job_is_valid_without_a_version(extra: dict) -> None:
    job = parse(api_message("delete", **extra))
    assert isinstance(job, DeleteJob)
    assert (job.tenant_id, job.doc_id) == (UUID(TENANT), UUID(DOC))


def test_unknown_fields_are_ignored() -> None:
    # "Add fields, never repurpose them": a newer producer must not break this worker.
    parse(ingest_message(added_in_a_later_release=True))


# --- unknown schema_version / job_type -----------------------------------------------------


@pytest.mark.parametrize("version", [0, 2])
@pytest.mark.parametrize(
    "message", [ingest_message(), api_message("reindex"), api_message("delete")]
)
def test_unknown_schema_version_is_rejected(message: dict, version: int) -> None:
    assert rejected_fields(message | {"schema_version": version}) == {"schema_version"}


def test_unknown_job_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        parse(ingest_message(job_type="purge"))


def test_missing_job_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Unable to extract tag"):
        parse(without(ingest_message(), "job_type"))


# --- bad keys ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        f"uploads/{TENANT}/{DOC}/v2/source.pdf",  # wrong prefix
        f"/raw/{TENANT}/{DOC}/v2/source.pdf",  # leading slash
        f"raw/{TENANT.upper()}/{DOC}/v2/source.pdf",  # non-canonical UUID
        f"raw/not-a-uuid/{DOC}/v2/source.pdf",
        f"raw/{TENANT}/{DOC}/v0/source.pdf",  # versions start at 1
        f"raw/{TENANT}/{DOC}/v02/source.pdf",  # leading zero
        f"raw/{TENANT}/{DOC}/2/source.pdf",  # missing 'v'
        f"raw/{TENANT}/{DOC}/v2/other.pdf",
        f"raw/{TENANT}/{DOC}/v2/source.PDF",
        f"raw/{TENANT}/{DOC}/v2/source.pdf/extra",
        f"raw/{TENANT}/../{DOC}/v2/source.pdf",  # path traversal
        f"raw/{TENANT}/{DOC}/v2/source.pdf\n",  # trailing newline
        f"raw%2F{TENANT}%2F{DOC}%2Fv2%2Fsource.pdf",  # URL-encoded
        "",
    ],
)
def test_bad_key_is_rejected(key: str) -> None:
    assert rejected_fields(ingest_message(key=key)) == {"key"}


# --- each job type has its own required and forbidden fields -------------------------------


@pytest.mark.parametrize("field", ["bucket", "key", "size_bytes", "etag"])
def test_ingest_job_requires_its_s3_fields(field: str) -> None:
    assert rejected_fields(without(ingest_message(), field)) == {field}


@pytest.mark.parametrize(
    ("field", "value"), [("tenant_id", TENANT), ("doc_id", DOC), ("version", 2)]
)
def test_ingest_job_must_not_carry_identity_fields(field: str, value: object) -> None:
    # Identity in an S3 job comes only from the key; a message that sets it is forged.
    assert rejected_fields(ingest_message(**{field: value})) == {field}


@pytest.mark.parametrize("field", ["tenant_id", "doc_id", "version"])
def test_reindex_job_requires_tenant_doc_and_version(field: str) -> None:
    assert rejected_fields(without(api_message("reindex"), field)) == {field}


@pytest.mark.parametrize("field", ["tenant_id", "doc_id"])
def test_delete_job_requires_tenant_and_doc(field: str) -> None:
    assert rejected_fields(without(api_message("delete"), field)) == {field}


def test_delete_job_must_not_name_a_version() -> None:
    assert rejected_fields(api_message("delete", version=2)) == {"version"}


@pytest.mark.parametrize(
    "message",
    [
        ingest_message(source="api"),  # ingest jobs only come from the S3 event rule
        api_message("reindex", source="s3_event"),
        api_message("delete", source="s3_event"),
    ],
)
def test_source_must_match_the_job_type(message: dict) -> None:
    assert rejected_fields(message) == {"source"}


# --- job_id --------------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["", "x" * 129])
def test_job_id_must_be_1_to_128_characters(job_id: str) -> None:
    assert rejected_fields(ingest_message(job_id=job_id)) == {"job_id"}


def test_job_id_of_128_characters_is_accepted() -> None:
    assert parse(ingest_message(job_id="x" * 128)).job_id == "x" * 128
