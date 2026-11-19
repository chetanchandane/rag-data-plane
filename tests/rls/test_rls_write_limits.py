"""Behaviour tests for the policy and grant decisions made in step 2 (docs/DECISIONS.md).

Every negative case asserts the specific failure:
  - a missing GRANT       -> InsufficientPrivilegeError, "permission denied for table X"
  - an RLS WITH CHECK     -> InsufficientPrivilegeError, "new row violates row-level security"
  - a tenant-consistent FK -> ForeignKeyViolationError on the named constraint
Both privilege failures share SQLSTATE 42501, so the message is what tells them apart.
"""

import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from shared.db import rls_transaction
from tests.rls.seed import Tenant

pytestmark = pytest.mark.db


class _RollBack(Exception):
    """Raised inside a transaction only to roll it back."""


def permission_denied(table: str) -> str:
    return rf"permission denied for table {table}$"


def rls_violation(table: str) -> str:
    return rf'new row violates row-level security policy for table "{table}"'


async def run_as(
    conn: asyncpg.Connection,
    tenant_id: uuid.UUID,
    role_level: int | None,
    query: str,
    *args: object,
) -> str:
    """Run one statement in its own transaction with the given request context."""
    async with rls_transaction(conn, tenant_id, role_level):
        return await conn.execute(query, *args)


# --- documents: column-level UPDATE --------------------------------------------------------


@pytest.mark.parametrize(("column", "value"), [("min_role_level", 1), ("active_version", 2)])
async def test_api_cannot_update_protected_document_columns(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant], column: str, value: int
) -> None:
    a, _ = tenants
    # Column names come from the parametrize list above, not from input.
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match=permission_denied("documents")):
        await run_as(
            api_conn, a.id, 3, f"UPDATE documents SET {column} = $1 WHERE id = $2",
            value, a.docs[3].doc_id,
        )  # fmt: skip


async def test_api_can_update_title_and_deleted_at(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    status = await run_as(
        api_conn, a.id, 3, "UPDATE documents SET title = $1, deleted_at = now() WHERE id = $2",
        "renamed", a.docs[3].doc_id,
    )  # fmt: skip
    assert status == "UPDATE 1"


# --- documents: column-level INSERT (migration 0002) and role check on writes ---------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("active_version", 5),
        ("deleted_at", datetime(2026, 1, 1, tzinfo=UTC)),
        ("id", uuid.UUID(int=1)),
        ("created_at", datetime(2020, 1, 1, tzinfo=UTC)),
    ],
)
async def test_api_cannot_insert_protected_document_columns(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant], column: str, value: object
) -> None:
    a, _ = tenants
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match=permission_denied("documents")):
        await run_as(
            api_conn, a.id, 3,
            f"INSERT INTO documents (tenant_id, external_id, min_role_level, {column}) "
            "VALUES ($1, 'new-doc', 1, $2)",
            a.id, value,
        )  # fmt: skip


async def test_api_can_register_a_document_with_the_allowed_columns(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    async with rls_transaction(api_conn, a.id, 3):
        row = await api_conn.fetchrow(
            """
            INSERT INTO documents (tenant_id, external_id, title, min_role_level)
            VALUES ($1, 'new-doc', 'New', 2)
            RETURNING id, active_version, deleted_at
            """,
            a.id,
        )
    # Server-side defaults filled the columns the API may not set.
    assert row["id"] is not None
    assert row["active_version"] is None
    assert row["deleted_at"] is None


async def test_api_cannot_register_a_document_above_its_role_level(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match=rls_violation("documents")):
        await run_as(
            api_conn, a.id, 1,
            "INSERT INTO documents (tenant_id, external_id, min_role_level) VALUES ($1, 'x', 3)",
            a.id,
        )  # fmt: skip


# --- document_versions: column-level INSERT (migration 0003), no UPDATE ---------------------

# Used by app_ingest in the FK test below; app_ingest may set every column.
INSERT_VERSION = """
    INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key, status, attempts)
    VALUES ($1, 2, $2, $3, 'raw/test/v2/source.pdf', $4, $5)
"""


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("lease_until", datetime(2030, 1, 1, tzinfo=UTC)),
        ("last_error", "boom"),
        ("parser", "pymupdf"),
        ("chunk_count", 3),
        ("indexed_at", datetime(2026, 1, 1, tzinfo=UTC)),
        # Even the values the WITH CHECK would accept are denied: it's the column, not the value.
        ("status", "pending_upload"),
        ("attempts", 0),
    ],
)
async def test_api_cannot_insert_worker_owned_version_columns(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant], column: str, value: object
) -> None:
    a, _ = tenants
    with pytest.raises(
        asyncpg.InsufficientPrivilegeError, match=permission_denied("document_versions")
    ):
        await run_as(
            api_conn, a.id, 3,
            f"INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key, {column}) "
            "VALUES ($1, 2, $2, $3, 'raw/test/v2/source.pdf', $4)",
            a.docs[1].doc_id, a.id, "1" * 64, value,
        )  # fmt: skip


async def test_api_can_register_a_version_with_the_allowed_columns(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    async with rls_transaction(api_conn, a.id, 3):
        row = await api_conn.fetchrow(
            """
            INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key)
            VALUES ($1, 2, $2, $3, 'raw/test/v2/source.pdf')
            RETURNING status, attempts, lease_until
            """,
            a.docs[1].doc_id,
            a.id,
            "1" * 64,
        )
    # Defaults supply what the API may not set.
    assert (row["status"], row["attempts"], row["lease_until"]) == ("pending_upload", 0, None)


async def test_version_insert_policy_keeps_its_second_layer(
    api_conn: asyncpg.Connection,
) -> None:
    """app_api can no longer write status or attempts, so the WITH CHECK can't be triggered
    through it. Assert the policy still requires both, in case the grant is ever widened."""
    with_check = await api_conn.fetchval(
        "SELECT with_check FROM pg_policies "
        "WHERE tablename = 'document_versions' AND policyname = 'document_versions_api_insert'"
    )
    assert "status = 'pending_upload'" in with_check
    assert "attempts = 0" in with_check


async def test_api_cannot_update_document_versions(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    with pytest.raises(
        asyncpg.InsufficientPrivilegeError, match=permission_denied("document_versions")
    ):
        await run_as(
            api_conn, a.id, 3,
            "UPDATE document_versions SET status = 'indexed' WHERE doc_id = $1",
            a.docs[1].doc_id,
        )  # fmt: skip


# --- query_cache: isolated by exact role level ----------------------------------------------


async def test_query_cache_is_isolated_by_role_level(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    insert_cache = """
        INSERT INTO query_cache (cache_key, tenant_id, role_level, response, expires_at)
        VALUES ($1, $2, $3, '{"answer": "level-3 only"}', now() + interval '1 hour')
    """
    cache_key = uuid.uuid4().hex * 2  # 64 chars
    await run_as(api_conn, a.id, 3, insert_cache, cache_key, a.id, 3)
    try:
        count_sql = "SELECT count(*) FROM query_cache"
        async with rls_transaction(api_conn, a.id, 3):
            assert await api_conn.fetchval(count_sql) == 1  # not vacuous
        async with rls_transaction(api_conn, a.id, 1):
            assert await api_conn.fetchval(count_sql) == 0  # level 1 can't read a level-3 entry

        # A level-1 context can't write an entry labelled level 3 either.
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match=rls_violation("query_cache")):
            await run_as(api_conn, a.id, 1, insert_cache, uuid.uuid4().hex * 2, a.id, 3)
    finally:
        # Tenants are deleted via documents; cache rows have no parent, so remove this one here.
        await run_as(api_conn, a.id, 3, "DELETE FROM query_cache WHERE cache_key = $1", cache_key)


# --- tenant-consistent foreign keys ---------------------------------------------------------
# FK checks bypass RLS. With a plain FK on doc_id these inserts would succeed, because B's
# document exists. Including tenant_id in the FK makes the cross-tenant reference invalid.


async def test_fk_rejects_a_version_pointing_at_another_tenants_document(
    ingest_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, b = tenants
    # tenant_id = A passes the RLS WITH CHECK; only the FK can reject it.
    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await run_as(
            ingest_conn, a.id, None, INSERT_VERSION, b.docs[1].doc_id, a.id, "2" * 64,
            "pending_upload", 0,
        )  # fmt: skip
    assert exc_info.value.constraint_name == "document_versions_doc_id_tenant_id_fkey"


async def test_fk_rejects_a_chunk_pointing_at_another_tenants_version(
    ingest_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, b = tenants
    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await run_as(
            ingest_conn, a.id, None,
            """
            INSERT INTO chunks (tenant_id, doc_id, version, chunk_index, min_role_level,
                                content, embedding)
            VALUES ($1, $2, 1, 50, 1, 'x', array_fill(0::real, ARRAY[1024])::vector)
            """,
            a.id, b.docs[1].doc_id,
        )  # fmt: skip
    assert exc_info.value.constraint_name == "chunks_doc_id_version_tenant_id_fkey"


# --- feedback: INSERT only -------------------------------------------------------------------


async def test_api_can_insert_feedback(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    # Rolled back on purpose (raising inside rls_transaction rolls it back): app_api can't
    # delete feedback, and no other role may, so a committed row would stay forever.
    with pytest.raises(_RollBack):
        async with rls_transaction(api_conn, a.id, 3):
            status = await api_conn.execute(
                "INSERT INTO feedback (tenant_id, trace_id, helpful) VALUES ($1, 'trace-1', true)",
                a.id,
            )
            assert status == "INSERT 0 1"
            raise _RollBack


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM feedback",
        "UPDATE feedback SET helpful = false",
        "DELETE FROM feedback",
    ],
)
async def test_api_cannot_read_update_or_delete_feedback(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant], query: str
) -> None:
    a, _ = tenants
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match=permission_denied("feedback")):
        await run_as(api_conn, a.id, 3, query)
