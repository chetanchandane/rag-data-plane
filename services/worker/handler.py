"""SQS entry point of the ingest worker Lambda (SPEC: Ingestion, "Failure handling").

Thin on purpose: it turns one queue message into one ingest_version() call (the same function
the local runner uses) and decides what a failure means for the message:

  Permanent error -> mark the version failed (ingest_version does this if it had claimed it),
                     send the original message to the DLQ with a short reason, then return
                     normally so Lambda deletes it from the source queue. If the DLQ send
                     fails, raise: SQS retries and nothing is lost.
  Anything else   -> raise. SQS redelivers it up to maxReceiveCount (3), then moves it to the
                     DLQ itself.

The queue's batch size is 1 (one document per invocation), so raising retries one message.
"""

import asyncio
import functools
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import boto3
from pydantic import ValidationError

from shared.db import create_pool
from shared.errors import MAX_REASON_LENGTH, PermanentError
from shared.jobs import IngestFileJob, parse_ingest_job
from worker.embedding import embed_batch, make_bedrock_client
from worker.pipeline import IngestResult, ingest_version

log = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.INFO)  # Lambda's root logger defaults to WARNING

RunIngest = Callable[[IngestFileJob], Awaitable[IngestResult]]
SendToDlq = Callable[[str, str], None]  # (original body, reason)


def _validation_reason(exc: ValidationError) -> str:
    """Where and why validation failed, never the input itself: a message body is untrusted
    and must not be copied into the DLQ attribute or logs."""
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "body"
    return f"invalid_message: {first['type']} at {where}"[:MAX_REASON_LENGTH]


async def process_message(
    body: str, *, raw_bucket: str, run_ingest: RunIngest, send_to_dlq: SendToDlq
) -> str:
    """Handle one message body. Returns the outcome for the log: indexed, superseded,
    skipped or dead_lettered. Raises on a transient error, so SQS retries."""
    try:
        job = parse_ingest_job(body)
    except ValidationError as exc:
        # No row is touched: an invalid message has no claim (SPEC: mark failed only after one).
        return await _dead_letter(send_to_dlq, body, _validation_reason(exc), job_id=None)

    try:
        if not isinstance(job, IngestFileJob):
            # Reindex and delete jobs come with the endpoints that produce them (Slice 2;
            # reindex semantics are Open decision #8). Visible in the DLQ, never dropped.
            raise PermanentError("unsupported_job_type", f"{job.job_type} jobs aren't handled yet")
        if job.bucket != raw_bucket:
            raise PermanentError("wrong_bucket", "the message names a bucket other than raw")
        result = await run_ingest(job)
    except PermanentError as exc:
        return await _dead_letter(send_to_dlq, body, exc.reason, job_id=job.job_id)

    log.info("job %s: %s, %d chunks", job.job_id, result.outcome, result.chunk_count)
    return result.outcome


async def _dead_letter(
    send_to_dlq: SendToDlq, body: str, reason: str, *, job_id: str | None
) -> str:
    # If this raises, the handler raises and SQS redelivers the message: nothing is lost.
    await asyncio.to_thread(send_to_dlq, body, reason)
    log.warning("job %s sent to the DLQ: %s", job_id, reason)
    return "dead_lettered"


# --- Lambda wiring -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    raw_bucket: str
    dlq_url: str
    embedding_model_id: str
    database_url: str
    s3: Any
    sqs: Any
    bedrock: Any


@functools.cache
def _deps() -> _Deps:
    """Built once per container (cold start) and reused by warm invocations."""
    region = os.environ["AWS_REGION"]  # set by Lambda
    ssm = boto3.client("ssm", region_name=region)
    # The DB URL holds a password, so it lives in SSM as a SecureString (SPEC: Secrets), not
    # in a plaintext environment variable. Fetched once and kept in memory.
    database_url = ssm.get_parameter(Name=os.environ["DATABASE_URL_PARAM"], WithDecryption=True)[
        "Parameter"
    ]["Value"]
    return _Deps(
        raw_bucket=os.environ["RAW_BUCKET"],
        dlq_url=os.environ["DLQ_URL"],
        embedding_model_id=os.environ["EMBEDDING_MODEL_ID"],
        database_url=database_url,
        s3=boto3.client("s3", region_name=region),
        sqs=boto3.client("sqs", region_name=region),
        bedrock=make_bedrock_client(region),
    )


async def _process_record(body: str, deps: _Deps) -> str:
    # A fresh pool per invocation. A pool kept across invocations would hold connections
    # through the time Lambda freezes the container, and Neon may have closed them by then.
    # Connecting costs well under a second; an ingest takes tens of seconds.
    pool = await create_pool(deps.database_url)
    try:

        async def run_ingest(job: IngestFileJob) -> IngestResult:
            def load_source() -> bytes:
                return deps.s3.get_object(Bucket=job.bucket, Key=job.key)["Body"].read()

            return await ingest_version(
                pool,
                job.key,
                load_source=load_source,
                embed=functools.partial(
                    embed_batch, deps.bedrock, model_id=deps.embedding_model_id
                ),
                embedding_model=deps.embedding_model_id,
            )

        def send_to_dlq(original: str, reason: str) -> None:
            deps.sqs.send_message(
                QueueUrl=deps.dlq_url,
                MessageBody=original,
                MessageAttributes={"reason": {"DataType": "String", "StringValue": reason}},
            )

        return await process_message(
            body, raw_bucket=deps.raw_bucket, run_ingest=run_ingest, send_to_dlq=send_to_dlq
        )
    finally:
        await pool.close()


def handler(event: dict, context: Any) -> None:
    """Lambda entry point for the SQS event source mapping."""
    deps = _deps()
    for record in event["Records"]:  # batch size 1: exactly one record
        log.info(
            "message %s, receive %s",
            record["messageId"],
            record.get("attributes", {}).get("ApproximateReceiveCount"),
        )
        asyncio.run(_process_record(record["body"], deps))
