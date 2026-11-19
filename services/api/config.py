"""API settings, read once at startup from environment variables.

Slice 1 runs the API locally, so everything comes from the environment (.env). When the API
moves to Lambda (Slice 2), the DB URL comes from SSM like the worker's, never from a
plaintext Lambda environment variable.
"""

import os
from dataclasses import dataclass
from uuid import UUID

# Largest PDF the API registers (DECISIONS 2026-11-19). The presigned URL signs the
# Content-Length, so S3 refuses an upload of any other size.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# How long a presigned upload URL stays valid.
UPLOAD_URL_EXPIRES_SECONDS = 15 * 60


@dataclass(frozen=True)
class Settings:
    database_url: str  # app_api role, pooled endpoint
    raw_bucket: str
    aws_region: str
    # Slice 1 auth stub (CLAUDE.md "Auth in Slice 1"); replaced by JWT claims in Slice 2.
    dev_auth_token: str
    dev_tenant_id: UUID
    dev_role_level: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Fail at startup, not on the first request, if anything is missing or malformed."""
        role_level = int(_required("DEV_ROLE_LEVEL"))
        if not 1 <= role_level <= 3:
            raise ValueError("DEV_ROLE_LEVEL must be 1, 2 or 3")
        return cls(
            database_url=_required("DATABASE_URL_API"),
            raw_bucket=_required("RAW_BUCKET"),
            aws_region=_required("AWS_REGION"),
            dev_auth_token=_required("DEV_AUTH_TOKEN"),
            dev_tenant_id=UUID(_required("DEV_TENANT_ID")),
            dev_role_level=role_level,
        )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value
