"""Catalog guard: every table must have RLS enabled and forced, and none may grant to PUBLIC.

Reads only the system catalogs, so it catches a future migration that adds a table without
RLS, even before any behavioural test is written for it. Connects as app_owner (the role
migrations run as); catalogs are readable by any role.
"""

import os

import psycopg
import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.db

# Tables that hold no tenant data and are exempt from the RLS requirement.
RLS_EXEMPT = {"alembic_version"}

# Guards against a vacuous pass (e.g. the migration never ran and there are no tables).
EXPECTED_TENANT_TABLES = {
    "documents",
    "document_versions",
    "chunks",
    "tenant_index_state",
    "query_cache",
    "feedback",
}


@pytest.fixture(scope="module")
def conn():
    load_dotenv()
    url = os.environ.get("DATABASE_URL_OWNER", "").strip()
    if not url:
        pytest.fail("DATABASE_URL_OWNER is not set")
    # autocommit: each check sees committed state; the negative test opens its own transaction.
    with psycopg.connect(url, connect_timeout=15, autocommit=True) as connection:
        yield connection


def public_tables(conn: psycopg.Connection) -> dict[str, tuple[bool, bool]]:
    """name -> (relrowsecurity, relforcerowsecurity) for ordinary and partitioned tables."""
    rows = conn.execute(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        """
    ).fetchall()
    return {name: (enabled, forced) for name, enabled, forced in rows}


def tables_without_forced_rls(conn: psycopg.Connection) -> set[str]:
    return {
        name
        for name, (enabled, forced) in public_tables(conn).items()
        if name not in RLS_EXEMPT and not (enabled and forced)
    }


def tables_granting_to_public(conn: psycopg.Connection) -> set[str]:
    # aclexplode lists each grant; grantee 0 means PUBLIC. A NULL relacl (default
    # privileges: owner only) produces no rows.
    rows = conn.execute(
        """
        SELECT DISTINCT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND a.grantee = 0
        """
    ).fetchall()
    return {name for (name,) in rows}


def test_expected_tables_exist(conn: psycopg.Connection) -> None:
    missing = EXPECTED_TENANT_TABLES - public_tables(conn).keys()
    assert not missing, f"tables missing (did the migration run?): {sorted(missing)}"


def test_every_table_has_rls_enabled_and_forced(conn: psycopg.Connection) -> None:
    bad = tables_without_forced_rls(conn)
    assert not bad, f"tables without ENABLE + FORCE ROW LEVEL SECURITY: {sorted(bad)}"


def test_no_table_grants_to_public(conn: psycopg.Connection) -> None:
    bad = tables_granting_to_public(conn)
    assert not bad, f"tables with privileges granted to PUBLIC: {sorted(bad)}"


def test_checks_detect_a_table_without_rls(conn: psycopg.Connection) -> None:
    """Proves the two checks above can fail: add a bad table, look, then roll it back."""
    with conn.transaction(force_rollback=True):
        conn.execute("CREATE TABLE rls_guard_probe (id int)")
        conn.execute("GRANT SELECT ON rls_guard_probe TO PUBLIC")
        assert "rls_guard_probe" in tables_without_forced_rls(conn)
        assert "rls_guard_probe" in tables_granting_to_public(conn)

        # Enabling RLS alone is not enough; FORCE is required too.
        conn.execute("ALTER TABLE rls_guard_probe ENABLE ROW LEVEL SECURITY")
        assert "rls_guard_probe" in tables_without_forced_rls(conn)

    assert "rls_guard_probe" not in public_tables(conn), "probe table was not rolled back"
