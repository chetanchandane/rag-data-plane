"""Verify and parse a source PDF (SPEC: Ingestion, worker steps 2 and 3).

parse() turns a PDF into Blocks in reading order: paragraphs and tables, each with its
page number and heading path. Tables come out whole, as markdown; the chunker decides
whether one has to be split.

Heading paths come from the PDF's bookmarks (its table of contents), which carry the exact
position of each heading on its page. A PDF without bookmarks still parses; its blocks just
have an empty heading path.

Footnotes are moved to the end of the document, like endnotes, under the section path
("Footnotes",), each with its own page number (DECISIONS 2026-11-24, "Step 19"). Left in
place, a page's footnotes land between the two halves of a sentence that runs onto the next
page, so a quote of that sentence can never match the chunk. The footnote markers in body
text ("records.33") are removed for the same reason, and a paragraph that runs over a page
break comes out as one block, so the chunker can't put its two halves in different chunks.
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

# Change the suffix whenever the parsing rules change: stored chunks are reused by
# chunk_index only if the parser and the chunker are both unchanged (worker/store.py).
PARSER_VERSION = f"pymupdf-{pymupdf.__version__}+footnotes-v1"

# Lines in the top or bottom 15% of a page are candidates for running headers and footers.
# Only lines that also repeat across pages are dropped, so a wider margin doesn't touch body
# text. 12% missed page numbers set at y 696 of 792 (DECISIONS 2026-11-24, "Step 19").
_MARGIN_FRACTION = 0.15
# A margin line is a header/footer if it appears on at least half the pages (min 3 pages).
_REPEAT_FRACTION = 0.5
_MIN_PAGES_FOR_REPEATS = 3
# A heading's bookmark points a few points above or below its text; allow for that.
_HEADING_Y_TOLERANCE = 3.0
_HORIZONTAL = (1.0, 0.0)
# Dot leaders followed by a page number (roman or arabic) at the end of a line.
_TOC_LINE = re.compile(r"\.{5,}\s*([0-9]+|[ivxlc]+)$", re.IGNORECASE)

# Footnotes. A footnote marker is a number of 1 to 3 digits.
_FOOTNOTE_NUMBER = re.compile(r"\d{1,3}")
FOOTNOTES_PATH = ("Footnotes",)
# Footnotes sit in the bottom half of a page.
_FOOTNOTE_AREA_FRACTION = 0.5
# "Smaller than the body text" means at least this many points smaller, so a font size
# rounded differently in one span doesn't count.
_SIZE_TOLERANCE = 0.25
# PyMuPDF's span flag for superscript text (raised and smaller than its line).
_SUPERSCRIPT = pymupdf.TEXT_FONT_SUPERSCRIPT
# A lowercase list item ("ii. ", "b) ", "(a) "): it starts a new block, so it doesn't
# continue the previous page's paragraph even though it starts in lowercase.
_LIST_ITEM = re.compile(r"^\(?[a-z]{1,4}[.)]\s")
# Text that ends a sentence: . ! ? : ; or one of them before a closing quote or bracket.
_SENTENCE_END = re.compile(r"[.!?:;][\"\u201d\u2019')\]]*$")

# PyMuPDF prints a one-time hint about an optional add-on; keep worker logs clean.
pymupdf.no_recommend_layout()


@dataclass(frozen=True)
class Block:
    """One paragraph or one table, in reading order."""

    page: int  # 1-based
    kind: Literal["text", "table"]
    text: str  # plain text for paragraphs, markdown for tables
    section_path: tuple[str, ...]  # e.g. ("3 Logical Components", "3.1 Variations")
    # Set only for a paragraph that runs over a page break: the page it ends on.
    page_end: int | None = None

    @property
    def last_page(self) -> int:
        return self.page_end if self.page_end is not None else self.page


def verify_hash(data: bytes, expected_sha256: str) -> None:
    """Raise PermanentError unless data hashes to the sha256 stored at registration.

    Runs before parsing: the bytes in S3 must be the bytes the client registered.
    """
    actual = hashlib.sha256(data).hexdigest()
    # compare_digest: constant-time comparison, the habit for anything hash-like.
    if not hmac.compare_digest(actual, expected_sha256.strip().lower()):
        raise PermanentError("sha256_mismatch", "file hash differs from the registered hash")


def page_count(data: bytes) -> int:
    """The PDF's pages, blank or image-only ones too: its size for the freshness SLO (step 25).
    Only called after parse() succeeded, so the bytes open. Opening reads no page content."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc.page_count


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
    repeated, body_size = _scan(doc)
    headings = _HeadingIndex(doc.get_toc(simple=False))
    blocks: list[Block] = []
    footnotes: list[Block] = []  # emitted after all pages
    previous_page_had_text = False
    for page in doc:
        page_no = page.number + 1
        tables = page.find_tables().tables
        table_rects = [pymupdf.Rect(t.bbox) for t in tables]
        # (y position, kind, text) for this page, sorted top to bottom afterwards.
        items: list[tuple[float, Literal["text", "table"], str]] = []

        raw_blocks = page.get_text("dict")["blocks"]
        notes = _footnote_blocks(raw_blocks, page.rect, body_size)
        for index, raw in enumerate(raw_blocks):
            is_note = index in notes
            lines = [
                line
                for line in raw.get("lines", [])  # image blocks have no lines
                if _keep_line(line, page.rect, repeated, table_rects)
            ]
            # A footnote keeps its own number ("60 See page 6 ..."); body text loses its
            # markers.
            text = _join_lines(lines, drop_markers=not is_note)
            if not text:
                continue
            if is_note:
                footnotes.append(Block(page_no, "text", text, FOOTNOTES_PATH))
            else:
                items.append((lines[0]["bbox"][1], "text", text))

        for table, rect in zip(tables, table_rects, strict=True):
            markdown = _table_markdown(table.extract())
            if markdown:
                items.append((rect.y0, "table", markdown))

        page_blocks = [
            Block(page_no, kind, text, headings.path_at(page_no, y))
            for y, kind, text in sorted(items, key=lambda item: item[0])
        ]
        had_text = bool(page_blocks)
        if previous_page_had_text and page_blocks and _continues(blocks[-1], page_blocks[0]):
            blocks[-1] = _joined(blocks[-1], page_blocks.pop(0))
        blocks.extend(page_blocks)
        previous_page_had_text = had_text
    return blocks + footnotes


def _continues(last: Block, first: Block) -> bool:
    """Does the next page's first block continue the previous page's last paragraph? Only
    if both are text under the same heading, the paragraph stops mid-sentence, and the next
    text starts in lowercase and isn't a list item."""
    return (
        last.kind == first.kind == "text"
        and last.section_path == first.section_path
        and _SENTENCE_END.search(last.text) is None
        and first.text[:1].islower()
        and _LIST_ITEM.match(first.text) is None
    )


def _joined(last: Block, first: Block) -> Block:
    """One paragraph from its two halves: it starts where the first half starts and ends
    on the page where the second half ends, so its chunk's page range covers both."""
    joiner = "" if last.text.endswith("-") else " "  # a word hyphenated across the break
    return Block(
        last.page, "text", last.text + joiner + first.text, last.section_path, first.last_page
    )


def _text_spans(raw: dict) -> list[dict]:
    """A raw block's spans that have visible text, in order."""
    return [span for line in raw.get("lines", []) for span in line["spans"] if span["text"].strip()]


def _footnote_blocks(raw_blocks: list[dict], page_rect: pymupdf.Rect, body_size: float) -> set[int]:
    """Indexes of the raw blocks that are this page's footnotes.

    A numbered footnote is a block in the bottom half of the page, set smaller than the body
    text, that starts with a 1-3 digit number set smaller still ("60" at 6.5 pt, then "See
    page 6 ..." at 10 pt). From the first one down, every smaller-than-body block is a
    footnote too. So is a run of them directly above it: a footnote continued from the
    previous page, which has no number.
    """
    top_of_area = page_rect.y0 + page_rect.height * _FOOTNOTE_AREA_FRACTION
    # (index, spans) of the text blocks in the bottom half, top to bottom.
    area = sorted(
        (
            (index, spans)
            for index, raw in enumerate(raw_blocks)
            if (spans := _text_spans(raw)) and raw["bbox"][1] >= top_of_area
        ),
        key=lambda item: raw_blocks[item[0]]["bbox"][1],
    )

    def small(spans: list[dict]) -> bool:
        return all(span["size"] < body_size - _SIZE_TOLERANCE for span in spans)

    def numbered(spans: list[dict]) -> bool:
        return (
            len(spans) >= 2
            and small(spans)
            and _FOOTNOTE_NUMBER.fullmatch(spans[0]["text"].strip()) is not None
            and spans[0]["size"] < spans[1]["size"] - _SIZE_TOLERANCE
        )

    first = next((pos for pos, (_, spans) in enumerate(area) if numbered(spans)), None)
    if first is None:
        return set()
    notes = {index for index, spans in area[first:] if small(spans)}
    for index, spans in reversed(area[:first]):
        if not small(spans):
            break
        notes.add(index)
    return notes


def _line_text(line: dict, *, drop_markers: bool = False) -> str:
    """A line's text. With drop_markers, footnote markers are left out: superscript numbers
    of 1-3 digits ("records.33", "21 CFR 312.62" + "5"). A superscript exponent ("10" +
    "6") is dropped too; as plain text it would read "106", which is wrong either way, and
    the corpus has none."""
    return "".join(
        span["text"]
        for span in line["spans"]
        if not (
            drop_markers
            and span["flags"] & _SUPERSCRIPT
            and _FOOTNOTE_NUMBER.fullmatch(span["text"].strip())
        )
    ).strip()


def _in_margin(bbox: tuple[float, float, float, float], page_rect: pymupdf.Rect) -> bool:
    margin = page_rect.height * _MARGIN_FRACTION
    return bbox[3] <= page_rect.y0 + margin or bbox[1] >= page_rect.y1 - margin


def _margin_key(text: str) -> str:
    # Digits become '#', so "Page 3" and "Page 4" count as the same footer.
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def _scan(doc: pymupdf.Document) -> tuple[set[str], float]:
    """One pass over the document before parsing, since extracting a page's text is the
    slow part. Returns:
      - the margin lines that repeat across most pages: running headers, footers, page
        numbers;
      - the body font size: the size most of the text is set in, by characters.
    """
    pages_with: Counter[str] = Counter()
    sizes: Counter[float] = Counter()
    for page in doc:
        raw_blocks = page.get_text("dict")["blocks"]
        keys = {
            _margin_key(_line_text(line))
            for raw in raw_blocks
            for line in raw.get("lines", [])
            if _line_text(line) and _in_margin(line["bbox"], page.rect)
        }
        pages_with.update(keys)
        for raw in raw_blocks:
            for span in _text_spans(raw):
                sizes[round(span["size"], 1)] += len(span["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 0.0
    if doc.page_count < _MIN_PAGES_FOR_REPEATS:
        return set(), body_size
    threshold = max(_MIN_PAGES_FOR_REPEATS, doc.page_count * _REPEAT_FRACTION)
    return {key for key, count in pages_with.items() if count >= threshold}, body_size


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


def _join_lines(lines: list[dict], *, drop_markers: bool = False) -> str:
    """Join a block's wrapped lines into one paragraph."""
    text = ""
    for line in lines:
        part = _line_text(line, drop_markers=drop_markers)
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
