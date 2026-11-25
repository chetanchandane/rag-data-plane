"""Split parsed Blocks into chunks (SPEC: Ingestion, worker step 4).

Rules, in order:
  1. A chunk is at most MAX_TOKENS. A paragraph longer than that is split at sentence ends
     (or between words, for a single huge sentence). A table longer than that is split by
     rows, repeating its header row in every piece.
  2. At a heading boundary, a chunk that already has MIN_TOKENS is closed, so chunks follow
     the document's sections. A smaller one keeps filling, so short sections don't become
     tiny chunks, but never across a top-level heading: a chunk stays inside one chapter,
     so its section_path is right for all of its text's chapter.
  3. A table is never split mid-row. It may share a chunk with the paragraphs around it,
     as a separate markdown block.

Chunking is deterministic: the same blocks always give the same chunks with the same
chunk_index. Resume depends on that (an index already in the DB means "already done").
Change CHUNKER_VERSION whenever these rules or the numbers change.
"""

import math
import re
from dataclasses import dataclass

from worker.pdf import Block

CHUNKER_VERSION = "headings-500-800-v1"
MIN_TOKENS = 500
MAX_TOKENS = 800
# Titan v2's tokenizer isn't published, so tokens are estimated: about 4 characters per
# token for English text. Only chunk sizes depend on it (see DECISIONS).
CHARS_PER_TOKEN = 4

_SEPARATOR = "\n\n"
# A sentence ends at . ! or ? followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    page_start: int
    page_end: int
    section_path: str | None  # headings joined with " > ", taken from the chunk's start


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def chunk(blocks: list[Block]) -> list[Chunk]:
    """Turn blocks (in reading order) into chunks numbered 0, 1, 2, ..."""
    pieces = [piece for block in blocks for piece in _split_block(block)]
    chunks: list[Chunk] = []
    current: list[Block] = []

    def close() -> None:
        if current:
            path = current[0].section_path
            chunks.append(
                Chunk(
                    index=len(chunks),
                    content=_SEPARATOR.join(p.text for p in current),
                    page_start=min(p.page for p in current),
                    page_end=max(p.last_page for p in current),
                    section_path=" > ".join(path) if path else None,
                )
            )
            current.clear()

    for piece in pieces:
        if current:
            size = estimate_tokens(_SEPARATOR.join(p.text for p in current))
            combined = estimate_tokens(_SEPARATOR.join([*(p.text for p in current), piece.text]))
            previous = current[-1].section_path
            new_section = piece.section_path != previous
            new_chapter = piece.section_path[:1] != previous[:1]
            if combined > MAX_TOKENS or new_chapter or (new_section and size >= MIN_TOKENS):
                close()
        current.append(piece)
    close()
    return chunks


def _split_block(block: Block) -> list[Block]:
    """Split one block into pieces of at most MAX_TOKENS (most blocks are one piece)."""
    if estimate_tokens(block.text) <= MAX_TOKENS:
        return [block]
    if block.kind == "table":
        texts = _split_table(block.text)
    else:
        texts = _pack(_sentences(block.text), " ")
    # Each piece keeps the block's page range: a piece of a paragraph that runs over a page
    # break may be on either page.
    return [
        Block(block.page, block.kind, text, block.section_path, block.page_end) for text in texts
    ]


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if estimate_tokens(sentence) <= MAX_TOKENS:
            parts.append(sentence)
        else:
            parts.extend(_pack(sentence.split(" "), " "))  # one huge "sentence": split by words
    return parts


def _pack(parts: list[str], joiner: str) -> list[str]:
    """Greedily join parts into strings of at most MAX_TOKENS."""
    out: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{joiner}{part}" if current else part
        if current and estimate_tokens(candidate) > MAX_TOKENS:
            out.append(current)
            current = part
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _split_table(markdown: str) -> list[str]:
    """Split a markdown table by rows; each piece repeats the header and separator rows."""
    lines = markdown.split("\n")
    header, rows = lines[:2], lines[2:]
    header_text = "\n".join(header)
    budget = MAX_TOKENS - estimate_tokens(header_text + "\n")
    pieces: list[str] = []
    current: list[str] = []
    for row in rows:
        if current and estimate_tokens("\n".join([*current, row])) > budget:
            pieces.append("\n".join([*header, *current]))
            current = []
        current.append(row)  # a single row over budget still goes out whole: never mid-row
    if current:
        pieces.append("\n".join([*header, *current]))
    return pieces
