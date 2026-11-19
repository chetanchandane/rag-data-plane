"""Run one ingest locally: a PDF on disk instead of S3, real Neon dev branch, real Bedrock.

    PYTHONPATH=services uv run python -m worker.local samples/doc.pdf \\
        --tenant <uuid> --doc <uuid> --version 1

It first registers the document and version the way the API will in step 9 (status
'uploaded', sha256 of the file), then runs the same ingest_version() the Lambda will run.
Registration is skipped if the rows exist, so rerunning the same command is safe and shows
the "already done" path. Pass --sha256 to register a different hash and see the
hash-mismatch path.

Needs in .env: DATABASE_URL_INGEST, AWS_REGION, EMBEDDING_MODEL_ID, AWS_PROFILE.
Dev only: never used on Lambda.
"""

import argparse
import asyncio
import functools
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

from shared.db import create_pool, rls_transaction
from shared.errors import PermanentError
from worker.embedding import embed_batch, make_bedrock_client
from worker.pipeline import ingest_version


async def register(
    conn,
    tenant_id: UUID,
    doc_id: UUID,
    version: int,
    *,
    sha256: str,
    s3_key: str,
    title: str,
    min_role_level: int,
) -> None:
    """Create the document and version rows if missing (stand-in for POST /v1/documents)."""
    async with rls_transaction(conn, tenant_id, None):
        await conn.execute(
            """
            INSERT INTO documents (id, tenant_id, external_id, title, min_role_level)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            doc_id,
            tenant_id,
            f"local:{doc_id}",
            title,
            min_role_level,
        )
        await conn.execute(
            """
            INSERT INTO document_versions (doc_id, version, tenant_id, sha256, s3_key, status)
            VALUES ($1, $2, $3, $4, $5, 'uploaded')
            ON CONFLICT DO NOTHING
            """,
            doc_id,
            version,
            tenant_id,
            sha256,
            s3_key,
        )


async def summary(conn, tenant_id: UUID, doc_id: UUID, version: int) -> dict:
    async with rls_transaction(conn, tenant_id, None):
        row = await conn.fetchrow(
            """
            SELECT v.status, v.attempts, v.chunk_count, v.last_error, d.active_version,
                   (SELECT count(*) FROM chunks c
                    WHERE c.doc_id = v.doc_id AND c.version = v.version) AS chunks_in_db
            FROM document_versions v JOIN documents d ON d.id = v.doc_id
            WHERE v.doc_id = $1 AND v.version = $2
            """,
            doc_id,
            version,
        )
    return dict(row) if row else {}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--doc", type=UUID, required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--min-role-level", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--sha256", help="register this hash instead of the file's")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    data = args.pdf.read_bytes()
    s3_key = f"raw/{args.tenant}/{args.doc}/v{args.version}/source.pdf"
    model_id = os.environ["EMBEDDING_MODEL_ID"]
    bedrock = make_bedrock_client(os.environ["AWS_REGION"])

    pool = await create_pool(os.environ["DATABASE_URL_INGEST"])
    try:
        async with pool.acquire() as conn:
            await register(
                conn,
                args.tenant,
                args.doc,
                args.version,
                sha256=args.sha256 or hashlib.sha256(data).hexdigest(),
                s3_key=s3_key,
                title=args.pdf.stem,
                min_role_level=args.min_role_level,
            )
        exit_code = 0
        try:
            result = await ingest_version(
                pool,
                s3_key,
                load_source=lambda: data,
                embed=functools.partial(embed_batch, bedrock, model_id=model_id),
                embedding_model=model_id,
            )
            print("result:", json.dumps(result.__dict__))
        except PermanentError as exc:
            # On Lambda this message would go to the DLQ with exc.reason as its attribute.
            print("permanent error (would go to the DLQ):", exc.reason)
            exit_code = 2
        async with pool.acquire() as conn:
            print("db:", json.dumps(await summary(conn, args.tenant, args.doc, args.version)))
        return exit_code
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
