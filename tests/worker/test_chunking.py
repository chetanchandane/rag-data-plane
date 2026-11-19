"""Unit tests for chunk() (no network)."""

from worker.chunking import MAX_TOKENS, MIN_TOKENS, chunk, estimate_tokens
from worker.pdf import Block


def words(n_tokens: int, word: str = "word") -> str:
    """Text of about n_tokens estimated tokens, ending in a full stop."""
    unit = word + " "  # 5 chars
    text = (unit * (n_tokens * 4 // len(unit))).strip()
    return text + "."


def text(page: int, body: str, *path: str) -> Block:
    return Block(page=page, kind="text", text=body, section_path=tuple(path))


def test_indexes_are_contiguous_and_chunking_is_deterministic() -> None:
    blocks = [text(p, words(300), "1 Intro") for p in range(1, 11)]
    first, second = chunk(blocks), chunk(blocks)
    assert first == second
    assert [c.index for c in first] == list(range(len(first)))


def test_no_chunk_exceeds_the_maximum() -> None:
    blocks = [text(1, words(n), "1 A") for n in (100, 700, 350, 790, 20, 600)]
    assert all(estimate_tokens(c.content) <= MAX_TOKENS for c in chunk(blocks))


def test_a_long_paragraph_is_split_at_sentence_ends() -> None:
    sentences = [f"Sentence {i} " + words(90) for i in range(20)]  # ~1,800 tokens in total
    chunks = chunk([text(1, " ".join(sentences), "1 A")])
    assert len(chunks) > 1
    assert all(estimate_tokens(c.content) <= MAX_TOKENS for c in chunks)
    assert all(c.content.endswith(".") for c in chunks)  # no sentence was cut in half


def test_one_huge_sentence_is_split_between_words() -> None:
    chunks = chunk([text(1, "word " * 2000, "1 A")])
    assert len(chunks) > 1
    assert all(estimate_tokens(c.content) <= MAX_TOKENS for c in chunks)
    assert all("wor d" not in c.content for c in chunks)


def test_a_heading_closes_a_chunk_that_is_big_enough() -> None:
    chunks = chunk([text(1, words(MIN_TOKENS + 50), "1 A"), text(1, words(100), "1 A", "1.1 B")])
    assert [c.section_path for c in chunks] == ["1 A", "1 A > 1.1 B"]


def test_a_small_section_joins_the_next_one_in_the_same_chapter() -> None:
    chunks = chunk([text(1, words(100), "1 A"), text(1, words(100), "1 A", "1.1 B")])
    assert len(chunks) == 1
    assert chunks[0].section_path == "1 A"  # the path where the chunk starts


def test_a_chunk_never_crosses_a_top_level_heading() -> None:
    chunks = chunk([text(1, words(100), "1 A"), text(2, words(100), "2 B")])
    assert [c.section_path for c in chunks] == ["1 A", "2 B"]


def test_pages_cover_every_block_in_the_chunk() -> None:
    chunks = chunk([text(3, words(100), "1 A"), text(4, words(100), "1 A")])
    assert (chunks[0].page_start, chunks[0].page_end) == (3, 4)


def test_no_section_path_without_headings() -> None:
    assert chunk([text(1, "Body.")])[0].section_path is None


def test_a_small_table_stays_whole() -> None:
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    chunks = chunk([Block(1, "table", table, ("1 A",))])
    assert [c.content for c in chunks] == [table]


def test_a_large_table_is_split_by_rows_with_the_header_repeated() -> None:
    header = "| Name | Description |\n|---|---|"
    rows = [f"| item {i} | {words(40)} |" for i in range(60)]  # ~2,400 tokens
    chunks = chunk([Block(1, "table", "\n".join([header, *rows]), ("A",))])
    assert len(chunks) > 1
    for c in chunks:
        assert c.content.startswith(header + "\n")
        assert estimate_tokens(c.content) <= MAX_TOKENS
    # Every row appears exactly once, whole and in order.
    body_rows = [line for c in chunks for line in c.content.split("\n")[2:]]
    assert body_rows == rows
