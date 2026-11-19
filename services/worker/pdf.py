"""Verify and parse a source PDF (SPEC: Ingestion, worker steps 2 and 3).

parse() turns a PDF into Blocks in reading order: paragraphs and tables, each with its
page number and heading path. Tables come out whole, as markdown; the chunker decides
whether one has to be split.

Heading paths come from the PDF's bookmarks (its table of contents), which carry the exact
position of each heading on its page. A PDF without bookmarks still parses; its blocks just
have an empty heading path.
"""

import hashlib
import hmac
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import pymupdf

from shared.errors import PermanentError

PARSER_VERSION = f"pymupdf-{pymupdf.__version__}"

# Lines in the top or bottom 12% of a page are candidates for running headers and footers.
_MARGIN_FRACTION = 0.12
# A margin line is a header/footer if it appears on at least half the pages (min 3 pages).
_REPEAT_FRACTION = 0.5
_MIN_PAGES_FOR_REPEATS = 3
# A heading's bookmark points a few points above or below its text; allow for that.
_HEADING_Y_TOLERANCE = 3.0
_HORIZONTAL = (1.0, 0.0)
# Dot leaders followed by a page number (roman or arabic) at the end of a line.
_TOC_LINE = re.compile(r"\.{5,}\s*([0-9]+|[ivxlc]+)$", re.IGNORECASE)

# PyMuPDF prints a one-time hint about an optional add-on; keep worker logs clean.
pymupdf.no_recommend_layout()


@dataclass(frozen=True)
class Block:
    """One paragraph or one table, in reading order."""

    page: int  # 1-based
    kind: Literal["text", "table"]
    text: str  # plain text for paragraphs, markdown for tables
    section_path: tuple[str, ...]  # e.g. ("3 Logical Components", "3.1 Variations")


def verify_hash(data: bytes, expected_sha256: str) -> None:
    """Raise PermanentError unless data hashes to the sha256 stored at registration.

    Runs before parsing: the bytes in S3 must be the bytes the client registered.
    """
    actual = hashlib.sha256(data).hexdigest()
    # compare_digest: constant-time comparison, the habit for anything hash-like.
    if not hmac.compare_digest(actual, expected_sha256.strip().lower()):
        raise PermanentError("sha256_mismatch", "file hash differs from the registered hash")


def parse(data: bytes) -> list[Block]:
    """Parse PDF bytes into Blocks. Raises PermanentError if the PDF can't be used."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several types for damaged files
        raise PermanentError("unparseable_pdf", f"cannot open PDF ({type(exc).__name__})") from exc
    with doc:
        if doc.needs_pass:
            raise PermanentError("unparseable_pdf", "PDF is password-protected")
        try:
            blocks = _parse_document(doc)
        except Exception as exc:
            raise PermanentError(
                "unparseable_pdf", f"cannot read PDF content ({type(exc).__name__})"
            ) from exc
    if not blocks:
        # Typically a scanned PDF (images only). Text extraction from images is out of scope.
        raise PermanentError("no_text", "PDF contains no extractable text")
    return blocks


def _parse_document(doc: pymupdf.Document) -> list[Block]:
    repeated = _repeated_margin_lines(doc)
    headings = _HeadingIndex(doc.get_toc(simple=False))
    blocks: list[Block] = []
    for page in doc:
        page_no = page.number + 1
        tables = page.find_tables().tables
        table_rects = [pymupdf.Rect(t.bbox) for t in tables]
        # (y position, kind, text) for this page, sorted top to bottom afterwards.
        items: list[tuple[float, Literal["text", "table"], str]] = []

        for raw in page.get_text("dict")["blocks"]:
            lines = [
                line
                for line in raw.get("lines", [])  # image blocks have no lines
                if _keep_line(line, page.rect, repeated, table_rects)
            ]
            text = _join_lines(lines)
            if text:
                items.append((lines[0]["bbox"][1], "text", text))

        for table, rect in zip(tables, table_rects, strict=True):
            markdown = _table_markdown(table.extract())
            if markdown:
                items.append((rect.y0, "table", markdown))

        for y, kind, text in sorted(items, key=lambda item: item[0]):
            blocks.append(Block(page_no, kind, text, headings.path_at(page_no, y)))
    return blocks


def _line_text(line: dict) -> str:
    return "".join(span["text"] for span in line["spans"]).strip()


def _in_margin(bbox: tuple[float, float, float, float], page_rect: pymupdf.Rect) -> bool:
    margin = page_rect.height * _MARGIN_FRACTION
    return bbox[3] <= page_rect.y0 + margin or bbox[1] >= page_rect.y1 - margin


def _margin_key(text: str) -> str:
    # Digits become '#', so "Page 3" and "Page 4" count as the same footer.
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def _repeated_margin_lines(doc: pymupdf.Document) -> set[str]:
    """Margin lines that repeat across most pages: running headers, footers, page numbers."""
    if doc.page_count < _MIN_PAGES_FOR_REPEATS:
        return set()
    pages_with: Counter[str] = Counter()
    for page in doc:
        keys = {
            _margin_key(_line_text(line))
            for raw in page.get_text("dict")["blocks"]
            for line in raw.get("lines", [])
            if _line_text(line) and _in_margin(line["bbox"], page.rect)
        }
        pages_with.update(keys)
    threshold = max(_MIN_PAGES_FOR_REPEATS, doc.page_count * _REPEAT_FRACTION)
    return {key for key, count in pages_with.items() if count >= threshold}


def _keep_line(
    line: dict, page_rect: pymupdf.Rect, repeated: set[str], table_rects: list[pymupdf.Rect]
) -> bool:
    text = _line_text(line)
    if not text:
        return False
    # Rotated text is a watermark or margin note (e.g. a vertical "available free of
    # charge" banner), not body text.
    if tuple(round(v, 3) for v in line["dir"]) != _HORIZONTAL:
        return False
    if _in_margin(line["bbox"], page_rect) and _margin_key(text) in repeated:
        return False
    # Table-of-contents entries ("2.1 Tenets ........ 6") are navigation, not content.
    if _TOC_LINE.search(text):
        return False
    # Table text is emitted once, as markdown, so skip it here to avoid duplicates.
    x0, y0, x1, y1 = line["bbox"]
    center = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
    return not any(center in rect for rect in table_rects)


def _join_lines(lines: list[dict]) -> str:
    """Join a block's wrapped lines into one paragraph."""
    text = ""
    for line in lines:
        part = _line_text(line)
        if not text:
            text = part
        elif text.endswith("-"):
            # A line ending in "-" was wrapped mid-word ("security-" / "centric"): no space.
            text += part
        else:
            text += " " + part
    return " ".join(text.split())


def _table_markdown(rows: list[list[str | None]]) -> str:
    """Render extracted table cells as a markdown table; the first row is the header.

    Built by hand rather than with PyMuPDF's to_markdown(), which copies a merged cell's
    text into every column it spans and repeats it many times over.
    """
    cleaned = [[" ".join((cell or "").split()).replace("|", "\\|") for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return ""
    # Merged cells leave columns that are empty in every row: drop them.
    keep = [i for i in range(len(cleaned[0])) if any(row[i] for row in cleaned)]
    cleaned = [[row[i] for i in keep] for row in cleaned]
    header, body = cleaned[0], cleaned[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


class _HeadingIndex:
    """Maps a position (page, y) to the heading path in force there, using the bookmarks."""

    def __init__(self, toc: list) -> None:
        entries: list[tuple[int, float, tuple[str, ...]]] = []
        stack: list[str] = []
        for entry in toc:
            level, title, page = entry[0], " ".join(str(entry[1]).split()), entry[2]
            if page < 1 or not title:
                continue  # bookmark that points nowhere
            # Bookmarks are in document order with levels 1, 2, 3...: the path to this one is
            # the stack of open headings above its level, plus its own title.
            del stack[level - 1 :]
            stack.append(title)
            point = entry[3].get("to") if len(entry) > 3 and isinstance(entry[3], dict) else None
            y = point.y if point is not None else 0.0
            entries.append((page, y, tuple(stack)))
        entries.sort(key=lambda e: (e[0], e[1]))
        self._keys = [(page, y) for page, y, _ in entries]
        self._paths = [path for _, _, path in entries]

    def path_at(self, page: int, y: float) -> tuple[str, ...]:
        # The last heading at or above this position (on this page or an earlier one).
        i = bisect_right(self._keys, (page, y + _HEADING_Y_TOLERANCE))
        return self._paths[i - 1] if i else ()
