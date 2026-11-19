"""Alembic environment: connects as app_owner and runs migrations in one transaction."""

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool, text

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Locally the URL comes from .env; in CI it is a real environment variable.
# load_dotenv never overrides a variable that is already set.
load_dotenv()


def owner_url() -> str:
    url = os.environ.get("DATABASE_URL_OWNER", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL_OWNER is not set (run db/bootstrap.py first).")
    # SQLAlchemy needs the driver named in the scheme; we use psycopg 3.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: print the SQL instead of running it."""
    context.configure(url=owner_url(), literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # NullPool: one short-lived connection, nothing kept open after the run.
    engine = create_engine(owner_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # Fail fast instead of hanging if a table is locked by another session,
        # and cap any single statement. Session-level SET is fine here because
        # migrations use the direct (non-pooled) endpoint.
        connection.execute(text("SET lock_timeout = '5s'"))
        connection.execute(text("SET statement_timeout = '60s'"))
        # SQLAlchemy auto-began a transaction for the SETs. Commit it, or Alembic
        # would see an open transaction, skip its own, and the migration would be
        # rolled back when the connection closes. The settings outlive the commit.
        connection.commit()

        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
