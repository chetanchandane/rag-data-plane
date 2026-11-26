"""Step 24: the load-test tenant in production, and tokens for k6
(docs/runbooks/load-test.md has the order and the results).

The tenant gets tenant A's 12 FDA guidances (eval/golden/corpus.json) through the real ingest
path: the deployed API registers each one, the PDF goes to its presigned S3 URL, and the
deployed worker indexes it. Rerunning is safe: the external ids are the corpus ids, so a
document that's already registered is reported, not uploaded again, and one whose upload
never happened gets a fresh upload URL. A `failed` document stays failed: the same bytes
return the same version (UNIQUE (doc_id, sha256)), and a deleted external id can't be
registered again. Fix the cause (its last_error), then use a new LOADTEST_TENANT.

    export PYTHONPATH=services AWS_PROFILE=ragdp AWS_REGION=us-east-2
    uv run python scripts/eval_corpus.py fetch           # the PDFs, hash-checked
    uv run python scripts/loadtest.py ingest --api-url "$API_URL"
    export TOKEN=$(uv run python scripts/loadtest.py token)   # 15 minutes

--api-url can also come from LOAD_API_URL. JWT_ISSUER comes from .env.
Costs: Titan embeddings for 448 pages, about $0.02.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from deployed_api import TERMINAL, Api, jwt_issuer, mint_token, signing_key, upload, watch
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval" / "golden" / "corpus.json"
CORPUS_DIR = ROOT / "eval" / "corpus"

# A fixed tenant, so every load run queries the same corpus. Generated once (uuid4); nothing
# else uses it. Every document is level 1, so the tokens are level 1 too (least privilege).
LOADTEST_TENANT = uuid.UUID("c98bf2d7-f2a9-407c-9bbb-6498b96b3767")
ROLE_LEVEL = 1
INGEST_SCOPES = ("documents:read", "documents:write")
QUERY_SCOPES = ("retrieve", "query")


def corpus() -> list[tuple[dict[str, Any], bytes]]:
    """(manifest entry, PDF bytes) for each document. Refuses a missing or changed file, so
    the load tenant always holds exactly the corpus the eval measured, and a document above
    the tokens' level, which the API would refuse halfway through the run."""
    out = []
    for entry in json.loads(MANIFEST.read_text())["documents"]:
        if entry["min_role_level"] > ROLE_LEVEL:
            sys.exit(f"loadtest.py: {entry['external_id']} is above level {ROLE_LEVEL}")
        path = CORPUS_DIR / entry["filename"]
        if not path.exists():
            sys.exit(f"loadtest.py: {path} is missing: run scripts/eval_corpus.py fetch")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            sys.exit(f"loadtest.py: {path} doesn't match the manifest's sha256")
        out.append((entry, data))
    return out


def cmd_ingest(args: argparse.Namespace) -> int:
    api = Api(
        args.api_url,
        LOADTEST_TENANT,
        subject="loadtest",
        scopes=INGEST_SCOPES,
        role_level=ROLE_LEVEL,
    )
    # Upload everything first: the worker takes 2 at a time, so the queue does the pacing.
    pending = []
    results: list[dict[str, Any]] = []
    for entry, data in corpus():
        reg = api.register(data, entry["external_id"], entry["filename"], entry["min_role_level"])
        row = {"external_id": entry["external_id"], "doc_id": reg["doc_id"]}
        if reg["upload"] is not None:
            pending.append((row, reg["version"], upload(reg, data)))
            print(f"{entry['external_id']}: uploaded v{reg['version']}", file=sys.stderr)
        elif reg["status"] in TERMINAL:
            results.append(row | {"status": reg["status"], "already": True})
        else:
            # Uploaded by an earlier run and still being indexed. The upload time isn't
            # known, so seen_at_s counts from now and isn't upload-to-indexed.
            pending.append((row | {"uploaded_earlier": True}, reg["version"], time.time()))
    for row, version, uploaded_at in pending:
        result = watch(api, row["doc_id"], version, uploaded_at, args.limit)
        results.append(row | result)
        print(f"{row['external_id']}: {result['status']} {result['seen_at_s']}", file=sys.stderr)
    print(json.dumps(results, indent=1, default=str))
    not_indexed = [r["external_id"] for r in results if r["status"] != "indexed"]
    if not_indexed:
        print(
            f"loadtest.py: not indexed: {not_indexed}. A failed document stays failed on a"
            " rerun: see this script's docstring.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_token(_: argparse.Namespace) -> int:
    issuer_url = jwt_issuer()
    token = mint_token(
        signing_key(),
        issuer_url,
        LOADTEST_TENANT,
        subject="loadtest",
        scopes=QUERY_SCOPES,
        role_level=ROLE_LEVEL,
    )
    print(token)
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="register, upload and index the corpus as the tenant")
    p.add_argument("--api-url", default=os.environ.get("LOAD_API_URL"))
    p.add_argument("--limit", type=float, default=1800, help="seconds to watch each document")
    sub.add_parser("token", help="print a 15-minute retrieve/query token for the tenant")
    args = parser.parse_args()
    if args.command == "ingest":
        if not args.api_url:
            sys.exit("loadtest.py: --api-url (or LOAD_API_URL) is required")
        return cmd_ingest(args)
    return cmd_token(args)


if __name__ == "__main__":
    sys.exit(main())
