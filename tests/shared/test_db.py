"""DB tests for the pool factory and rls_transaction (Neon dev branch)."""

from uuid import uuid4

import asyncpg
import pytest
from pgvector import Vector

from shared.db import create_pool, rls_transaction
from tests.dbutil import db_url, direct_url

pytestmark = pytest.mark.db


async def test_pool_refuses_a_role_that_could_bypass_rls() -> None:
    # The owner URL is in libpq form; asyncpg doesn't understand channel_binding.
    owner = db_url("DATABASE_URL_OWNER").replace("&channel_binding=require", "")
    pool = await create_pool(owner)
    try:
        with pytest.raises(RuntimeError, match="refusing to connect as 'app_owner'"):
            async with pool.acquire():
                pass
    finally:
        await pool.close()


async def test_pool_registers_the_vector_codec() -> None:
    pool = await create_pool(db_url("DATABASE_URL_INGEST"))
    try:
        async with pool.acquire() as conn:
            # A plain list goes in as a binary vector parameter, a Vector comes back.
            value = await conn.fetchval("SELECT $1::vector", [1.0, 2.0, 3.0])
    finally:
        await pool.close()
    assert isinstance(value, Vector)
    assert value.to_list() == [1.0, 2.0, 3.0]


async def test_rls_transaction_sets_context_and_timeout_only_for_the_transaction() -> None:
    # Direct endpoint: before, during and after must be the same server session.
    conn = await asyncpg.connect(direct_url(db_url("DATABASE_URL_API")), timeout=15)
    tenant = uuid4()
    try:
        timeout_before = await conn.fetchval("SHOW statement_timeout")
        async with rls_transaction(conn, tenant, 2):
            inside = await conn.fetchrow(
                "SELECT app_tenant() AS tenant, app_role_level() AS role, "
                "current_setting('statement_timeout') AS timeout"
            )
        after = await conn.fetchrow(
            "SELECT app_tenant() AS tenant, app_role_level() AS role, "
            "current_setting('statement_timeout') AS timeout"
        )
    finally:
        await conn.close()

    assert (inside["tenant"], inside["role"], inside["timeout"]) == (tenant, 2, "2s")
    assert (after["tenant"], after["role"], after["timeout"]) == (None, 0, timeout_before)


async def test_statement_timeout_cancels_a_slow_query() -> None:
    conn = await asyncpg.connect(db_url("DATABASE_URL_API"), timeout=15)
    try:
        with pytest.raises(asyncpg.QueryCanceledError, match="statement timeout"):
            async with rls_transaction(conn, uuid4(), 1, statement_timeout_ms=200):
                await conn.execute("SELECT pg_sleep(2)")
    finally:
        await conn.close()
