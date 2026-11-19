"""DB fixtures for the worker tests. Only tests that ask for `pool` touch the database."""

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from shared.db import create_pool, rls_transaction
from tests.dbutil import db_url


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await create_pool(db_url("DATABASE_URL_INGEST"))
    yield pool
    await pool.close()


@pytest.fixture
async def tenant(pool: asyncpg.Pool) -> AsyncIterator[uuid.UUID]:
    """A fresh tenant per test; everything it wrote is deleted afterwards."""
    tenant_id = uuid.uuid4()
    yield tenant_id
    async with pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
        await conn.execute("DELETE FROM documents")  # versions and chunks cascade
        await conn.execute("DELETE FROM tenant_index_state")
