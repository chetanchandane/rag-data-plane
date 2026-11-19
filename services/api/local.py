"""Serve the API on this laptop (Slice 1; Slice 2 puts it on Lambda).

    PYTHONPATH=services uv run python -m api.local                     # dev branch, from .env
    PYTHONPATH=services uv run python -m api.local \\
        --database-url-param /ragdp/prod/database_url_api --bucket <raw bucket>   # production

Listens on 127.0.0.1 only: the Slice 1 auth stub is one shared token, so the API must not be
reachable from the network. Presigned URLs are signed with your `aws login` session, so they
stop working when that session ends, even inside their 15 minutes.

The flags override .env, so a production run can't silently use the dev branch's URL.
Dev only: never used on Lambda.
"""

import argparse
import os

import uvicorn
from dotenv import load_dotenv

from api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url-param", help="SSM name of the app_api URL (production)")
    parser.add_argument("--bucket", help="raw bucket the upload URLs point at")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    load_dotenv()  # never overrides a variable that is already set
    if args.database_url_param:
        os.environ.pop("DATABASE_URL_API", None)  # Settings refuses both being set
        os.environ["DATABASE_URL_API_PARAM"] = args.database_url_param
    if args.bucket:
        os.environ["RAW_BUCKET"] = args.bucket
    uvicorn.run(create_app, factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
