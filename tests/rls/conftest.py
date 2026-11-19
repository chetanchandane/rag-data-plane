"""Fixtures for RLS tests: real app-role connections and two seeded tenants.

The tests connect as app_api / app_ingest over Neon's pooled endpoint, exactly like the app.
Every test gets fresh random tenant IDs, so leftovers from a crashed run can't affect results.
"""

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.dbutil import db_url, direct_url
from tests.rls.seed import Tenant, delete_tenant, seed_tenant


@pytest.fixture
async def api_conn() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(db_url("DATABASE_URL_API"), timeout=15)
    yield conn
    await conn.close()


@pytest.fixture
async def api_conn_direct() -> AsyncIterator[asyncpg.Connection]:
    """app_api on the direct endpoint: one client connection is one server session."""
    conn = await asyncpg.connect(direct_url(db_url("DATABASE_URL_API")), timeout=15)
    yield conn
    await conn.close()


@pytest.fixture
async def ingest_conn() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(db_url("DATABASE_URL_INGEST"), timeout=15)
    yield conn
    await conn.close()


@pytest.fixture
async def tenants(ingest_conn: asyncpg.Connection) -> AsyncIterator[tuple[Tenant, Tenant]]:
    """Tenants A and B, each with a level-1 and a level-3 document."""
    a = await seed_tenant(ingest_conn, uuid.uuid4())
    b = await seed_tenant(ingest_conn, uuid.uuid4())
    yield a, b
    for t in (a, b):
        await delete_tenant(ingest_conn, t.id)
