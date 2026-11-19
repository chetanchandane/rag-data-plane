"""Unit tests for verify_hash and parse (no network; PDFs are generated in memory)."""

import hashlib

import pytest

from shared.errors import PermanentError
from tests.worker.pdfgen import Page, make_pdf
from worker.pdf import parse, verify_hash

# --- verify_hash ---------------------------------------------------------------------------


def test_matching_hash_passes() -> None:
    data = b"%PDF-1.7 example"
    verify_hash(data, hashlib.sha256(data).hexdigest())
    verify_hash(data, hashlib.sha256(data).hexdigest().upper())  # case doesn't matter


def test_mismatched_hash_is_permanent() -> None:
    with pytest.raises(PermanentError) as exc_info:
        verify_hash(b"the bytes in S3", hashlib.sha256(b"what was registered").hexdigest())
    assert exc_info.value.code == "sha256_mismatch"


# --- parse: failures -----------------------------------------------------------------------


def test_bytes_that_are_not_a_pdf_are_permanent() -> None:
    with pytest.raises(PermanentError) as exc_info:
        parse(b"this is not a PDF at all")
    assert exc_info.value.code == "unparseable_pdf"


def test_truncated_pdf_is_permanent() -> None:
    data = make_pdf([Page(["Some text."])])
    with pytest.raises(PermanentError) as exc_info:
        parse(data[:200])
    assert exc_info.value.code == "unparseable_pdf"


def test_pdf_without_text_is_permanent() -> None:
    # What a scanned (image-only) PDF looks like to a text extractor.
    with pytest.raises(PermanentError) as exc_info:
        parse(make_pdf([Page()]))
    assert exc_info.value.code == "no_text"


# --- parse: content ------------------------------------------------------------------------


def test_paragraphs_come_out_in_order_with_page_numbers() -> None:
    blocks = parse(make_pdf([Page(["First.", "Second."]), Page(["Third."])]))
    assert [(b.page, b.kind, b.text) for b in blocks] == [
        (1, "text", "First."),
        (1, "text", "Second."),
        (2, "text", "Third."),
    ]


def test_running_headers_and_page_numbers_are_dropped() -> None:
    pages = [Page([f"Body of page {n}."]) for n in range(1, 5)]
    blocks = parse(make_pdf(pages, header="ACME SPEC 42", page_numbers=True))
    assert [b.text for b in blocks] == [f"Body of page {n}." for n in range(1, 5)]


def test_margin_text_that_does_not_repeat_is_kept() -> None:
    # Only one page has this line in its top margin: it is content, not a running header.
    pages = [Page(lines_at=[(40, "A note at the top.")]), Page(["Body."]), Page(["Body 2."])]
    assert "A note at the top." in [b.text for b in parse(make_pdf(pages))]


def test_rotated_text_is_dropped() -> None:
    blocks = parse(make_pdf([Page(["Body."], rotated="Available free of charge")]))
    assert [b.text for b in blocks] == ["Body."]


def test_table_of_contents_lines_are_dropped() -> None:
    page = Page(["Contents", "2.1 Tenets of Zero Trust ......................... 6", "Body."])
    assert [b.text for b in parse(make_pdf([page]))] == ["Contents", "Body."]


def test_heading_paths_come_from_bookmarks_and_their_positions() -> None:
    pages = [
        Page(lines_at=[(100, "1 Intro"), (200, "Intro text.")]),
        Page(lines_at=[(100, "More intro text."), (300, "1.1 Detail"), (400, "Detail text.")]),
    ]
    toc = [(1, "1 Intro", 1, 90), (2, "1.1 Detail", 2, 290)]
    paths = {b.text: b.section_path for b in parse(make_pdf(pages, toc=toc))}
    assert paths["Intro text."] == ("1 Intro",)
    # Same page as the 1.1 heading, but above it: still in the parent section.
    assert paths["More intro text."] == ("1 Intro",)
    assert paths["Detail text."] == ("1 Intro", "1.1 Detail")


def test_without_bookmarks_the_path_is_empty() -> None:
    assert parse(make_pdf([Page(["Body."])]))[0].section_path == ()


def test_table_comes_out_once_as_markdown() -> None:
    rows = [["Name", "Value"], ["alpha", "1"], ["beta", "2"]]
    blocks = parse(make_pdf([Page(["Before the table."], table=rows)]))
    assert [b.kind for b in blocks] == ["text", "table"]
    assert blocks[1].text == "| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 2 |"
    # The cell text is not repeated as loose paragraphs.
    assert not any("alpha" in b.text for b in blocks if b.kind == "text")
