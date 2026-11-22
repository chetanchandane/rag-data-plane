"""The eval corpus (step 16; DECISIONS 2026-11-22, "Step 16"): fetch it, render it, load it.

    uv run python scripts/eval_corpus.py fetch     # download tenant A's FDA PDFs, check hashes
    uv run python scripts/eval_corpus.py render    # write the synthetic documents as PDFs
    PYTHONPATH=services uv run python scripts/eval_corpus.py load   # ingest everything

Tenant A is the public FDA/ICH guidance in eval/golden/corpus.json. Tenant B's fictional
SOPs, the six canary documents and the prompt-injection document are text files listed in
eval/golden/synthetic.json. Everything goes through the worker's own parse, chunk, embed and
activate code (worker.pipeline.ingest_version) with real Titan embeddings, so the eval runs
on exactly what production would index.

`load` writes only to the Neon `eval` branch: EVAL_DATABASE_URL_INGEST must point at the
endpoint named by NEON_EVAL_ENDPOINT_ID, and never at the dev endpoint. It's safe to rerun:
ingestion is idempotent, so loaded versions are skipped. Downloads land in eval/corpus/
(gitignored).
"""

import argparse
import asyncio
import functools
import hashlib
import json
import os
import sys
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "eval" / "golden"
CORPUS_DIR = ROOT / "eval" / "corpus"  # gitignored
# Document ids are derived from tenant and external id, so a rerun finds the same rows.
DOC_NAMESPACE = uuid.UUID("5d0f6a7e-1c2b-4f3a-9e8d-7c6b5a4f3e2d")
USER_AGENT = "rag-data-plane eval corpus fetch"

# Layout for rendered documents (US letter, 1-inch margins). Built-in Helvetica: unlike
# PyMuPDF's Story layout, it doesn't turn "fi" into a ligature character, which would break
# full-text search on those words.
PAGE = pymupdf.paper_rect("letter")
MARGIN = 72
BODY_SIZE, H1_SIZE, H2_SIZE = 11, 16, 13
LEADING = 1.4


@dataclass(frozen=True)
class EvalDocument:
    tenant_id: uuid.UUID
    external_id: str
    title: str
    min_role_level: int
    kind: str  # fda, sop, canary or injection

    @property
    def doc_id(self) -> uuid.UUID:
        return uuid.uuid5(DOC_NAMESPACE, f"{self.tenant_id}/{self.external_id}")


def tenants() -> dict[str, uuid.UUID]:
    data = json.loads((GOLDEN / "personas.json").read_text())
    return {key: uuid.UUID(t["tenant_id"]) for key, t in data["tenants"].items()}


def corpus_manifest() -> list[dict]:
    return json.loads((GOLDEN / "corpus.json").read_text())["documents"]


def synthetic_manifest() -> list[dict]:
    return json.loads((GOLDEN / "synthetic.json").read_text())["documents"]


def all_documents() -> list[tuple[EvalDocument, Path | None, dict]]:
    """(document, PDF path for tenant A's downloads or None, manifest entry)."""
    ids = tenants()
    docs = []
    for entry in corpus_manifest():
        doc = EvalDocument(
            ids["A"], entry["external_id"], entry["title"], entry["min_role_level"], "fda"
        )
        docs.append((doc, CORPUS_DIR / entry["filename"], entry))
    for entry in synthetic_manifest():
        doc = EvalDocument(
            ids[entry["tenant"]],
            entry["external_id"],
            entry["title"],
            entry["min_role_level"],
            entry["kind"],
        )
        docs.append((doc, None, entry))
    return docs


# --- fetch --------------------------------------------------------------------------------


def fetch(entries: list[dict], directory: Path = CORPUS_DIR, opener=urllib.request.urlopen) -> None:
    """Download each PDF unless a file with the right hash is already there. A mismatch is
    an error: FDA replaced the document, and the manifest (and golden set) need a review."""
    directory.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        path = directory / entry["filename"]
        if path.exists() and _sha256(path.read_bytes()) == entry["sha256"]:
            print(f"ok       {entry['filename']}")
            continue
        if urlsplit(entry["url"]).scheme != "https":
            raise ValueError(f"{entry['external_id']}: only https URLs are fetched")
        request = urllib.request.Request(entry["url"], headers={"User-Agent": USER_AGENT})
        with opener(request, timeout=60) as response:
            data = response.read()
        digest = _sha256(data)
        if digest != entry["sha256"]:
            raise RuntimeError(
                f"{entry['external_id']}: SHA-256 {digest} doesn't match the manifest"
                f" ({entry['sha256']}). The source changed; review it before updating corpus.json."
            )
        path.write_bytes(data)
        print(f"fetched  {entry['filename']} ({len(data) // 1024} KB)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- render -------------------------------------------------------------------------------


def render_pdf(markdown: str) -> bytes:
    """A small Markdown subset (# title, ## headings, paragraphs) as a PDF. Headings become
    bookmarks, so the worker records section paths. The output depends only on the text:
    the same text gives the same bytes and so the same SHA-256 (no dates, no random file id)."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    y = MARGIN
    width = PAGE.width - 2 * MARGIN
    toc: list[list] = []

    for block in [b.strip() for b in markdown.strip().split("\n\n") if b.strip()]:
        if block.startswith("## "):
            text, size, font, level = block[3:], H2_SIZE, "hebo", 2
        elif block.startswith("# "):
            text, size, font, level = block[2:], H1_SIZE, "hebo", 1
        else:
            text, size, font, level = " ".join(block.split()), BODY_SIZE, "helv", 0
        lines = _wrap(text, font, size, width)
        needed = len(lines) * size * LEADING
        if y + min(needed, 3 * size * LEADING) > PAGE.height - MARGIN:
            page = doc.new_page(width=PAGE.width, height=PAGE.height)
            y = MARGIN
        if level:
            toc.append([level, text, page.number + 1, y])
        for line in lines:
            if y + size * LEADING > PAGE.height - MARGIN:
                page = doc.new_page(width=PAGE.width, height=PAGE.height)
                y = MARGIN
            y += size * LEADING
            page.insert_text((MARGIN, y), line, fontname=font, fontsize=size)
        y += size * 0.8  # space after each block

    doc.set_toc(
        [
            [lvl, title, number, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(0, top)}]
            for lvl, title, number, top in toc
        ]
    )
    doc.set_metadata({})
    return doc.tobytes(garbage=3, deflate=True, no_new_id=True)


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and pymupdf.get_text_length(candidate, fontname=font, fontsize=size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + ([current] if current else [])


def render_all(directory: Path = CORPUS_DIR) -> dict[str, bytes]:
    """Render every synthetic document into directory; returns external_id -> PDF bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for entry in synthetic_manifest():
        pdf = render_pdf((GOLDEN / entry["file"]).read_text())
        (directory / f"{entry['external_id']}.pdf").write_bytes(pdf)
        rendered[entry["external_id"]] = pdf
    return rendered


# --- load ---------------------------------------------------------------------------------


def check_eval_endpoint(url: str, eval_endpoint: str, dev_endpoint: str | None) -> None:
    """Refuse any database but the eval branch: a mistake here would write 450 pages of eval
    data into dev or production."""
    host = urlsplit(url).hostname or ""
    if not eval_endpoint or not host.startswith(f"{eval_endpoint}"):
        raise SystemExit(f"refusing: the URL's host isn't the eval endpoint {eval_endpoint!r}")
    if dev_endpoint and host.startswith(dev_endpoint):
        raise SystemExit("refusing: that is the dev branch's endpoint")


async def load() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    from shared.db import create_pool
    from shared.embedding import embed_batch, make_bedrock_client
    from worker.local import register, summary
    from worker.pipeline import ingest_version

    url = os.environ.get("EVAL_DATABASE_URL_INGEST", "")
    check_eval_endpoint(
        url, os.environ.get("NEON_EVAL_ENDPOINT_ID", ""), os.environ.get("NEON_DEV_ENDPOINT_ID")
    )
    model_id = os.environ["EMBEDDING_MODEL_ID"]
    embed = functools.partial(
        embed_batch, make_bedrock_client(os.environ["AWS_REGION"]), model_id=model_id
    )
    rendered = render_all()

    failures = 0
    pool = await create_pool(url)
    try:
        for doc, path, _entry in all_documents():
            data = path.read_bytes() if path is not None else rendered[doc.external_id]
            s3_key = f"raw/{doc.tenant_id}/{doc.doc_id}/v1/source.pdf"
            async with pool.acquire() as conn:
                await register(
                    conn,
                    doc.tenant_id,
                    doc.doc_id,
                    1,
                    sha256=_sha256(data),
                    s3_key=s3_key,
                    title=doc.title,
                    min_role_level=doc.min_role_level,
                    external_id=doc.external_id,
                )
            result = await ingest_version(
                pool,
                s3_key,
                load_source=lambda data=data: data,
                embed=embed,
                embedding_model=model_id,
            )
            async with pool.acquire() as conn:
                state = await summary(conn, doc.tenant_id, doc.doc_id, 1)
            ok = state.get("status") == "indexed"
            failures += not ok
            status, chunks = state.get("status"), state.get("chunks_in_db")
            print(f"{result.outcome:10} {status!s:8} {chunks!s:>3} chunks  {doc.external_id}")
    finally:
        await pool.close()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["fetch", "render", "load"])
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(corpus_manifest())
    elif args.command == "render":
        for external_id, pdf in render_all().items():
            print(f"rendered {external_id}.pdf ({len(pdf)} bytes, sha256 {_sha256(pdf)[:12]})")
    else:
        return asyncio.run(load())
    return 0


if __name__ == "__main__":
    sys.exit(main())
