"""Embeddings from Bedrock Titan Text Embeddings v2 (SPEC: Ingestion, worker step 6).

Titan v2 embeds one text per InvokeModel call, so a "batch" is up to 32 calls run on a small
thread pool; the caller writes each batch to the DB before starting the next, so a crash
loses at most one batch of work.

Retries come from botocore's "standard" retry mode: it retries throttling (429) and 5xx
errors with exponential backoff and full jitter, 5 attempts in total. Errors that survive
the retries are raised and treated as transient by the worker.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

EMBEDDING_DIMENSIONS = 1024  # must match chunks.embedding vector(1024)
# Concurrent calls within one batch. Small on purpose: the worker's reserved concurrency
# is 2, so at most 16 calls are in flight across the whole service.
MAX_PARALLEL_CALLS = 8

# total_max_attempts counts the first try; botocore's "max_attempts" would count retries only
# (max_attempts=5 means 6 tries).
_RETRY_CONFIG = Config(
    retries={"mode": "standard", "total_max_attempts": 5},
    connect_timeout=5,
    read_timeout=30,
)


def make_bedrock_client(region: str):
    """A bedrock-runtime client. Credentials come from the environment (IAM role on Lambda,
    the AWS_PROFILE login session locally). Clients are thread-safe; create one and reuse it.
    """
    return boto3.client("bedrock-runtime", region_name=region, config=_RETRY_CONFIG)


def embed_batch(client, texts: list[str], *, model_id: str) -> list[list[float]]:
    """Embed texts; the result is in the same order as the input."""

    def embed_one(text: str) -> list[float]:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            # normalize: unit-length vectors, so cosine distance in pgvector is well defined.
            body=json.dumps(
                {"inputText": text, "dimensions": EMBEDDING_DIMENSIONS, "normalize": True}
            ),
        )
        vector = json.loads(response["body"].read())["embedding"]
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"expected {EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}")
        return vector

    if not texts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, len(texts))) as pool:
        return list(pool.map(embed_one, texts))  # map() keeps input order
