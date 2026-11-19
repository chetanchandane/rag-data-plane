"""DB fixtures for API tests. Only tests that ask for them touch the database."""

import uuid
from collections.abc import AsyncIterator, Callable

import asyncpg
import pytest

from shared.db import create_pool, rls_transaction
from tests.dbutil import db_url


@pytest.fixture
async def api_pool() -> AsyncIterator[asyncpg.Pool]:
    """app_api, as the API connects. 4 connections so concurrency tests really overlap."""
    pool = await create_pool(db_url("DATABASE_URL_API"), max_size=4)
    yield pool
    await pool.close()


@pytest.fixture
async def ingest_pool() -> AsyncIterator[asyncpg.Pool]:
    """app_ingest: reads rows back regardless of role level, and cleans up."""
    pool = await create_pool(db_url("DATABASE_URL_INGEST"))
    yield pool
    await pool.close()


@pytest.fixture
async def new_tenant(ingest_pool: asyncpg.Pool) -> AsyncIterator[Callable[[], uuid.UUID]]:
    """Fresh random tenants; everything they wrote is deleted afterwards."""
    created: list[uuid.UUID] = []

    def make() -> uuid.UUID:
        created.append(uuid.uuid4())
        return created[-1]

    yield make
    for tenant_id in created:
        async with ingest_pool.acquire() as conn, rls_transaction(conn, tenant_id, None):
            await conn.execute("DELETE FROM documents")  # versions cascade
