"""Serve the API on this laptop (step 13 puts it on Lambda).

    PYTHONPATH=services uv run python -m api.local                     # dev branch, from .env
    PYTHONPATH=services uv run python -m api.local \\
        --database-url-param /ragdp/prod/database_url_api --bucket <raw bucket> \\
        --queue-url <ingest queue url>                                      # production

Requests need a token from `scripts/issuer.py mint`. With no API Gateway in front, the API
verifies it itself against JWT_ISSUER's published keys (api/auth.py). Listens on 127.0.0.1
only: it's plain HTTP, for this laptop. Presigned URLs are signed with your `aws login`
session, so they stop working when that session ends, even inside their 15 minutes.

The flags override .env, so a production run can't silently use the dev branch's URL.
DELETE sends its purge job to INGEST_QUEUE_URL, which production's worker reads: point a run
on the dev branch at it and the worker finds no such document there (a DLQ entry and an
alarm). So test deletes against the dev branch in the DB tests, not through api.local.
Dev only: never used on Lambda.
"""

import argparse
import logging
import os
import sys

import uvicorn
from dotenv import load_dotenv

from api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url-param", help="SSM name of the app_api URL (production)")
    parser.add_argument("--bucket", help="raw bucket the upload URLs point at")
    parser.add_argument("--queue-url", help="ingest queue DELETE sends purge jobs to")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    load_dotenv()  # never overrides a variable that is already set
    if args.database_url_param:
        os.environ.pop("DATABASE_URL_API", None)  # Settings refuses both being set
        os.environ["DATABASE_URL_API_PARAM"] = args.database_url_param
    if args.bucket:
        os.environ["RAW_BUCKET"] = args.bucket
    if args.queue_url:
        os.environ["INGEST_QUEUE_URL"] = args.queue_url
    if not os.environ.get("JWT_ISSUER", "").strip():
        sys.exit("JWT_ISSUER is not set: put the issuer URL in .env (see .env.example)")
    # Shows the app's own log lines too, such as why a token got a 401 (api/auth.py).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    uvicorn.run(create_app, factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
