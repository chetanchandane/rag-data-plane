"""One-time setup of a Neon branch, run as Neon's default owner role.

    uv run python db/bootstrap.py                                     # dev branch
    uv run python db/bootstrap.py --production --aws-account-id <id>  # production branch

Creates what migrations must not own, because `alembic downgrade` would drop it:
  - login roles app_owner, app_api, app_ingest (no superuser, no BYPASSRLS)
  - CREATE on schema public for app_owner only
  - the vector extension (on Neon only the privileged role can create extensions)

Dev: NEON_ADMIN_URL comes from .env. Passwords are reused from .env if a URL is already
there, otherwise generated, and the DATABASE_URL_* lines are written to .env. Safe to
re-run. Neon hostnames don't contain the branch name, so the guard compares the endpoint ID
in NEON_ADMIN_URL with NEON_DEV_ENDPOINT_ID.

Production: the owner URL is typed at a hidden prompt, never read from or written to .env.
Every run generates new passwords (a rotation) and stores the three role URLs in SSM
Parameter Store as SecureStrings; nothing else keeps them. Then it runs the migrations as
app_owner. It refuses the dev endpoint, checks the AWS account, and asks you to type the
endpoint ID before changing anything. Needs an interactive terminal.

No mode ever prints a password.
"""

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from dotenv import dotenv_values, load_dotenv, set_key
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# role -> (.env key, which Neon endpoint the role uses)
ROLES = {
    "app_owner": ("DATABASE_URL_OWNER", "direct"),  # migrations: direct, non-pooled
    "app_api": ("DATABASE_URL_API", "pooled"),
    "app_ingest": ("DATABASE_URL_INGEST", "pooled"),
}

# Production role URLs: SSM names, matching infra/modules/secrets.
PRODUCTION_PARAMETERS = {
    "app_owner": "/ragdp/prod/database_url_owner",
    "app_api": "/ragdp/prod/database_url_api",
    "app_ingest": "/ragdp/prod/database_url_ingest",
}

# CA bundle used to verify Neon's certificate (sslmode=verify-full). Named as a file because
# sslrootcert=system fails with psycopg-binary's bundled OpenSSL on macOS.
# macOS and the Lambda base image (Amazon Linux 2023): /etc/ssl/cert.pem.
# Debian/Ubuntu (CI): /etc/ssl/certs/ca-certificates.crt.
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


def check_production_endpoint(host: str, dev_endpoint: str) -> str:
    """The mirror of check_dev_branch: production must NOT be the dev endpoint, or the dev
    passwords in .env would be rotated away and dev's URLs would land in SSM."""
    if not dev_endpoint:
        sys.exit("Refusing to run: NEON_DEV_ENDPOINT_ID is not set, so dev can't be ruled out.")
    actual = endpoint_id(host)
    if actual == dev_endpoint:
        sys.exit(f"Refusing to run: {actual} is the dev endpoint, not production.")
    return actual


def build_url(role: str, password: str, host: str, dbname: str, params: str) -> str:
    # token_urlsafe passwords contain only [A-Za-z0-9_-], so no URL escaping is needed.
    return f"postgresql://{role}:{password}@{host}/{dbname}?{params}"


def role_urls(passwords: dict[str, str], admin_host: str, dbname: str) -> dict[str, str]:
    """The URL each role connects with: owner on the direct endpoint for libpq, app roles on
    the pooled endpoint for asyncpg."""
    ep = endpoint_id(admin_host)
    domain = admin_host.split(".", 1)[1]
    hosts = {"direct": f"{ep}.{domain}", "pooled": f"{ep}-pooler.{domain}"}
    urls = {}
    for role, (_, endpoint) in ROLES.items():
        params = LIBPQ_PARAMS if endpoint == "direct" else ASYNCPG_PARAMS
        urls[role] = build_url(role, passwords[role], hosts[endpoint], dbname, params)
    return urls


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


def ensure_role(conn: psycopg.Connection, role: str, password: str) -> bool:
    """Create the role, or set its password if it exists. Returns True if it was created."""
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
    else:
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )
    check_role_attributes(conn, role)
    return exists is None


def prepare_schema(conn: psycopg.Connection) -> None:
    # PG15+ already defaults to this; stated explicitly so nobody else can create objects.
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    conn.execute("GRANT CREATE ON SCHEMA public TO app_owner")
    print("Schema public: CREATE revoked from PUBLIC, granted to app_owner.")
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    version = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()[0]
    print(f"Extension vector {version} present.")


def as_libpq(url: str) -> str:
    """The same URL with libpq's parameters (adds channel_binding), for psycopg."""
    base, _, params = url.partition("?")
    return url if params == LIBPQ_PARAMS else f"{base}?{LIBPQ_PARAMS}"


def check_logins(urls: dict[str, str]) -> None:
    """Prove each role can log in over its own endpoint with full certificate verification.
    The check uses psycopg, so every URL gets libpq's parameters."""
    for role, url in urls.items():
        with psycopg.connect(as_libpq(url), connect_timeout=15) as conn:
            user = conn.execute("SELECT current_user").fetchone()[0]
        print(f"Login check: {user} via the {ROLES[role][1]} endpoint, verify-full OK.")


def bootstrap_dev(override: bool) -> None:
    admin_url = os.environ.get("NEON_ADMIN_URL", "").strip()
    if not admin_url:
        sys.exit("NEON_ADMIN_URL is not set in .env.")
    admin = urlsplit(admin_url)
    check_dev_branch(admin.hostname, override)

    current_env = dotenv_values(ENV_FILE)
    passwords = {}
    for role, (env_key, _) in ROLES.items():
        existing_url = (current_env.get(env_key) or "").strip()
        existing = urlsplit(existing_url).password if existing_url else None
        passwords[role] = existing or secrets.token_urlsafe(32)
    urls = role_urls(passwords, admin.hostname, admin.path.lstrip("/"))

    # autocommit: each statement stands alone; CREATE ROLE is cluster-level anyway.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for role, (env_key, _) in ROLES.items():
            # .env is the source of truth for dev passwords; re-setting is harmless.
            created = ensure_role(conn, role, passwords[role])
            source = "synced from .env" if current_env.get(env_key) else "newly generated"
            print(f"Role {role} {'created' if created else 'exists'}; password {source}.")
            if not (current_env.get(env_key) or "").strip():
                set_key(ENV_FILE, env_key, urls[role], quote_mode="never")
                print(f"Wrote {env_key} to .env.")
        prepare_schema(conn)

    ENV_FILE.chmod(0o600)
    check_logins(urls)


def bootstrap_production(aws_account_id: str) -> None:
    import boto3  # only production mode talks to AWS

    if not sys.stdin.isatty():
        sys.exit("Production mode needs an interactive terminal (hidden prompt, confirmation).")

    session = boto3.session.Session(region_name=os.environ.get("AWS_REGION") or None)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != aws_account_id:
        sys.exit(
            f"Refusing to run: AWS credentials are for account {account}, not {aws_account_id}."
        )
    ssm = session.client("ssm")

    admin_url = getpass.getpass(
        "Neon owner URL for the PRODUCTION branch (direct endpoint, not shown): "
    ).strip()
    admin = urlsplit(admin_url)
    if not admin.hostname or not admin.path.lstrip("/"):
        sys.exit("That doesn't look like a postgresql:// URL with a database name.")
    ep = check_production_endpoint(
        admin.hostname, os.environ.get("NEON_DEV_ENDPOINT_ID", "").strip()
    )

    print(f"\nAbout to rotate the passwords of {', '.join(ROLES)} on Neon endpoint {ep},")
    print(f"store their URLs in SSM ({session.region_name}, account {account}):")
    for name in PRODUCTION_PARAMETERS.values():
        print(f"  {name}")
    print("and run the migrations. A worker that is running keeps the old URL until its next")
    print("cold start, so run this while nothing is ingesting.")
    if input(f"Type the endpoint ID ({ep}) to continue: ").strip() != ep:
        sys.exit("Not confirmed; nothing changed.")

    passwords = {role: secrets.token_urlsafe(32) for role in ROLES}
    urls = role_urls(passwords, admin.hostname, admin.path.lstrip("/"))

    # SSM first: a password is never set on a role without being stored somewhere. If the
    # run stops halfway, re-running rotates again and puts everything back in step.
    for role, name in PRODUCTION_PARAMETERS.items():
        ssm.put_parameter(Name=name, Value=urls[role], Type="SecureString", Overwrite=True)
        print(f"Stored {name}.")

    with psycopg.connect(admin_url, autocommit=True) as conn:
        for role in ROLES:
            created = ensure_role(conn, role, passwords[role])
            print(f"Role {role} {'created' if created else 'exists'}; password rotated.")
        prepare_schema(conn)
    check_logins(urls)

    # Alembic's env.py reads DATABASE_URL_OWNER. Set it in this process only, after
    # load_dotenv, so the dev owner URL in .env can't be used by mistake.
    os.environ["DATABASE_URL_OWNER"] = urls["app_owner"]
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with psycopg.connect(urls["app_owner"], connect_timeout=15) as conn:
        head = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    print(f"Migrations at {head} on {ep}. Done: the URLs exist only in SSM.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--i-know-this-is-dev", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--aws-account-id", help="required with --production")
    args = parser.parse_args()

    load_dotenv(ENV_FILE)
    if args.production:
        if args.i_know_this_is_dev or not args.aws_account_id:
            parser.error("--production needs --aws-account-id and excludes --i-know-this-is-dev")
        bootstrap_production(args.aws_account_id)
    else:
        bootstrap_dev(args.i_know_this_is_dev)


if __name__ == "__main__":
    main()
