"""The five RLS tests from docs/SPEC.md "Proof": isolation enforced by Postgres, not app code.

None of the read queries has a tenant or role WHERE clause. If RLS were missing or wrong,
they would return other tenants' rows.
"""

import asyncpg
import pytest

from shared.db import rls_transaction
from tests.rls.seed import Tenant

pytestmark = pytest.mark.db

# Tables app_api can SELECT from. (feedback is INSERT-only for app_api.)
API_READABLE_TABLES = [
    "documents",
    "document_versions",
    "chunks",
    "tenant_index_state",
    "query_cache",
]


async def test_no_context_returns_zero_rows(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    # Not vacuous: with a context, tenant A's chunks are visible.
    async with rls_transaction(api_conn, a.id, 3):
        assert await api_conn.fetchval("SELECT count(*) FROM chunks") == 2

    # Without one, app_tenant() is NULL and app_role_level() is 0: nothing matches.
    assert await api_conn.fetchval("SELECT count(*) FROM chunks") == 0
    for table in API_READABLE_TABLES:
        # Table names come from the constant list above, not from input.
        assert await api_conn.fetchval(f"SELECT count(*) FROM {table}") == 0, table


async def test_tenant_a_role_1_sees_only_its_own_level_1_rows(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    async with rls_transaction(api_conn, a.id, 1):
        chunks = await api_conn.fetch("SELECT tenant_id, min_role_level FROM chunks")
        documents = await api_conn.fetch("SELECT id FROM documents")
        versions = await api_conn.fetch("SELECT doc_id FROM document_versions")

    # Exactly A's level-1 rows: nothing from B, nothing from A's level-3 document.
    assert [tuple(r) for r in chunks] == [(a.id, 1)]
    assert [r["id"] for r in documents] == [a.docs[1].doc_id]
    # document_versions has no role column; it inherits visibility from its document.
    assert [r["doc_id"] for r in versions] == [a.docs[1].doc_id]


async def test_api_without_a_role_level_sees_nothing(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    """role_level=None is how the worker runs. If it ever reached an app_api connection,
    the tenant alone must not be enough: app_role_level() is 0 and nothing matches."""
    a, _ = tenants
    count_all = "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks)"

    async with rls_transaction(api_conn, a.id, 3):
        assert tuple(await api_conn.fetchrow(count_all)) == (2, 2)  # not vacuous

    async with rls_transaction(api_conn, a.id, None):
        assert await api_conn.fetchval("SELECT app_role_level()") == 0
        assert tuple(await api_conn.fetchrow(count_all)) == (0, 0)


async def test_context_is_gone_in_the_next_transaction(
    api_conn_direct: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    # Direct endpoint on purpose: through the transaction-mode pooler the two transactions
    # may run on different server sessions, and the test would pass without proving
    # anything. The pid check below makes that impossible.
    conn = api_conn_direct
    a, _ = tenants
    async with rls_transaction(conn, a.id, 3):
        first_pid = await conn.fetchval("SELECT pg_backend_pid()")
        assert await conn.fetchval("SELECT count(*) FROM chunks") == 2

    # Same server session, next transaction: set_config(..., true) did not outlive the
    # first one. This is what stops one request's context leaking into the next request
    # that reuses a pooled server connection.
    async with conn.transaction():
        second_pid = await conn.fetchval("SELECT pg_backend_pid()")
        setting = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
        count = await conn.fetchval("SELECT count(*) FROM chunks")

    assert second_pid == first_pid, "transactions ran on different server sessions"
    assert setting in (None, "")
    assert count == 0


async def test_session_level_context_would_leak(
    api_conn_direct: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    """Control for the test above: with is_local = false the context DOES survive, so that
    test would catch a regression to session-level settings."""
    conn = api_conn_direct
    a, _ = tenants
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.tenant_id', $1, false), "
            "set_config('app.role_level', '3', false)",
            str(a.id),
        )
    async with conn.transaction():
        leaked = await conn.fetchval("SELECT count(*) FROM chunks")
    assert leaked == 2  # tenant A's rows visible with no context set in this transaction


async def test_tenant_id_from_the_request_is_ignored(
    api_conn: asyncpg.Connection, tenants: tuple[Tenant, Tenant]
) -> None:
    a, b = tenants
    # Context comes from the verified claims (tenant A). Simulate a handler that wrongly
    # uses a tenant_id or document id taken from the request body (tenant B's).
    async with rls_transaction(api_conn, a.id, 3):
        assert (
            await api_conn.fetchval("SELECT count(*) FROM chunks WHERE tenant_id = $1", b.id) == 0
        )
        assert (
            await api_conn.fetchval("SELECT count(*) FROM documents WHERE tenant_id = $1", b.id)
            == 0
        )
        assert (
            await api_conn.fetchrow("SELECT * FROM documents WHERE id = $1", b.docs[1].doc_id)
            is None
        )


async def test_writes_cannot_cross_roles_or_tenants(
    api_conn: asyncpg.Connection,
    ingest_conn: asyncpg.Connection,
    tenants: tuple[Tenant, Tenant],
) -> None:
    a, b = tenants
    insert_chunk = """
        INSERT INTO chunks (tenant_id, doc_id, version, chunk_index, min_role_level,
                            content, embedding)
        VALUES ($1, $2, 1, 99, 1, 'injected', array_fill(0::real, ARRAY[1024])::vector)
    """

    # app_api has no INSERT grant on chunks at all: rejected before RLS is consulted.
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
        async with rls_transaction(api_conn, a.id, 3):
            await api_conn.execute(insert_chunk, a.id, a.docs[1].doc_id)

    # app_ingest scoped to tenant A writing a tenant B row (pointing at B's real version,
    # so only the policy can be the reason it fails): the WITH CHECK rejects it.
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with rls_transaction(ingest_conn, a.id, None):
            await ingest_conn.execute(insert_chunk, b.id, b.docs[1].doc_id)

    # Nothing was written for B.
    async with rls_transaction(ingest_conn, b.id, None):
        assert await ingest_conn.fetchval("SELECT count(*) FROM chunks WHERE chunk_index = 99") == 0
