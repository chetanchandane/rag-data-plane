"""Unit tests for verify_hash and parse (no network; PDFs are generated in memory)."""

import hashlib

import pytest

from shared.errors import PermanentError
from tests.worker.pdfgen import Page, make_pdf
from worker.chunking import chunk
from worker.pdf import FOOTNOTES_PATH, parse, verify_hash

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


# --- parse: footnotes (DECISIONS 2026-11-24, "Step 19") -------------------------------------

# Enough body text that the body font size is the most common one, as in a real document.
BODY = "Body text that makes the body font size the most common one in this document. " * 3


def test_footnotes_between_the_halves_of_a_sentence_move_to_the_end() -> None:
    # The sentence runs onto page 2; page 1's footnotes sit between its two halves.
    pages = [
        Page(
            [BODY, "Current training materials should be available to clinical trial"],
            footnotes=[("60", "See page 6 of the 2003 part 11 guidance."), ("61", "Ibid.")],
        ),
        Page(["personnel and participants during the clinical investigation."]),
    ]
    blocks = parse(make_pdf(pages))

    assert [(b.page, b.text, b.section_path) for b in blocks[-2:]] == [
        (1, "60 See page 6 of the 2003 part 11 guidance.", FOOTNOTES_PATH),
        (1, "61 Ibid.", FOOTNOTES_PATH),
    ]
    chunks = chunk(blocks)
    body = " ".join(chunks[0].content.split())
    # The quote a model would give now matches the chunk (whitespace is normalized).
    assert "available to clinical trial personnel and participants" in body
    assert "See page 6" not in body
    # Footnotes are under their own top-level path, so they get their own chunk.
    assert chunks[-1].section_path == "Footnotes"
    assert chunks[-1].content.startswith("60 See page 6")


def test_footnotes_keep_their_pages_and_come_after_all_body_text() -> None:
    pages = [
        Page([BODY], footnotes=[("1", "First note.")]),
        Page([BODY], footnotes=[("2", "Second note.")]),
    ]
    blocks = parse(make_pdf(pages))
    assert [(b.page, b.section_path == FOOTNOTES_PATH) for b in blocks] == [
        (1, False),
        (2, False),
        (1, True),
        (2, True),
    ]


def test_a_footnote_continued_from_the_previous_page_is_a_footnote() -> None:
    # No number of its own, but set small and directly above the page's numbered footnote.
    pages = [
        Page([BODY], footnotes=[("1", "A long note that continues")]),
        Page([BODY], footnotes=[(None, "on the next page."), ("2", "Second note.")]),
    ]
    notes = [b.text for b in parse(make_pdf(pages)) if b.section_path == FOOTNOTES_PATH]
    assert notes == ["1 A long note that continues", "on the next page.", "2 Second note."]


def test_footnote_markers_in_body_text_are_removed() -> None:
    page = Page([BODY], marked=[("the same as for original records.", "33", "Q4. Is FDA")])
    texts = [b.text for b in parse(make_pdf([page]))]
    assert "the same as for original records. Q4. Is FDA" in texts


def test_a_marker_right_after_a_number_is_removed_too() -> None:
    # Kept, it would turn "312.62" into "312.625" (code review, Step 19).
    page = Page([BODY], marked=[("under 21 CFR 312.62", "5", "records must be kept.")])
    texts = [b.text for b in parse(make_pdf([page]))]
    assert "under 21 CFR 312.62 records must be kept." in texts


def test_small_text_without_a_numbered_footnote_stays_in_place() -> None:
    # A caption in small type at the bottom of the page is content, not a footnote.
    page = Page([BODY], small_lines_at=[(700, "Figure 1. Data flow in small type.")])
    blocks = parse(make_pdf([page]))
    assert blocks[-1].text == "Figure 1. Data flow in small type."
    assert blocks[-1].section_path == ()


def test_a_numbered_small_note_in_the_top_half_is_not_a_footnote() -> None:
    page = Page([BODY], footnotes=[("1", "A note-like line high on the page.")], footnotes_top=200)
    blocks = parse(make_pdf([page]))
    assert not any(b.section_path == FOOTNOTES_PATH for b in blocks)


def test_a_paragraph_across_a_page_break_is_one_block() -> None:
    # Otherwise the chunker could close a chunk between its two halves.
    pages = [
        Page([BODY, "verified by a dated signature or by generation through a validated"]),
        Page(["process) to have the same information as the original."]),
    ]
    blocks = parse(make_pdf(pages))
    assert blocks[-1].text == (
        "verified by a dated signature or by generation through a validated"
        " process) to have the same information as the original."
    )
    # It starts on page 1 and ends on page 2, and its chunk covers both pages.
    assert (blocks[-1].page, blocks[-1].last_page) == (1, 2)
    assert (chunk(blocks)[-1].page_start, chunk(blocks)[-1].page_end) == (1, 2)


def test_a_paragraph_over_three_pages_is_one_block() -> None:
    pages = [Page([BODY, "one paragraph that"]), Page(["runs over"]), Page(["three pages."])]
    assert parse(make_pdf(pages))[-1].text == "one paragraph that runs over three pages."


def test_a_word_hyphenated_across_a_page_break_is_joined_without_a_space() -> None:
    pages = [Page([BODY, "a security-"]), Page(["centric design."])]
    assert parse(make_pdf(pages))[-1].text == "a security-centric design."


@pytest.mark.parametrize(
    ("end_of_page", "next_page"),
    [
        ("This sentence ends here.", "and this one starts in lowercase."),  # sentence ended
        ("A list introduced by:", "first item"),
        ("A heading without a full stop", "Next Section Starts Here"),  # next is capitalized
        ("Data management plans", "ii. change control procedures"),  # a lowercase list item
        ("The sponsor should document", "b) the system requirements"),
    ],
)
def test_text_on_the_next_page_is_not_joined_unless_it_continues(
    end_of_page: str, next_page: str
) -> None:
    blocks = parse(make_pdf([Page([BODY, end_of_page]), Page([next_page])]))
    assert [b.text for b in blocks[-2:]] == [end_of_page, next_page]


def test_a_page_number_just_above_the_old_12_percent_margin_is_dropped() -> None:
    # y 730 of 842 is outside the bottom 12% (from 741) but inside the bottom 15% (from 716).
    pages = [Page([f"Body of page {n}."], lines_at=[(730, str(n))]) for n in range(1, 5)]
    assert [b.text for b in parse(make_pdf(pages))] == [f"Body of page {n}." for n in range(1, 5)]


def test_text_under_a_new_heading_is_not_joined_to_the_previous_page() -> None:
    # Page 2 starts a section (its bookmark is at the top), so its first block isn't the
    # rest of page 1's paragraph even though page 1 stops mid-sentence.
    pages = [
        Page(lines_at=[(100, "1 Intro"), (200, BODY), (240, "a paragraph that stops here")]),
        Page(lines_at=[(100, "continued text in a new section.")]),
    ]
    toc = [(1, "1 Intro", 1, 90), (1, "2 Next", 2, 90)]
    blocks = parse(make_pdf(pages, toc=toc))
    assert [b.text for b in blocks[-2:]] == [
        "a paragraph that stops here",
        "continued text in a new section.",
    ]
