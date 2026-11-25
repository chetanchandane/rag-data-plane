"""Build small PDFs in memory for tests, so no test depends on a downloaded file."""

from dataclasses import dataclass, field

import pymupdf

PAGE_HEIGHT = 842  # PyMuPDF's default page (A4): 595 x 842 points
BODY_SIZE = 11  # insert_text's default font size
FOOTNOTE_SIZE = 9
MARKER_SIZE = 6
FOOTNOTES_TOP = 640  # in the bottom half, above the bottom margin


@dataclass
class Page:
    paragraphs: list[str] = field(default_factory=list)
    # (y, text) lines placed at exact heights, for heading-position tests.
    lines_at: list[tuple[float, str]] = field(default_factory=list)
    rotated: str | None = None
    table: list[list[str]] | None = None
    # Body lines with a raised footnote marker: (text before, marker, text after).
    marked: list[tuple[str, str, str]] = field(default_factory=list)
    # Footnotes at the bottom of the page: (number, text), set like real ones (a 6 pt
    # number, then 9 pt text). A footnote continued from the previous page has no number:
    # (None, text).
    footnotes: list[tuple[str | None, str]] = field(default_factory=list)
    footnotes_top: float = FOOTNOTES_TOP
    # (y, text) lines set at the footnote size but without a number.
    small_lines_at: list[tuple[float, str]] = field(default_factory=list)


def make_pdf(
    pages: list[Page],
    *,
    header: str | None = None,
    page_numbers: bool = False,
    toc: list[tuple[int, str, int, float]] | None = None,  # (level, title, page, y)
) -> bytes:
    doc = pymupdf.open()
    for number, spec in enumerate(pages, start=1):
        page = doc.new_page()
        if header:
            page.insert_text((72, 40), header)  # inside the top margin
        if page_numbers:
            page.insert_text((290, PAGE_HEIGHT - 30), str(number))  # inside the bottom margin
        y = 150.0
        for paragraph in spec.paragraphs:
            page.insert_text((72, y), paragraph)
            y += 40
        for line_y, text in spec.lines_at:
            page.insert_text((72, line_y), text)
        if spec.rotated:
            page.insert_text((500, 600), spec.rotated, rotate=90)
        if spec.table:
            _draw_table(page, spec.table, top=y + 20)
        for before, marker, after in spec.marked:
            width = pymupdf.get_text_length(before, fontsize=BODY_SIZE)
            page.insert_text((72, y), before)
            # Raised and smaller: PyMuPDF flags it as superscript.
            page.insert_text((72 + width, y - 4), marker, fontsize=MARKER_SIZE + 1)
            # Real PDFs carry the space after a marker in the next span ("tasks.", "61",
            # " Relevant").
            page.insert_text((72 + width + 9, y), f" {after}")
            y += 40
        note_y = spec.footnotes_top
        for number, text in spec.footnotes:
            x = 72.0
            if number is not None:
                page.insert_text((x, note_y - 3), number, fontsize=MARKER_SIZE)
                x += 8
                text = f" {text}"
            page.insert_text((x, note_y), text, fontsize=FOOTNOTE_SIZE)
            note_y += 20
        for line_y, text in spec.small_lines_at:
            page.insert_text((72, line_y), text, fontsize=FOOTNOTE_SIZE)
    if toc:
        doc.set_toc(
            [
                [level, title, page, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(72, y)}]
                for level, title, page, y in toc
            ]
        )
    return doc.tobytes()


def _draw_table(page: pymupdf.Page, rows: list[list[str]], *, top: float) -> None:
    """A ruled grid with text in each cell: what find_tables() detects."""
    xs = [72 + 130 * i for i in range(len(rows[0]) + 1)]
    ys = [top + 30 * i for i in range(len(rows) + 1)]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            page.insert_text((xs[c] + 5, ys[r] + 20), text)
