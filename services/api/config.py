"""API settings, read once at startup from environment variables.

The DB URL comes from one of two places, never both:
  DATABASE_URL_API        the URL itself: the dev branch, from .env;
  DATABASE_URL_API_PARAM  an SSM SecureString name: production. The URL is read once at
                          startup and kept in memory only, like the worker's (and the API
                          Lambda's in Slice 2), so it's never in .env or a plaintext env var.
"""

import os
from dataclasses import dataclass

import boto3

# Largest PDF the API registers (DECISIONS 2026-11-19). The presigned URL signs the
# Content-Length, so S3 refuses an upload of any other size.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# How long a presigned upload URL stays valid.
UPLOAD_URL_EXPIRES_SECONDS = 15 * 60

# /v1/query defaults (DECISIONS 2026-11-22, "Step 15"). Both are tuned on the golden set.
DEFAULT_ABSTAIN_THRESHOLD = 0.3  # the top rerank score below which the API abstains
DEFAULT_LLM_CONTEXT_CHUNKS = 5  # SPEC: Query flow, "rerank (top 5)"
MAX_LLM_CONTEXT_CHUNKS = 30  # can't exceed what recall returns
EFFORT_LEVELS = ("low", "medium", "high")


@dataclass(frozen=True)
class Settings:
    database_url: str  # app_api role, pooled endpoint
    raw_bucket: str
    aws_region: str
    # Must be the worker's model, or query and chunk vectors aren't comparable.
    embedding_model_id: str
    rerank_model_id: str  # Cohere model, e.g. rerank-v5.0-fast
    # Bedrock inference profile IDs (us.*), called through the Converse API.
    llm_primary_model_id: str
    llm_secondary_model_id: str
    # Claude's effort level. Sent only to Anthropic models (api/llm.py).
    llm_primary_effort: str = "low"
    abstain_threshold: float = DEFAULT_ABSTAIN_THRESHOLD
    llm_context_chunks: int = DEFAULT_LLM_CONTEXT_CHUNKS
    # The token issuer the API verifies tokens against when no API Gateway is in front
    # (api/auth.py). Not set on Lambda: there API Gateway's authorizer verifies them.
    jwt_issuer: str | None = None

    @classmethod
    def from_env(cls, ssm=None) -> "Settings":
        """Fail at startup, not on the first request, if anything is missing or malformed.
        ssm: an SSM client, for tests; by default one is created if it's needed."""
        return cls(
            database_url=_database_url(ssm),
            raw_bucket=_required("RAW_BUCKET"),
            aws_region=_required("AWS_REGION"),
            embedding_model_id=_required("EMBEDDING_MODEL_ID"),
            rerank_model_id=_required("RERANK_MODEL_ID"),
            llm_primary_model_id=_required("LLM_PRIMARY_MODEL_ID"),
            llm_secondary_model_id=_required("LLM_SECONDARY_MODEL_ID"),
            llm_primary_effort=_effort(),
            abstain_threshold=_threshold(),
            llm_context_chunks=_context_chunks(),
            jwt_issuer=_issuer(),
        )


def _effort() -> str:
    effort = os.environ.get("LLM_PRIMARY_EFFORT", "").strip() or "low"
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"LLM_PRIMARY_EFFORT must be one of {', '.join(EFFORT_LEVELS)}")
    return effort


def _threshold() -> float:
    raw = os.environ.get("ABSTAIN_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_ABSTAIN_THRESHOLD
    threshold = float(raw)
    # Rerank relevance scores are in [0, 1], so anything else is a typo.
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("ABSTAIN_THRESHOLD must be between 0 and 1")
    return threshold


def _context_chunks() -> int:
    raw = os.environ.get("LLM_CONTEXT_CHUNKS", "").strip()
    if not raw:
        return DEFAULT_LLM_CONTEXT_CHUNKS
    chunks = int(raw)
    if not 1 <= chunks <= MAX_LLM_CONTEXT_CHUNKS:
        raise ValueError(f"LLM_CONTEXT_CHUNKS must be 1 to {MAX_LLM_CONTEXT_CHUNKS}")
    return chunks


def _issuer() -> str | None:
    issuer = os.environ.get("JWT_ISSUER", "").strip()
    if not issuer:
        return None
    # A token's iss must equal this exactly, so https://x/ and https://x would differ.
    if not issuer.startswith("https://") or issuer.endswith("/"):
        raise ValueError("JWT_ISSUER must be an https URL with no trailing slash")
    return issuer


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
