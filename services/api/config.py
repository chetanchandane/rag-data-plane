"""API settings, read once at startup from environment variables.

The DB URL comes from one of two places, never both:
  DATABASE_URL_API        the URL itself: the dev branch, from .env;
  DATABASE_URL_API_PARAM  an SSM SecureString name: production. The URL is read once at
                          startup and kept in memory only, like the worker's (and the API
                          Lambda's in Slice 2), so it's never in .env or a plaintext env var.
"""

import os
from dataclasses import dataclass
from uuid import UUID

import boto3

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
    def from_env(cls, ssm=None) -> "Settings":
        """Fail at startup, not on the first request, if anything is missing or malformed.
        ssm: an SSM client, for tests; by default one is created if it's needed."""
        role_level = int(_required("DEV_ROLE_LEVEL"))
        if not 1 <= role_level <= 3:
            raise ValueError("DEV_ROLE_LEVEL must be 1, 2 or 3")
        return cls(
            database_url=_database_url(ssm),
            raw_bucket=_required("RAW_BUCKET"),
            aws_region=_required("AWS_REGION"),
            dev_auth_token=_required("DEV_AUTH_TOKEN"),
            dev_tenant_id=UUID(_required("DEV_TENANT_ID")),
            dev_role_level=role_level,
        )


def _database_url(ssm) -> str:
    url = os.environ.get("DATABASE_URL_API", "").strip()
    param = os.environ.get("DATABASE_URL_API_PARAM", "").strip()
    # Both set would leave it unclear which database the API is talking to.
    if bool(url) == bool(param):
        raise RuntimeError("set exactly one of DATABASE_URL_API or DATABASE_URL_API_PARAM")
    if url:
        return url
    ssm = ssm or boto3.client("ssm", region_name=_required("AWS_REGION"))
    return ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value
