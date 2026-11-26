"""Step 23: the queue and worker rows of SPEC's chaos table, on the deployed stack
(docs/runbooks/chaos.md has the order, the Lambda timeout commands and the results).

Everything runs as one dedicated test tenant, through the deployed API with tokens minted by
scripts/issuer.py's key, and S3 uploads to presigned URLs, exactly as a client would. The
read-only checks connect as app_ingest (the URL from SSM), inside that tenant's RLS context.

    export PYTHONPATH=services AWS_PROFILE=ragdp AWS_REGION=us-east-2
    uv run python scripts/chaos.py pdf /tmp/chaos-300.pdf --pages 210
    uv run python scripts/chaos.py ingest /tmp/chaos-300.pdf --external-id clean
    uv run python scripts/chaos.py check --doc <doc_id>
    uv run python scripts/chaos.py duplicate --doc <doc_id>
    uv run python scripts/chaos.py out-of-order /tmp/chaos-300.pdf /tmp/small.pdf --external-id ooo
    uv run python scripts/chaos.py delete --doc <doc_id> [--doc ...]

Common options (or environment): --api-url (CHAOS_API_URL), --tenant (CHAOS_TENANT),
--bucket (CHAOS_BUCKET), --queue-url (CHAOS_QUEUE_URL). JWT_ISSUER comes from .env.
Costs: Titan embeddings for what's ingested (a 300-chunk PDF is under $0.01).
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pymupdf
from deployed_api import Api, upload, watch
from dotenv import load_dotenv

from shared.db import create_pool, rls_transaction
from shared.jobs import IngestFileJob

DATABASE_URL_INGEST_PARAM = "/ragdp/prod/database_url_ingest"
WORKER_LOG_GROUP = "/aws/lambda/ragdp-worker"
SCOPES = ("documents:read", "documents:write")

# Words for the synthetic PDF. Seeded, so the same --pages gives the same bytes every time,
# and every paragraph carries its own numbers, so no two chunks have the same text.
_WORDS = (
    "trial sponsor monitoring record data site subject consent protocol review audit "
    "deviation report safety investigator quality system risk sample visit"
).split()


# --- synthetic PDF ------------------------------------------------------------------------


def make_pdf(pages: int) -> bytes:
    """Letter pages of plain body text: about 1.4 chunks a page with the current chunker.
    Parsing is about 15x slower than embedding (PyMuPDF's table finder reads every
    character), so a PDF can't be made embedding-heavy; the runbook times the kill instead."""
    rng = random.Random(23)
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        paragraphs = []
        for i in range(7):
            sentences = [
                " ".join(rng.choice(_WORDS) for _ in range(rng.randint(8, 16))).capitalize()
                + f" ({number}.{i}-{rng.randint(100, 999)})."
                for _ in range(6)
            ]
            paragraphs.append(" ".join(sentences))
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 552, 760), "\n\n".join(paragraphs), fontsize=9)
    return doc.tobytes(no_new_id=True)  # no random trailer ID: same --pages, same bytes


# --- worker logs -------------------------------------------------------------------------


def worker_log_line(job_id: str, since_ms: int, limit_s: float) -> str | None:
    """The worker's outcome line for job_id ("job <id>: <outcome>, N chunks"), or None if it
    doesn't appear within limit_s."""
    logs = boto3.client("logs")
    deadline = time.time() + limit_s
    while time.time() < deadline:
        events = logs.filter_log_events(
            logGroupName=WORKER_LOG_GROUP, startTime=since_ms, filterPattern=f'"job {job_id}:"'
        )["events"]
        if events:
            return events[0]["message"].strip()
        time.sleep(5)
    return None


# --- database checks (read-only, app_ingest) ------------------------------------------------


async def check(tenant: uuid.UUID, doc_ids: list[str]) -> list[dict[str, Any]]:
    """Per version: status, attempts, lease, chunk rows, distinct chunk indexes and a hash of
    the chunk texts in order (equal hashes: the same chunks as another run)."""
    url = boto3.client("ssm").get_parameter(Name=DATABASE_URL_INGEST_PARAM, WithDecryption=True)
    pool = await create_pool(url["Parameter"]["Value"], max_size=1)
    out = []
    try:
        async with pool.acquire() as conn, rls_transaction(conn, tenant, None):
            for doc_id in doc_ids:
                rows = await conn.fetch(
                    """
                    SELECT v.version, v.status, v.attempts, v.last_error, v.chunk_count,
                           v.lease_until > now() AS lease_live, d.active_version,
                           d.deleted_at IS NOT NULL AS deleted,
                           (SELECT count(*) FROM chunks c
                             WHERE c.doc_id = v.doc_id AND c.version = v.version) AS rows,
                           (SELECT count(DISTINCT chunk_index) FROM chunks c
                             WHERE c.doc_id = v.doc_id AND c.version = v.version) AS distinct,
                           (SELECT md5(string_agg(content, '|' ORDER BY chunk_index)) FROM chunks c
                             WHERE c.doc_id = v.doc_id AND c.version = v.version) AS content_md5
                    FROM document_versions v JOIN documents d ON d.id = v.doc_id
                    WHERE v.doc_id = $1 ORDER BY v.version
                    """,
                    uuid.UUID(doc_id),
                )
                out.extend({"doc_id": doc_id} | dict(r) for r in rows)
    finally:
        await pool.close()
    return out


# --- commands -------------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace, api: Api) -> dict[str, Any]:
    data = Path(args.pdf).read_bytes()
    reg = api.register(data, args.external_id, Path(args.pdf).name)
    uploaded_at = upload(reg, data)
    print(f"doc {reg['doc_id']} v{reg['version']} uploaded; watching", file=sys.stderr)
    result = watch(api, reg["doc_id"], reg["version"], uploaded_at, args.limit)
    return {"doc_id": reg["doc_id"], "uploaded_at": uploaded_at} | result


def cmd_duplicate(args: argparse.Namespace) -> dict[str, Any]:
    """Send the worker the same S3 job again, as if SQS had delivered it twice. Waits for the
    worker's log line for that job id, so the check after it can't run before the worker
    has seen the message ("unchanged" would then prove nothing)."""
    tenant, doc = args.tenant, args.doc
    key = f"raw/{tenant}/{doc}/v{args.version}/source.pdf"
    head = boto3.client("s3").head_object(Bucket=args.bucket, Key=key)
    job = IngestFileJob(
        schema_version=1,
        job_id=f"chaos-duplicate-{uuid.uuid4().hex[:8]}",
        job_type="ingest",
        source="s3_event",
        bucket=args.bucket,
        key=key,
        size_bytes=head["ContentLength"],
        etag=head["ETag"].strip('"'),
        emitted_at=datetime.now(UTC),
    )
    sent_ms = int(time.time() * 1000)
    boto3.client("sqs").send_message(QueueUrl=args.queue_url, MessageBody=job.model_dump_json())
    print(f"sent {job.job_id}; waiting up to {args.wait}s for the worker", file=sys.stderr)
    line = worker_log_line(job.job_id, sent_ms, args.wait)
    if line is None:
        sys.exit(f"chaos.py: the worker never logged {job.job_id} within {args.wait}s")
    return {"job_id": job.job_id, "worker_log": line, "after": asyncio.run(check(tenant, [doc]))}


def cmd_out_of_order(args: argparse.Namespace, api: Api) -> dict[str, Any]:
    """v1 (large, slow) and v2 (small, fast) of one document, uploaded back to back. Worker
    concurrency is 2, so both run at once and v2 is indexed while v1 is still parsing."""
    big, small = Path(args.v1_pdf).read_bytes(), Path(args.v2_pdf).read_bytes()
    v1 = api.register(big, args.external_id, Path(args.v1_pdf).name)
    v2 = api.register(small, args.external_id, Path(args.v2_pdf).name)
    assert v1["doc_id"] == v2["doc_id"] and (v1["version"], v2["version"]) == (1, 2), (v1, v2)
    t1 = upload(v1, big)
    time.sleep(args.gap)
    t2 = upload(v2, small)
    second = watch(api, v2["doc_id"], 2, t2, args.limit)
    first = watch(api, v1["doc_id"], 1, t1, args.limit)
    return {
        "doc_id": v1["doc_id"],
        "v1": first,
        "v2": second,
        "active_version": api.document(v1["doc_id"])["active_version"],
    }


def cmd_check(args: argparse.Namespace) -> list[dict[str, Any]]:
    return asyncio.run(check(args.tenant, args.doc))


def cmd_delete(args: argparse.Namespace, api: Api) -> dict[str, int]:
    return {doc: api.delete(doc) for doc in args.doc}


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-url", default=os.environ.get("CHAOS_API_URL"))
    parser.add_argument("--tenant", type=uuid.UUID, default=os.environ.get("CHAOS_TENANT"))
    parser.add_argument("--bucket", default=os.environ.get("CHAOS_BUCKET"))
    parser.add_argument("--queue-url", default=os.environ.get("CHAOS_QUEUE_URL"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pdf", help="write the synthetic PDF")
    p.add_argument("out")
    p.add_argument("--pages", type=int, required=True)

    p = sub.add_parser("ingest", help="register, upload, watch to a terminal status")
    p.add_argument("pdf")
    p.add_argument("--external-id", required=True)
    p.add_argument("--limit", type=float, default=400, help="seconds to watch")

    p = sub.add_parser("check", help="read-only DB state of documents")
    p.add_argument("--doc", action="append", required=True)

    p = sub.add_parser("duplicate", help="re-send a version's S3 job to the queue")
    p.add_argument("--doc", required=True)
    p.add_argument("--version", type=int, default=1)
    p.add_argument("--wait", type=float, default=120, help="seconds to wait for the worker")

    p = sub.add_parser("out-of-order", help="v2 finishes before v1")
    p.add_argument("v1_pdf")
    p.add_argument("v2_pdf")
    p.add_argument("--external-id", required=True)
    p.add_argument("--gap", type=float, default=3, help="seconds between the uploads")
    p.add_argument("--limit", type=float, default=400)

    p = sub.add_parser("delete", help="DELETE documents through the API (cleanup)")
    p.add_argument("--doc", action="append", required=True)

    args = parser.parse_args()
    if args.command == "pdf":
        Path(args.out).write_bytes(make_pdf(args.pages))
        print(f"wrote {args.out}")
        return
    if args.tenant is None:
        sys.exit("chaos.py: --tenant (or CHAOS_TENANT) is required")
    result: Any
    if args.command == "check":
        result = cmd_check(args)
    elif args.command == "duplicate":
        result = cmd_duplicate(args)
    else:
        # Only these talk to the API, so only they mint a token (from the SSM signing key).
        if not args.api_url:
            sys.exit("chaos.py: --api-url (or CHAOS_API_URL) is required")
        api = Api(args.api_url, args.tenant, subject="chaos", scopes=SCOPES)
        commands = {"ingest": cmd_ingest, "out-of-order": cmd_out_of_order, "delete": cmd_delete}
        result = commands[args.command](args, api)
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    main()
