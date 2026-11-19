"""One-time setup of a Neon dev branch, run as Neon's default owner role.

    uv run python db/bootstrap.py

Creates what migrations must not own, because `alembic downgrade` would drop it:
  - login roles app_owner, app_api, app_ingest (no superuser, no BYPASSRLS)
  - CREATE on schema public for app_owner only
  - the vector extension (on Neon only the privileged role can create extensions)

Passwords: reused from .env if a URL is already there, otherwise generated. The script
writes the DATABASE_URL_* lines into .env and never prints a password. Safe to re-run.

Dev only. Neon hostnames don't contain the branch name, so the guard compares the
endpoint ID in NEON_ADMIN_URL with NEON_DEV_ENDPOINT_ID. Production role passwords
live in SSM Parameter Store, never in .env; this script is not used for production.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from dotenv import dotenv_values, load_dotenv, set_key
from psycopg import sql

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# role -> (.env key, which Neon endpoint the role uses)
ROLES = {
    "app_owner": ("DATABASE_URL_OWNER", "direct"),  # migrations: direct, non-pooled
    "app_api": ("DATABASE_URL_API", "pooled"),
    "app_ingest": ("DATABASE_URL_INGEST", "pooled"),
}

# CA bundle used to verify Neon's certificate (sslmode=verify-full). Named as a file because
# sslrootcert=system fails with psycopg-binary's bundled OpenSSL on macOS.
# macOS: /etc/ssl/cert.pem. Debian/Ubuntu (CI): /etc/ssl/certs/ca-certificates.crt.
CA_BUNDLE = "/etc/ssl/cert.pem"

# The owner URL is used by psycopg/libpq (Alembic), which also supports SCRAM channel binding.
LIBPQ_PARAMS = f"sslmode=verify-full&sslrootcert={CA_BUNDLE}&channel_binding=require"
# The app roles are used by asyncpg, which does not understand channel_binding.
ASYNCPG_PARAMS = f"sslmode=verify-full&sslrootcert={CA_BUNDLE}"


def endpoint_id(host: str) -> str:
    """'ep-abc-123-pooler.c-16.us-east-1.aws.neon.tech' -> 'ep-abc-123'."""
    return host.split(".", 1)[0].removesuffix("-pooler")


def check_dev_branch(host: str, override: bool) -> None:
    expected = os.environ.get("NEON_DEV_ENDPOINT_ID", "").strip()
    actual = endpoint_id(host)
    if override:
        print(f"WARNING: --i-know-this-is-dev set, skipping the dev-branch check ({actual}).")
        return
    if not expected:
        sys.exit("Refusing to run: NEON_DEV_ENDPOINT_ID is not set in .env.")
    if actual != expected:
        sys.exit(f"Refusing to run: endpoint {actual} is not the dev endpoint {expected}.")
    print(f"Dev branch check passed (endpoint {actual}).")


def build_url(role: str, password: str, host: str, dbname: str, params: str) -> str:
    # token_urlsafe passwords contain only [A-Za-z0-9_-], so no URL escaping is needed.
    return f"postgresql://{role}:{password}@{host}/{dbname}?{params}"


def check_role_attributes(conn: psycopg.Connection, role: str) -> None:
    row = conn.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls,
               EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'neon_superuser')
                 AND pg_has_role(%s, 'neon_superuser', 'MEMBER')
        FROM pg_roles WHERE rolname = %s
        """,
        (role, role),
    ).fetchone()
    login, superuser, createrole, createdb, replication, bypassrls, neon_super = row
    if not login or superuser or createrole or createdb or replication or bypassrls or neon_super:
        sys.exit(f"Role {role} has unexpected privileges: {row}. Fix it by hand, then re-run.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--i-know-this-is-dev", action="store_true")
    args = parser.parse_args()

    load_dotenv(ENV_FILE)
    admin_url = os.environ.get("NEON_ADMIN_URL", "").strip()
    if not admin_url:
        sys.exit("NEON_ADMIN_URL is not set in .env.")
    admin = urlsplit(admin_url)
    check_dev_branch(admin.hostname, args.i_know_this_is_dev)

    ep = endpoint_id(admin.hostname)
    domain = admin.hostname.split(".", 1)[1]
    hosts = {"direct": f"{ep}.{domain}", "pooled": f"{ep}-pooler.{domain}"}
    dbname = admin.path.lstrip("/")
    current_env = dotenv_values(ENV_FILE)
    passwords: dict[str, str] = {}

    # autocommit: each statement stands alone; CREATE ROLE is cluster-level anyway.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for role, (env_key, endpoint) in ROLES.items():
            existing_url = (current_env.get(env_key) or "").strip()
            password = urlsplit(existing_url).password if existing_url else None
            password = password or secrets.token_urlsafe(32)
            passwords[role] = password

            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            # CREATE/ALTER ROLE cannot take bind parameters, so the password is quoted with
            # sql.Literal (driver-side escaping) and the name with sql.Identifier.
            if exists is None:
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                print(f"Created role {role}.")
            else:
                # .env is the source of truth for dev passwords; re-setting is harmless.
                conn.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                source = "synced from .env" if existing_url else "newly generated"
                print(f"Role {role} exists; password {source}.")
            check_role_attributes(conn, role)

            if not existing_url:
                params = LIBPQ_PARAMS if endpoint == "direct" else ASYNCPG_PARAMS
                url = build_url(role, password, hosts[endpoint], dbname, params)
                set_key(ENV_FILE, env_key, url, quote_mode="never")
                print(f"Wrote {env_key} to .env.")

        # PG15+ already defaults to this; stated explicitly so nobody else can create objects.
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("GRANT CREATE ON SCHEMA public TO app_owner")
        print("Schema public: CREATE revoked from PUBLIC, granted to app_owner.")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        version = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()[0]
        print(f"Extension vector {version} present.")

    ENV_FILE.chmod(0o600)

    # Prove each role can log in over its own endpoint with full certificate verification.
    for role, (_, endpoint) in ROLES.items():
        url = build_url(role, passwords[role], hosts[endpoint], dbname, LIBPQ_PARAMS)
        with psycopg.connect(url, connect_timeout=15) as conn:
            user = conn.execute("SELECT current_user").fetchone()[0]
        print(f"Login check: {user} via {endpoint} endpoint, verify-full OK.")


if __name__ == "__main__":
    main()
