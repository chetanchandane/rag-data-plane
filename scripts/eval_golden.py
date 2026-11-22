"""Generate the golden set from tenant A's corpus (step 16; DECISIONS 2026-11-22, "Step 16").

    uv run python scripts/eval_corpus.py fetch          # the PDFs first
    PYTHONPATH=services uv run python scripts/eval_golden.py generate

Answerable questions: chunks are made by the worker's own parse and chunk code (so each
question's source is exactly a chunk the worker indexes), sampled across the documents with a
fixed seed, and Mistral Large 3 writes one question per chunk with a reference answer and a
verbatim supporting quote. A question is kept only if its quote is really in the chunk (the
API's own citation rule). Unanswerable questions: on-topic questions these documents don't
answer, for abstain precision.

Mistral is a different provider from both answering models, so no model is graded on its own
phrasing. Calls Bedrock (paid, well under $1); writes eval/golden/questions.jsonl and
unanswerable.jsonl. Rerunning replaces them.
"""

import argparse
import json
import random
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "eval" / "golden"
CORPUS_DIR = ROOT / "eval" / "corpus"

GENERATOR_MODEL = "mistral.mistral-large-3-675b-instruct"  # in-region in us-east-2
REGION = "us-east-2"
PROMPT_VERSION = "2026-11-22.2"
SEED = 20261122
ANSWERABLE_TARGET = 150
CANDIDATES = 240  # about a third yield no question (contents pages, quotes off the text)
UNANSWERABLE_TARGET = 30
MIN_CHUNK_TOKENS = 120  # shorter chunks are mostly headings and lists
MIN_QUOTE_WORDS = 4
_ELLIPSIS = re.compile(r"\.\.\.|\u2026")
PARALLEL_CALLS = 4

QUESTION_SYSTEM = """\
You write evaluation questions for a retrieval system over FDA and ICH clinical-trial \
guidance. Given one passage, write one question a clinical-trial professional might \
realistically ask that the passage answers on its own.

Rules:
- The question must make sense without seeing the passage: never say "the passage", "this \
section" or "the document"; name the topic instead.
- Ask about requirements, definitions, responsibilities, timelines or procedures, not about \
page numbers, footnotes, document history or contact details.
- The answer must use only the passage and be one to three sentences.
- quote must be copied exactly from the passage: one sentence or phrase that supports the \
answer, verbatim.
- If the passage can't support a good question (a contents page, a list of references, \
headings only), set usable to false and leave the other fields empty.
Answer by calling the submit_question tool."""

UNANSWERABLE_SYSTEM = """\
You write evaluation questions for a retrieval system. The system holds only the documents \
listed by the user: FDA and ICH guidance on clinical trials of drugs. Write questions a \
clinical-trial professional might realistically ask that none of these documents can answer.

Spread them over these categories, at most 5 per category: medical devices and diagnostics; \
non-US law (EU Clinical Trials Regulation, UK, Japan, China, India); drug manufacturing, \
quality and CMC; specific diseases, drugs or doses; pricing, reimbursement, insurance and \
contracts; veterinary, food and cosmetics; tax, employment and site finances.

Each question must need facts that are only in other documents: a specific law, product, \
number or procedure. Never ask about a general topic these documents do cover (good clinical \
practice, informed consent, IRBs, monitoring, safety reporting, statistical principles, \
estimands, control groups, study reports, multi-regional trials, electronic records and \
signatures), even if you frame it for another country or product type. Make them specific and \
realistic, not trick questions. Answer by calling the submit_questions tool."""

QUESTION_TOOL = {
    "toolSpec": {
        "name": "submit_question",
        "description": "Submit one evaluation question.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "usable": {"type": "boolean"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["usable", "question", "answer", "quote"],
            }
        },
    }
}

UNANSWERABLE_TOOL = {
    "toolSpec": {
        "name": "submit_questions",
        "description": "Submit the questions.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "topic": {"type": "string"},
                            },
                            "required": ["question", "topic"],
                        },
                    }
                },
                "required": ["questions"],
            }
        },
    }
}


@dataclass(frozen=True)
class SourceChunk:
    external_id: str
    title: str
    page_start: int | None
    page_end: int | None
    section_path: str | None
    content: str


def normalize(text: str) -> str:
    """The API's citation normalization (api/answering.py): whitespace and case only."""
    return " ".join(text.split()).casefold()


def corpus_chunks() -> list[SourceChunk]:
    """Every chunk of tenant A's PDFs, as the worker would index them."""
    from worker.chunking import chunk
    from worker.pdf import parse

    manifest = json.loads((GOLDEN / "corpus.json").read_text())["documents"]
    chunks = []
    for entry in manifest:
        path = CORPUS_DIR / entry["filename"]
        if not path.exists():
            raise SystemExit(f"{path} is missing: run scripts/eval_corpus.py fetch first")
        for c in chunk(parse(path.read_bytes())):
            chunks.append(
                SourceChunk(
                    entry["external_id"],
                    entry["title"],
                    c.page_start,
                    c.page_end,
                    c.section_path,
                    c.content,
                )
            )
    return chunks


def sample(chunks: list[SourceChunk], n: int, seed: int = SEED) -> list[SourceChunk]:
    """n chunks spread over the documents in proportion to their size (at least 2 each),
    skipping very short chunks. Deterministic for a given seed."""
    from worker.chunking import estimate_tokens

    rng = random.Random(seed)
    eligible = [c for c in chunks if estimate_tokens(c.content) >= MIN_CHUNK_TOKENS]
    by_doc: dict[str, list[SourceChunk]] = {}
    for c in eligible:
        by_doc.setdefault(c.external_id, []).append(c)
    total = len(eligible)
    picked: list[SourceChunk] = []
    for doc_chunks in by_doc.values():
        quota = min(len(doc_chunks), max(2, round(n * len(doc_chunks) / total)))
        picked.extend(rng.sample(doc_chunks, quota))
    rng.shuffle(picked)
    return picked[:n]


def tool_input(response: dict, name: str) -> dict | None:
    for block in response.get("output", {}).get("message", {}).get("content", []):
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == name and isinstance(tool_use.get("input"), dict):
            return tool_use["input"]
    return None


def question_for(client, source: SourceChunk) -> tuple[dict | None, str]:
    """One question for one chunk and "kept", or None and why it was dropped: the model
    declined, left a field empty, or quoted text that isn't in the chunk (a made-up quote
    would make the reference wrong)."""
    response = client.converse(
        modelId=GENERATOR_MODEL,
        system=[{"text": QUESTION_SYSTEM}],
        messages=[{"role": "user", "content": [{"text": f"Passage:\n\n{source.content}"}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.3},
        toolConfig={"tools": [QUESTION_TOOL], "toolChoice": {"tool": {"name": "submit_question"}}},
    )
    data = tool_input(response, "submit_question")
    if not data:
        return None, "no_tool_call"
    if data.get("usable") is not True:
        return None, "declined"
    question, answer, quote = (
        str(data.get(k, "")).strip() for k in ("question", "answer", "quote")
    )
    if not question or not answer or not quote:
        return None, "empty_field"
    quote = supporting_span(quote, source.content)
    if quote is None:
        return None, "quote_not_in_chunk"
    return {
        "question": question,
        "reference_answer": answer,
        "source": {
            "external_id": source.external_id,
            "page_start": source.page_start,
            "page_end": source.page_end,
            "section_path": source.section_path,
            "quote": quote,
        },
    }, "kept"


def supporting_span(quote: str, content: str) -> str | None:
    """A verbatim span of content that the quote supports, or None. Models sometimes join two
    sentences with "..."; that's accepted when every piece is in the chunk, and the longest
    piece is kept, so the stored quote is always one verbatim span."""
    pieces = [piece.strip(" .,;:") for piece in _ELLIPSIS.split(quote)]
    pieces = [piece for piece in pieces if piece]
    if not pieces or any(len(piece.split()) < MIN_QUOTE_WORDS for piece in pieces):
        return None
    if any(normalize(piece) not in normalize(content) for piece in pieces):
        return None
    return max(pieces, key=len)


def unanswerable_questions(client, titles: list[str], n: int) -> list[dict]:
    listing = "\n".join(f"- {t}" for t in titles)
    response = client.converse(
        modelId=GENERATOR_MODEL,
        system=[{"text": UNANSWERABLE_SYSTEM}],
        messages=[
            {
                "role": "user",
                "content": [{"text": f"The documents:\n{listing}\n\nWrite {n} questions."}],
            }
        ],
        inferenceConfig={"maxTokens": 4000, "temperature": 0.7},
        toolConfig={
            "tools": [UNANSWERABLE_TOOL],
            "toolChoice": {"tool": {"name": "submit_questions"}},
        },
    )
    data = tool_input(response, "submit_questions") or {}
    seen, out = set(), []
    for item in data.get("questions", []):
        question = str(item.get("question", "")).strip()
        if question and normalize(question) not in seen:
            seen.add(normalize(question))
            out.append({"question": question, "topic": str(item.get("topic", "")).strip()})
    return out[:n]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def generate() -> int:
    client = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(retries={"mode": "standard", "total_max_attempts": 3}, read_timeout=60),
    )
    candidates = sample(corpus_chunks(), CANDIDATES)
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        results = list(pool.map(lambda c: question_for(client, c), candidates))

    meta = {"generator": GENERATOR_MODEL, "prompt_version": PROMPT_VERSION, "seed": SEED}
    reasons = Counter(reason for _, reason in results)
    kept = [row for row, _ in results if row is not None][:ANSWERABLE_TARGET]
    answerable = [{"id": f"q{i:03d}", **row, **meta} for i, row in enumerate(kept, 1)]
    write_jsonl(GOLDEN / "questions.jsonl", answerable)

    titles = [d["title"] for d in json.loads((GOLDEN / "corpus.json").read_text())["documents"]]
    unanswerable = [
        {"id": f"u{i:03d}", **row, **meta}
        for i, row in enumerate(unanswerable_questions(client, titles, UNANSWERABLE_TARGET), 1)
    ]
    write_jsonl(GOLDEN / "unanswerable.jsonl", unanswerable)

    per_doc = Counter(q["source"]["external_id"] for q in answerable)
    print(f"candidates {len(candidates)}: {dict(reasons)}")
    print(f"answerable {len(answerable)}, unanswerable {len(unanswerable)}")
    for external_id, count in sorted(per_doc.items()):
        print(f"  {count:3}  {external_id}")
    return 0 if len(answerable) >= ANSWERABLE_TARGET * 0.9 else 1


def regenerate_unanswerable(n: int) -> int:
    """Only the unanswerable questions, as candidates for review: writes n of them to
    eval/golden/unanswerable.candidates.jsonl, to be curated into unanswerable.jsonl."""
    client = boto3.client("bedrock-runtime", region_name=REGION, config=Config(read_timeout=120))
    titles = [d["title"] for d in json.loads((GOLDEN / "corpus.json").read_text())["documents"]]
    meta = {"generator": GENERATOR_MODEL, "prompt_version": PROMPT_VERSION}
    rows = [{**row, **meta} for row in unanswerable_questions(client, titles, n)]
    write_jsonl(GOLDEN / "unanswerable.candidates.jsonl", rows)
    print(f"{len(rows)} candidates in unanswerable.candidates.jsonl")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["generate", "unanswerable"])
    parser.add_argument("--n", type=int, default=40, help="unanswerable candidates to write")
    args = parser.parse_args()
    if args.command == "unanswerable":
        return regenerate_unanswerable(args.n)
    return generate()


if __name__ == "__main__":
    sys.exit(main())
