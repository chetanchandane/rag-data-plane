"""scripts/eval_golden.py and the committed golden set (step 16). Fake Bedrock; no network.
The check that every quote is in its document parses the downloaded PDFs: it's opt-in
(`pytest -m corpus`) and skipped when they aren't there."""

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_golden.py"
_spec = importlib.util.spec_from_file_location("eval_golden", _PATH)
eval_golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_golden)

GOLDEN = eval_golden.GOLDEN


def rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (GOLDEN / name).read_text().splitlines()]


def manifest_ids() -> set[str]:
    return {d["external_id"] for d in json.loads((GOLDEN / "corpus.json").read_text())["documents"]}


# --- the committed set --------------------------------------------------------------------


def test_150_answerable_questions_with_sources_in_the_corpus():
    questions = rows("questions.jsonl")
    assert len(questions) == 150
    assert len({q["id"] for q in questions}) == 150
    assert len({eval_golden.normalize(q["question"]) for q in questions}) == 150
    for q in questions:
        assert q["question"].strip() and q["reference_answer"].strip()
        assert q["source"]["external_id"] in manifest_ids()
        assert len(q["source"]["quote"].split()) >= eval_golden.MIN_QUOTE_WORDS
        assert "..." not in q["source"]["quote"]
    # Every document contributes.
    assert set(Counter(q["source"]["external_id"] for q in questions)) == manifest_ids()


def test_30_unanswerable_and_10_extraction_questions():
    unanswerable = rows("unanswerable.jsonl")
    assert len(unanswerable) == 30 and len({u["id"] for u in unanswerable}) == 30
    assert len({eval_golden.normalize(u["question"]) for u in unanswerable}) == 30
    extraction = rows("extraction.jsonl")
    assert len(extraction) == 10 and len({x["id"] for x in extraction}) == 10


@pytest.mark.corpus  # parses all 448 pages: minutes on this laptop, so opt-in (-m corpus)
def test_every_quote_is_in_a_chunk_of_its_document():
    if not all((eval_golden.CORPUS_DIR / f"{i}.pdf").exists() for i in manifest_ids()):
        pytest.skip("the corpus PDFs aren't downloaded (scripts/eval_corpus.py fetch)")
    by_doc: dict[str, list[str]] = {}
    for c in eval_golden.corpus_chunks():
        by_doc.setdefault(c.external_id, []).append(eval_golden.normalize(c.content))
    for q in rows("questions.jsonl"):
        quote = eval_golden.normalize(q["source"]["quote"])
        assert any(quote in content for content in by_doc[q["source"]["external_id"]]), q["id"]


# --- quote checking -----------------------------------------------------------------------

CHUNK = (
    "The sponsor should ensure that monitoring reports are reviewed. "
    "Follow-up actions should be documented and tracked to resolution."
)


def test_a_verbatim_quote_is_kept():
    quote = "monitoring reports are reviewed"
    assert eval_golden.supporting_span(quote, CHUNK) == quote


def test_whitespace_and_case_differences_are_allowed():
    assert eval_golden.supporting_span("MONITORING  reports\nare reviewed", CHUNK)


def test_an_ellipsis_joined_quote_keeps_its_longest_verbatim_piece():
    quote = "monitoring reports are reviewed... actions should be documented and tracked"
    assert eval_golden.supporting_span(quote, CHUNK) == "actions should be documented and tracked"
    assert eval_golden.supporting_span(quote.replace("...", "…"), CHUNK)


@pytest.mark.parametrize(
    "quote",
    [
        "monitoring reports are approved",  # changed word
        "monitoring reports are reviewed... and then archived for ten years",  # one piece made up
        "the sponsor... reviewed",  # pieces too short to identify a passage
        "",
    ],
)
def test_anything_else_is_rejected(quote):
    assert eval_golden.supporting_span(quote, CHUNK) is None


# --- one question per chunk, with a fake model --------------------------------------------

SOURCE = eval_golden.SourceChunk("fda-ich-e6r3", "E6(R3)", 42, 42, "3 Sponsor", CHUNK)


class FakeBedrock:
    def __init__(self, payload: dict | None) -> None:
        self.payload = payload
        self.requests: list[dict] = []

    def converse(self, **request):
        self.requests.append(request)
        content = []
        if self.payload is not None:
            tool_use = {"toolUseId": "t", "name": "submit_question", "input": self.payload}
            content.append({"toolUse": tool_use})
        return {"output": {"message": {"content": content}}, "stopReason": "tool_use"}


GOOD = {
    "usable": True,
    "question": "Who reviews monitoring reports?",
    "answer": "The sponsor.",
    "quote": "monitoring reports are reviewed",
}


def test_a_good_question_is_kept_with_its_source():
    client = FakeBedrock(GOOD)
    row, reason = eval_golden.question_for(client, SOURCE)
    assert reason == "kept"
    assert row["source"] == {
        "external_id": "fda-ich-e6r3",
        "page_start": 42,
        "page_end": 42,
        "section_path": "3 Sponsor",
        "quote": "monitoring reports are reviewed",
    }
    request = client.requests[0]
    assert request["modelId"] == eval_golden.GENERATOR_MODEL
    assert request["toolConfig"]["toolChoice"] == {"tool": {"name": "submit_question"}}


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, "no_tool_call"),
        (GOOD | {"usable": False}, "declined"),
        (GOOD | {"question": " "}, "empty_field"),
        (GOOD | {"quote": "reports are approved by the board"}, "quote_not_in_chunk"),
    ],
)
def test_unusable_output_is_dropped_with_a_reason(payload, reason):
    assert eval_golden.question_for(FakeBedrock(payload), SOURCE) == (None, reason)


# --- sampling -----------------------------------------------------------------------------


def chunks_for(doc: str, n: int) -> list:
    text = "word " * (eval_golden.MIN_CHUNK_TOKENS * 2)
    return [eval_golden.SourceChunk(doc, doc, i, i, None, f"{doc} {i} {text}") for i in range(n)]


def test_sampling_is_repeatable_and_spread_across_documents():
    pool = chunks_for("big", 80) + chunks_for("small", 4) + chunks_for("mid", 16)
    first = eval_golden.sample(pool, 20)
    assert first == eval_golden.sample(pool, 20)
    counts = Counter(c.external_id for c in first)
    assert counts["small"] >= 2 and counts["big"] > counts["mid"] > 0


def test_very_short_chunks_are_never_sampled():
    short = [eval_golden.SourceChunk("d", "d", 1, 1, None, "Contents")]
    assert eval_golden.sample(short + chunks_for("d", 3), 3)
    assert all(c.content != "Contents" for c in eval_golden.sample(short + chunks_for("d", 3), 3))
