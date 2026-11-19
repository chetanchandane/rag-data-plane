"""Postgres access for the API and the worker: a pool factory and the RLS transaction.

All tenant data is read and written inside rls_transaction(). It is the only place the
request context (tenant, role level) and the statement timeout are set.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
from pgvector.asyncpg import register_vector

# The app may only connect as these roles. Both are NOBYPASSRLS; the owner roles are not.
APP_ROLES = frozenset({"app_api", "app_ingest"})

# SPEC: Query path, "Postgres: 2 s statement timeout".
API_STATEMENT_TIMEOUT_MS = 2_000
# The worker's statements are heavier (bulk chunk inserts, cleanup of superseded chunks).
WORKER_STATEMENT_TIMEOUT_MS = 30_000
# 0 would DISABLE the timeout in Postgres, so it is rejected along with anything outside
# these bounds.
MIN_STATEMENT_TIMEOUT_MS = 100
MAX_STATEMENT_TIMEOUT_MS = 60_000

# SET LOCAL can't take bind parameters; set_config can. is_local = true: the settings end
# with the transaction. With Neon's pooled (transaction-mode) endpoint a session-level
# setting could leak into the next request served by the same server connection.
_SET_CONTEXT_WITH_ROLE = """
    SELECT set_config('app.tenant_id', $1, true),
           set_config('statement_timeout', $2, true),
           set_config('app.role_level', $3, true)
"""
# No role level: app.role_level stays unset, so app_role_level() returns 0 and every
# role-checked policy (all of app_api's reads) matches nothing. Fail closed.
_SET_CONTEXT_WITHOUT_ROLE = """
    SELECT set_config('app.tenant_id', $1, true),
           set_config('statement_timeout', $2, true)
"""


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Runs once for every new connection the pool opens."""
    # Refuse to run as anything that might bypass RLS (e.g. a DSN pasted with the owner role).
    user = await conn.fetchval("SELECT current_user")
    if user not in APP_ROLES:
        raise RuntimeError(f"refusing to connect as {user!r}; expected one of {sorted(APP_ROLES)}")
    # Embeddings are sent and received as vectors (binary codec), never as formatted strings.
    await register_vector(conn)


async def create_pool(dsn: str, *, min_size: int = 0, max_size: int = 2) -> asyncpg.Pool:
    """Create a connection pool for app_api or app_ingest.

    Callers keep the pool at module scope so warm Lambda invocations reuse it. Defaults per
    SPEC: min_size=0 (no idle connections held), max_size=2. The DSN should point at Neon's
    pooled endpoint so many Lambda instances share few server connections.
    """
    return await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, init=_init_connection
    )


@asynccontextmanager
async def rls_transaction(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    role_level: int | None,
    *,
    statement_timeout_ms: int = API_STATEMENT_TIMEOUT_MS,
) -> AsyncIterator[None]:
    """Open a transaction with the request context set for RLS.

        async with pool.acquire() as conn:
            async with rls_transaction(conn, claims.tenant_id, claims.role_level):
                rows = await conn.fetch(...)

    tenant_id and role_level must come from verified claims, never from the request body.

    role_level has no default on purpose, so every caller states it:
      - the API always passes the role level from the claims (1 to 3);
      - the worker passes None. app_ingest's policies check the tenant only, and with no
        role level set, anything role-checked sees zero rows.
    """
    # Strict types: a str tenant_id could be anything the caller was handed; a UUID object
    # was parsed somewhere on purpose. bool is excluded because True is an int in Python.
    if not isinstance(tenant_id, UUID):
        raise TypeError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")
    if role_level is not None:
        if isinstance(role_level, bool) or not isinstance(role_level, int):
            raise TypeError(f"role_level must be an int or None, got {type(role_level).__name__}")
        if not 1 <= role_level <= 3:
            raise ValueError(f"role_level must be between 1 and 3, got {role_level}")
    if isinstance(statement_timeout_ms, bool) or not isinstance(statement_timeout_ms, int):
        raise TypeError("statement_timeout_ms must be an int")
    if not MIN_STATEMENT_TIMEOUT_MS <= statement_timeout_ms <= MAX_STATEMENT_TIMEOUT_MS:
        raise ValueError(
            f"statement_timeout_ms must be between {MIN_STATEMENT_TIMEOUT_MS} and "
            f"{MAX_STATEMENT_TIMEOUT_MS}, got {statement_timeout_ms}"
        )
    # Nested, conn.transaction() would be a savepoint and the outer transaction's settings
    # would still apply: an inner role_level=None would inherit the outer role instead of
    # failing closed. One context per transaction, so refuse.
    if conn.is_in_transaction():
        raise RuntimeError("rls_transaction cannot be nested inside another transaction")

    async with conn.transaction():
        if role_level is None:
            await conn.execute(_SET_CONTEXT_WITHOUT_ROLE, str(tenant_id), str(statement_timeout_ms))
        else:
            await conn.execute(
                _SET_CONTEXT_WITH_ROLE,
                str(tenant_id),
                str(statement_timeout_ms),
                str(role_level),
            )
        yield
