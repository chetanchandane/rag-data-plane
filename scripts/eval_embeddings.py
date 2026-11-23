"""Question embeddings for the leak matrix (step 17; DECISIONS 2026-11-22, "Step 17").

    PYTHONPATH=services uv run python scripts/eval_embeddings.py   # Titan, 190 calls, < $0.001

CI has no Bedrock access, so every golden, unanswerable and extraction question is embedded
once, with the API's own model, and committed in eval/golden/question_embeddings.json.

Each vector is stored as base64 of little-endian float32. pgvector stores float32 too, so
the database sees exactly the vector a live request would send. Next to it is the SHA-256 of
the question text: if a question is edited, load() refuses the file (and a unit test fails)
until this script is rerun.
"""

import base64
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "eval" / "golden"
EMBEDDINGS = GOLDEN / "question_embeddings.json"
QUESTION_FILES = ("questions.jsonl", "unanswerable.jsonl", "extraction.jsonl")
MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024


def all_questions() -> list[dict]:
    """Every question the leak matrix asks: [{"id", "question"}], ids unique across files."""
    questions = []
    for name in QUESTION_FILES:
        for line in (GOLDEN / name).read_text().splitlines():
            row = json.loads(line)
            questions.append({"id": row["id"], "question": row["question"]})
    ids = [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("question ids repeat across the golden files")
    return questions


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def encode(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()


def decode(data: str) -> list[float]:
    raw = base64.b64decode(data)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def load(path: Path = EMBEDDINGS, model_id: str = MODEL_ID) -> dict[str, list[float]]:
    """question id -> vector, for exactly the current questions. Raises ValueError if the
    file is for another model, misses a question, or a question's text has changed."""
    data = json.loads(path.read_text())
    if data["model_id"] != model_id or data["dimensions"] != DIMENSIONS:
        raise ValueError(f"{path.name} is for {data['model_id']}, not {model_id}")
    stored = data["embeddings"]
    vectors, problems = {}, []
    for q in all_questions():
        entry = stored.get(q["id"])
        if entry is None:
            problems.append(f"{q['id']}: no embedding")
        elif entry["text_sha256"] != text_sha256(q["question"]):
            problems.append(f"{q['id']}: the question changed since it was embedded")
        else:
            vector = decode(entry["vector"])
            if len(vector) != DIMENSIONS:
                problems.append(f"{q['id']}: {len(vector)} dimensions")
            vectors[q["id"]] = vector
    if problems:
        raise ValueError(
            f"{path.name} is stale; rerun scripts/eval_embeddings.py: " + "; ".join(problems)
        )
    return vectors


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from shared.embedding import embed_batch, make_bedrock_client

    model_id = os.environ.get("EMBEDDING_MODEL_ID", MODEL_ID)
    if model_id != MODEL_ID:
        raise SystemExit(f"EMBEDDING_MODEL_ID is {model_id}; this fixture is for {MODEL_ID}")
    questions = all_questions()
    client = make_bedrock_client(os.environ["AWS_REGION"])
    vectors = embed_batch(client, [q["question"] for q in questions], model_id=model_id)
    data = {
        "model_id": model_id,
        "dimensions": DIMENSIONS,
        "encoding": "base64 of little-endian float32",
        "embeddings": {
            q["id"]: {"text_sha256": text_sha256(q["question"]), "vector": encode(v)}
            for q, v in zip(questions, vectors, strict=True)
        },
    }
    EMBEDDINGS.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {len(vectors)} embeddings to {EMBEDDINGS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
