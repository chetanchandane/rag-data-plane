"""POST /v1/documents and GET /v1/documents/{id} against the Neon dev branch, as app_api.

Step 9's "done when": duplicate upload, a body carrying another tenant's tenant_id, and
concurrent registrations of new content for one document.
"""

import asyncio
import urllib.parse
import uuid

import asyncpg
import pytest

from api.uploads import checksum_header
from shared.db import rls_transaction
from shared.jobs import s3_key_for
from tests.api.apphelpers import build_app, client, registration

pytestmark = pytest.mark.db


async def ingest_rows(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str, *args):
    async with pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
        return await conn.fetch(sql, *args)


def url_key(upload: dict) -> str:
    return urllib.parse.urlparse(upload["url"]).path.lstrip("/")


async def test_a_new_registration_creates_version_1_and_an_upload_url(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    body = registration(b"%PDF one", min_role_level=2)
    async with client(build_app(api_pool, tenant, 2)) as c:
        resp = await c.post("/v1/documents", json=body)

    assert resp.status_code == 201
    out = resp.json()
    assert (out["version"], out["status"], out["duplicate"]) == (1, "pending_upload", False)
    doc_id = uuid.UUID(out["doc_id"])
    key = s3_key_for(tenant, doc_id, 1)
    assert url_key(out["upload"]) == key
    assert out["upload"]["headers"] == {
        "x-amz-checksum-sha256": checksum_header(body["content_sha256"])
    }

    [doc] = await ingest_rows(ingest_pool, tenant, "SELECT * FROM documents")
    assert (doc["id"], doc["tenant_id"], doc["external_id"]) == (doc_id, tenant, "doc-1")
    assert (doc["title"], doc["min_role_level"], doc["active_version"]) == ("handbook.pdf", 2, None)
    [ver] = await ingest_rows(ingest_pool, tenant, "SELECT * FROM document_versions")
    assert (ver["version"], ver["sha256"], ver["s3_key"]) == (1, body["content_sha256"], key)
    assert (ver["status"], ver["attempts"]) == ("pending_upload", 0)


async def test_a_duplicate_upload_creates_no_new_version(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    body = registration(b"%PDF same")
    async with client(build_app(api_pool, tenant, 1)) as c:
        first = (await c.post("/v1/documents", json=body)).json()
        # Hash in uppercase: still the same file.
        again = await c.post(
            "/v1/documents", json=body | {"content_sha256": body["content_sha256"].upper()}
        )

        # Still pending_upload: the client may have lost its URL, so it gets a fresh one.
        assert again.status_code == 200
        dup = again.json()
        assert (dup["doc_id"], dup["version"], dup["duplicate"]) == (first["doc_id"], 1, True)
        assert url_key(dup["upload"]) == url_key(first["upload"])

        # Once the worker has the file, a duplicate gets no URL.
        await ingest_rows(ingest_pool, tenant, "UPDATE document_versions SET status = 'indexed'")
        done = await c.post("/v1/documents", json=body)
    assert done.status_code == 200
    assert (done.json()["status"], done.json()["upload"]) == ("indexed", None)
    assert len(await ingest_rows(ingest_pool, tenant, "SELECT 1 FROM document_versions")) == 1


async def test_new_content_for_a_document_gets_the_next_version(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    async with client(build_app(api_pool, tenant, 1)) as c:
        v1 = (await c.post("/v1/documents", json=registration(b"%PDF v1"))).json()
        resp = await c.post("/v1/documents", json=registration(b"%PDF v2"))
    assert resp.status_code == 201
    v2 = resp.json()
    assert (v2["doc_id"], v2["version"], v2["duplicate"]) == (v1["doc_id"], 2, False)
    assert url_key(v2["upload"]) == s3_key_for(tenant, uuid.UUID(v1["doc_id"]), 2)
    assert len(await ingest_rows(ingest_pool, tenant, "SELECT 1 FROM documents")) == 1


async def test_a_tenant_id_in_the_body_is_ignored(api_pool, ingest_pool, new_tenant):
    claims_tenant, other_tenant = new_tenant(), new_tenant()
    body = registration() | {"tenant_id": str(other_tenant), "role_level": 3}
    async with client(build_app(api_pool, claims_tenant, 1)) as c:
        resp = await c.post("/v1/documents", json=body)

    assert resp.status_code == 201
    doc_id = uuid.UUID(resp.json()["doc_id"])
    assert url_key(resp.json()["upload"]) == s3_key_for(claims_tenant, doc_id, 1)
    [row] = await ingest_rows(ingest_pool, claims_tenant, "SELECT id, tenant_id FROM documents")
    assert tuple(row) == (doc_id, claims_tenant)
    assert await ingest_rows(ingest_pool, other_tenant, "SELECT 1 FROM documents") == []
    assert await ingest_rows(ingest_pool, other_tenant, "SELECT 1 FROM document_versions") == []


async def test_concurrent_registrations_of_new_content_get_distinct_versions(
    api_pool, ingest_pool, new_tenant
):
    """All four start at once on a document that doesn't exist yet. Without the row lock,
    two of them compute the same next version and one fails on the primary key."""
    tenant = new_tenant()
    async with client(build_app(api_pool, tenant, 1)) as c:
        responses = await asyncio.gather(
            *(c.post("/v1/documents", json=registration(f"%PDF {n}".encode())) for n in range(4))
        )
    assert [r.status_code for r in responses] == [201] * 4
    assert sorted(r.json()["version"] for r in responses) == [1, 2, 3, 4]
    assert len({r.json()["doc_id"] for r in responses}) == 1
    rows = await ingest_rows(ingest_pool, tenant, "SELECT version FROM document_versions")
    assert sorted(r["version"] for r in rows) == [1, 2, 3, 4]


async def test_concurrent_registrations_of_the_same_content_make_one_version(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    async with client(build_app(api_pool, tenant, 1)) as c:
        responses = await asyncio.gather(
            *(c.post("/v1/documents", json=registration(b"%PDF same")) for _ in range(3))
        )
    assert sorted(r.status_code for r in responses) == [200, 200, 201]
    assert {r.json()["version"] for r in responses} == {1}
    assert len(await ingest_rows(ingest_pool, tenant, "SELECT 1 FROM document_versions")) == 1


async def test_hidden_and_mismatched_documents_get_the_same_409(api_pool, ingest_pool, new_tenant):
    tenant = new_tenant()
    async with client(build_app(api_pool, tenant, 3)) as admin:
        await admin.post("/v1/documents", json=registration(external_id="secret", min_role_level=3))
        await admin.post("/v1/documents", json=registration(external_id="open", min_role_level=1))
        # Visible document, different level.
        mismatch = await admin.post(
            "/v1/documents",
            json=registration(b"%PDF new", external_id="open", min_role_level=2),
        )
    async with client(build_app(api_pool, tenant, 1)) as reader:
        # The role-1 caller can't see "secret" (level 3), so this can't add a version to it.
        hidden = await reader.post(
            "/v1/documents", json=registration(b"%PDF new", external_id="secret")
        )

    assert mismatch.status_code == hidden.status_code == 409
    assert mismatch.json() == hidden.json()  # nothing tells the two cases apart
    rows = await ingest_rows(ingest_pool, tenant, "SELECT version FROM document_versions")
    assert len(rows) == 2  # only the two first registrations


async def test_get_shows_each_versions_status_attempts_and_last_error(
    api_pool, ingest_pool, new_tenant
):
    tenant = new_tenant()
    async with client(build_app(api_pool, tenant, 1)) as c:
        first = (await c.post("/v1/documents", json=registration(b"%PDF v1"))).json()
        await c.post("/v1/documents", json=registration(b"%PDF v2"))
        await ingest_rows(
            ingest_pool,
            tenant,
            "UPDATE document_versions SET status = 'failed', attempts = 1,"
            " last_error = 'sha256_mismatch: file does not match' WHERE version = 1",
        )
        resp = await c.get(f"/v1/documents/{first['doc_id']}")

    assert resp.status_code == 200
    out = resp.json()
    assert (out["doc_id"], out["external_id"], out["active_version"]) == (
        first["doc_id"],
        "doc-1",
        None,
    )
    assert [
        (v["version"], v["status"], v["attempts"], v["last_error"]) for v in out["versions"]
    ] == [
        (2, "pending_upload", 0, None),
        (1, "failed", 1, "sha256_mismatch: file does not match"),
    ]


async def test_get_returns_404_for_documents_the_caller_cant_see(api_pool, new_tenant):
    tenant, other = new_tenant(), new_tenant()
    async with client(build_app(api_pool, tenant, 3)) as admin:
        secret = (await admin.post("/v1/documents", json=registration(min_role_level=3))).json()
        assert (await admin.get(f"/v1/documents/{secret['doc_id']}")).status_code == 200
    async with client(build_app(api_pool, tenant, 1)) as reader:
        above_role = await reader.get(f"/v1/documents/{secret['doc_id']}")
    async with client(build_app(api_pool, other, 3)) as other_admin:
        other_tenant = await other_admin.get(f"/v1/documents/{secret['doc_id']}")
        unknown = await other_admin.get(f"/v1/documents/{uuid.uuid4()}")

    assert above_role.status_code == other_tenant.status_code == unknown.status_code == 404
    assert above_role.json() == other_tenant.json() == unknown.json()
