"""Unit tests for rls_transaction (no network): argument checks and which settings it sets.

A small stand-in connection records the SQL it is given, so these run without a database.
The behaviour against real Postgres is covered by tests/shared/test_db.py and tests/rls/.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from shared.db import (
    API_STATEMENT_TIMEOUT_MS,
    WORKER_STATEMENT_TIMEOUT_MS,
    rls_transaction,
)


class FakeConnection:
    def __init__(self, in_transaction: bool = False) -> None:
        self.in_transaction = in_transaction
        self.executed: list[tuple[str, tuple]] = []

    def is_in_transaction(self) -> bool:
        return self.in_transaction

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, query: str, *args: object) -> None:
        self.executed.append((query, args))


@pytest.mark.parametrize(
    ("tenant_id", "role_level", "kwargs", "error"),
    [
        (str(uuid4()), 1, {}, TypeError),  # a str could have come from anywhere
        (None, 1, {}, TypeError),
        (uuid4(), "3", {}, TypeError),
        (uuid4(), True, {}, TypeError),  # bool is an int subclass
        (uuid4(), 0, {}, ValueError),
        (uuid4(), 4, {}, ValueError),
        (uuid4(), 1, {"statement_timeout_ms": 0}, ValueError),  # 0 disables the timeout
        (uuid4(), 1, {"statement_timeout_ms": 99}, ValueError),
        (uuid4(), 1, {"statement_timeout_ms": 60_001}, ValueError),
        (uuid4(), 1, {"statement_timeout_ms": "2000"}, TypeError),
    ],
)
async def test_rejects_bad_arguments(
    tenant_id: object, role_level: object, kwargs: dict, error: type[Exception]
) -> None:
    conn = FakeConnection()
    with pytest.raises(error):
        async with rls_transaction(conn, tenant_id, role_level, **kwargs):
            pass
    assert conn.executed == []  # rejected before any SQL


async def test_refuses_to_nest() -> None:
    conn = FakeConnection(in_transaction=True)
    with pytest.raises(RuntimeError, match="cannot be nested"):
        async with rls_transaction(conn, uuid4(), 1):
            pass
    assert conn.executed == []


async def test_with_a_role_sets_tenant_timeout_and_role() -> None:
    conn, tenant = FakeConnection(), uuid4()
    async with rls_transaction(conn, tenant, 2):
        pass
    [(query, args)] = conn.executed
    assert "app.role_level" in query
    assert args == (str(tenant), str(API_STATEMENT_TIMEOUT_MS), "2")


async def test_without_a_role_does_not_set_role_level() -> None:
    conn, tenant = FakeConnection(), uuid4()
    async with rls_transaction(
        conn, tenant, None, statement_timeout_ms=WORKER_STATEMENT_TIMEOUT_MS
    ):
        pass
    [(query, args)] = conn.executed
    assert "app.role_level" not in query
    assert "app.tenant_id" in query
    assert args == (str(tenant), str(WORKER_STATEMENT_TIMEOUT_MS))


@pytest.mark.parametrize("timeout_ms", [100, 60_000])
async def test_accepts_the_timeout_bounds(timeout_ms: int) -> None:
    conn = FakeConnection()
    async with rls_transaction(conn, uuid4(), 1, statement_timeout_ms=timeout_ms):
        pass
    assert conn.executed[0][1][1] == str(timeout_ms)


def test_named_timeout_defaults() -> None:
    assert (API_STATEMENT_TIMEOUT_MS, WORKER_STATEMENT_TIMEOUT_MS) == (2_000, 30_000)
