"""Connection-string helpers shared by DB tests (marked `db`)."""

import os

import pytest
from dotenv import load_dotenv

# Locally the URLs come from .env; in CI they are real environment variables.
load_dotenv()


def db_url(key: str) -> str:
    url = os.environ.get(key, "").strip()
    if not url:
        pytest.fail(f"{key} is not set (run db/bootstrap.py)")
    return url


def direct_url(pooled_url: str) -> str:
    """Same role and database, but Neon's direct endpoint instead of the pooler.

    Neon names the pooled host '<endpoint>-pooler.<region domain>'.
    """
    return pooled_url.replace("-pooler.", ".", 1)
