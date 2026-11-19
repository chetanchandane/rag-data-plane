"""IngestJob v1: the message contract on the ingest queue (SPEC: Ingestion).

IngestJob is a union of one model per job_type, each with its own required fields:

  job_type  source    required                          must be null/absent
  --------  --------  --------------------------------  ---------------------------
  ingest    s3_event  bucket, key, size_bytes, etag     tenant_id, doc_id, version
  reindex   api       tenant_id, doc_id, version        -
  delete    api       tenant_id, doc_id                 version

An ingest job's identity comes only from its key (parsed here, then confirmed against the
stored s3_key by the worker). A message that also sets tenant_id/doc_id/version is forged
or malformed and is rejected. A key alone is never trusted.

Anything that fails validation (including an unknown schema_version) raises
pydantic.ValidationError. The queue handler logs the reason and lets the message go to the
DLQ; it never drops one silently. Fields may be added to v1, never repurposed.
"""

import re
from datetime import datetime
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# raw/{tenant_id}/{doc_id}/v{version}/source.pdf
# Lowercase canonical UUIDs only (what str(uuid) produces), version >= 1 with no leading
# zero, nothing before or after. fullmatch() anchors both ends.
S3_KEY_PATTERN = re.compile(
    rf"raw/(?P<tenant_id>{_UUID})/(?P<doc_id>{_UUID})/v(?P<version>[1-9][0-9]{{0,8}})/source\.pdf"
)


class S3KeyParts(NamedTuple):
    tenant_id: UUID
    doc_id: UUID
    version: int


def parse_s3_key(key: str) -> S3KeyParts:
    """Parse a raw-bucket key. Raises ValueError if it doesn't match the layout exactly."""
    match = S3_KEY_PATTERN.fullmatch(key)
    if match is None:
        raise ValueError("key does not match raw/{tenant_id}/{doc_id}/v{version}/source.pdf")
    return S3KeyParts(
        tenant_id=UUID(match["tenant_id"]),
        doc_id=UUID(match["doc_id"]),
        version=int(match["version"]),
    )


class _JobBase(BaseModel):
    """Fields every job carries."""

    # frozen: a job is a message, not something to edit. extra="ignore": fields added to v1
    # later must not make older workers reject the message.
    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal[1]
    # EventBridge supplies its event id; the API generates a UUIDv4. Bounded so a bad
    # producer can't push arbitrarily long ids into logs.
    job_id: str = Field(min_length=1, max_length=128)
    emitted_at: datetime
    traceparent: str | None = None


class IngestFileJob(_JobBase):
    """A file landed in the raw bucket. Produced only by the EventBridge rule."""

    job_type: Literal["ingest"]
    source: Literal["s3_event"]
    bucket: str = Field(min_length=1)
    key: str
    size_bytes: int = Field(ge=0)
    etag: str = Field(min_length=1)
    # Typed as None: the SPEC's shape includes these as null, and any other value is rejected.
    tenant_id: None = None
    doc_id: None = None
    version: None = None

    @field_validator("key")
    @classmethod
    def _key_matches_layout(cls, key: str) -> str:
        parse_s3_key(key)  # raises ValueError, which pydantic reports as a ValidationError
        return key

    def key_parts(self) -> S3KeyParts:
        """tenant_id, doc_id and version as claimed by the key. Not yet trusted."""
        return parse_s3_key(self.key)


class ReindexJob(_JobBase):
    """Re-run indexing for one version. Produced by the API."""

    job_type: Literal["reindex"]
    source: Literal["api"]
    tenant_id: UUID
    doc_id: UUID
    version: int = Field(ge=1)


class DeleteJob(_JobBase):
    """Purge a whole document (chunks, versions, S3 objects). Produced by the API."""

    job_type: Literal["delete"]
    source: Literal["api"]
    tenant_id: UUID
    doc_id: UUID
    # A delete covers every version, so a message naming one is ambiguous and is rejected.
    version: None = None


# Pydantic reads job_type first and validates against that one model, so errors name the
# fields of the job type the producer meant.
IngestJob = Annotated[IngestFileJob | ReindexJob | DeleteJob, Field(discriminator="job_type")]

_INGEST_JOB = TypeAdapter(IngestJob)


def parse_ingest_job(body: str | bytes) -> IngestFileJob | ReindexJob | DeleteJob:
    """Validate one queue message body (JSON). Raises pydantic.ValidationError if invalid."""
    return _INGEST_JOB.validate_json(body)
