"""POST /v1/documents and GET /v1/documents/{id} (SPEC: Architecture, "Upload flow").

Every query runs as app_api inside rls_transaction() with the caller's claims, so RLS limits
it to the caller's tenant and role level even where a WHERE clause doesn't say so.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.auth import Claims, get_claims
from api.config import MAX_UPLOAD_BYTES, Settings
from api.deps import get_pool, get_s3, get_settings
from api.uploads import presign_upload
from shared.db import rls_transaction
from shared.jobs import s3_key_for

router = APIRouter()


class RegisterRequest(BaseModel):
    # extra="ignore": a body field such as tenant_id or role_level is dropped, never read.
    # Tenant and role come only from get_claims(). strict: no "2" -> 2 or true -> 1.
    model_config = ConfigDict(extra="ignore", strict=True)

    external_id: str = Field(min_length=1, max_length=256)
    filename: str = Field(min_length=1, max_length=255)
    content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")  # hex, as stored
    size_bytes: int = Field(ge=1, le=MAX_UPLOAD_BYTES)
    min_role_level: int = Field(ge=1, le=3)

    @field_validator("content_sha256")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        # UNIQUE (doc_id, sha256) compares text: one spelling per hash, or "AB.." and
        # "ab.." would register the same file twice.
        return value.lower()


class UploadOut(BaseModel):
    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime


class RegisterResponse(BaseModel):
    doc_id: UUID
    version: int
    status: str
    duplicate: bool
    upload: UploadOut | None  # null unless the version is still waiting for its file


class VersionOut(BaseModel):
    version: int
    status: str
    attempts: int
    last_error: str | None
    chunk_count: int | None
    created_at: datetime
    indexed_at: datetime | None


class DocumentOut(BaseModel):
    doc_id: UUID
    external_id: str
    title: str | None
    min_role_level: int
    active_version: int | None
    created_at: datetime
    versions: list[VersionOut]  # newest first


@dataclass(frozen=True)
class _Registered:
    doc_id: UUID
    version: int
    status: str
    duplicate: bool


# Creates the document on first registration. app_api may insert only these four columns
# (migration 0002); id, active_version and deleted_at come from defaults.
# DO NOTHING when the external_id exists, including when RLS hides that document.
_INSERT_DOCUMENT = """
INSERT INTO documents (tenant_id, external_id, title, min_role_level)
VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id, external_id) DO NOTHING
"""

# The row lock serializes registrations for one document until COMMIT, so two concurrent
# requests can't both compute the same next version (DECISIONS, "Concurrent version
# allocation"). FOR UPDATE needs UPDATE on one column; app_api has it on title and deleted_at.
_LOCK_DOCUMENT = """
SELECT id, min_role_level, deleted_at FROM documents
WHERE tenant_id = $1 AND external_id = $2
FOR UPDATE
"""

_FIND_VERSION_BY_HASH = """
SELECT version, status FROM document_versions WHERE doc_id = $1 AND sha256 = $2
"""

_NEXT_VERSION = """
SELECT COALESCE(max(version), 0) + 1 FROM document_versions WHERE doc_id = $1
"""

# app_api may insert only these five columns (migration 0003); status starts as
# 'pending_upload' and attempts as 0, from defaults.
_INSERT_VERSION = """
INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key)
VALUES ($1, $2, $3, $4, $5)
"""

# One message for both cases on purpose: the caller must not learn whether a document it
# can't see exists at a higher role level, or what that level is.
_NOT_REGISTRABLE = "external_id is in use by a document you can't register versions for"


async def _register(
    conn: asyncpg.Connection, tenant_id: UUID, body: RegisterRequest
) -> _Registered:
    """Runs inside rls_transaction(). Idempotent on (document, sha256)."""
    await conn.execute(
        _INSERT_DOCUMENT, tenant_id, body.external_id, body.filename, body.min_role_level
    )
    doc = await conn.fetchrow(_LOCK_DOCUMENT, tenant_id, body.external_id)
    # None: the external_id exists but RLS hides it (its level is above the caller's).
    # A level mismatch: the API can't change a document's level (column grant), and a new
    # version at a different level would leave chunks and document out of sync.
    if doc is None or doc["min_role_level"] != body.min_role_level:
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_REGISTRABLE)
    if doc["deleted_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "document is deleted")
    doc_id = doc["id"]

    existing = await conn.fetchrow(_FIND_VERSION_BY_HASH, doc_id, body.content_sha256)
    if existing is not None:
        return _Registered(doc_id, existing["version"], existing["status"], duplicate=True)

    version = await conn.fetchval(_NEXT_VERSION, doc_id)
    await conn.execute(
        _INSERT_VERSION,
        doc_id,
        version,
        tenant_id,
        body.content_sha256,
        s3_key_for(tenant_id, doc_id, version),
    )
    return _Registered(doc_id, version, "pending_upload", duplicate=False)


@router.post("/v1/documents", status_code=status.HTTP_201_CREATED)
async def register_document(
    body: RegisterRequest,
    response: Response,
    claims: Annotated[Claims, Depends(get_claims)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    s3: Annotated[object, Depends(get_s3)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RegisterResponse:
    """Register a document version and get a presigned upload URL.

    201: a new version. 200: this document already has a version with this sha256, returned
    as is (no new version). Either way the response carries an upload URL only while the
    version is still pending_upload: a client that lost its URL can ask again
    (DECISIONS 2026-11-19).
    """
    # RLS would reject this insert too; checking first gives a clear 403 instead of an error.
    if body.min_role_level > claims.role_level:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "min_role_level is above your role level")

    async with pool.acquire() as conn, rls_transaction(conn, claims.tenant_id, claims.role_level):
        registered = await _register(conn, claims.tenant_id, body)

    # Presign after COMMIT, so the transaction (and the row lock) stays short.
    upload = None
    if registered.status == "pending_upload":
        signed = presign_upload(
            s3,
            bucket=settings.raw_bucket,
            key=s3_key_for(claims.tenant_id, registered.doc_id, registered.version),
            sha256_hex=body.content_sha256,
            size_bytes=body.size_bytes,
        )
        upload = UploadOut(**signed.__dict__)
    if registered.duplicate:
        response.status_code = status.HTTP_200_OK
    return RegisterResponse(
        doc_id=registered.doc_id,
        version=registered.version,
        status=registered.status,
        duplicate=registered.duplicate,
        upload=upload,
    )


@router.get("/v1/documents/{doc_id}")
async def get_document(
    doc_id: UUID,
    claims: Annotated[Claims, Depends(get_claims)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> DocumentOut:
    """A document and its versions: status, attempts, last error.

    404 for a document that doesn't exist, is deleted, belongs to another tenant or is above
    the caller's role level. RLS makes those look the same, and so does the response.
    """
    async with pool.acquire() as conn, rls_transaction(conn, claims.tenant_id, claims.role_level):
        doc = await conn.fetchrow(
            """
            SELECT id, external_id, title, min_role_level, active_version, created_at
            FROM documents WHERE id = $1 AND deleted_at IS NULL
            """,
            doc_id,
        )
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
        versions = await conn.fetch(
            """
            SELECT version, status, attempts, last_error, chunk_count, created_at, indexed_at
            FROM document_versions WHERE doc_id = $1 ORDER BY version DESC
            """,
            doc_id,
        )
    return DocumentOut(
        doc_id=doc["id"],
        external_id=doc["external_id"],
        title=doc["title"],
        min_role_level=doc["min_role_level"],
        active_version=doc["active_version"],
        created_at=doc["created_at"],
        versions=[VersionOut(**dict(v)) for v in versions],
    )
