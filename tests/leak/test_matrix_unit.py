"""The leak matrix's checks, on made-up responses: each kind of leak is caught, allowed
content isn't flagged, and the committed embeddings match the current questions. No network.
"""

import json
import math
import uuid

import pytest

from tests.leak import matrix

PERSONAS = {p.name: p for p in matrix.personas()}
DOCS = matrix.documents()
TOKENS = matrix.canaries()
DOC_BY_EXTERNAL_ID = {owner.external_id: doc_id for doc_id, owner in DOCS.items()}


def chunk(external_id: str, content: str = "text") -> dict:
    return {"doc_id": str(DOC_BY_EXTERNAL_ID[external_id]), "content": content}


def retrieval(*chunks: dict, degraded: list[str] | None = None) -> dict:
    return {"chunks": list(chunks), "degraded": degraded or [], "lexical_weight": 1.0}


def token_of(external_id: str) -> str:
    return next(t for t, owner in TOKENS.items() if owner.external_id == external_id)


def test_the_manifests_give_six_personas_24_documents_and_six_canaries():
    assert len(PERSONAS) == 6 and len(DOCS) == 24 and len(TOKENS) == 6
    assert matrix.allowed_canaries(PERSONAS["a-r2"]) == {
        token_of("ta-canary-r1"),
        token_of("ta-canary-r2"),
    }
    assert matrix.allowed_canaries(PERSONAS["b-r1"]) == {token_of("tb-canary-r1")}


def test_allowed_chunks_and_canaries_are_not_leaks():
    result = matrix.PersonaResult(PERSONAS["a-r2"])
    response = retrieval(
        chunk("fda-ich-e6r3"), chunk("ta-canary-r2", f"ref {token_of('ta-canary-r2')}")
    )
    result.add_retrieval("q001", response, DOCS, TOKENS)
    assert result.leaks == []
    assert result.canaries_seen == {token_of("ta-canary-r2")}
    assert result.missing_canaries == {token_of("ta-canary-r1")}


@pytest.mark.parametrize(
    ("persona", "external_id", "kind"),
    [
        ("a-r3", "tb-sop-pv-004-sae-reporting", "structural"),  # another tenant
        ("a-r1", "ta-canary-r2", "structural"),  # a higher role level
        ("b-r3", "fda-ich-e6r3", "structural"),
    ],
)
def test_a_chunk_from_a_document_the_persona_may_not_see_is_a_leak(persona, external_id, kind):
    result = matrix.PersonaResult(PERSONAS[persona])
    result.add_retrieval("q001", retrieval(chunk(external_id)), DOCS, TOKENS)
    assert [leak.kind for leak in result.leaks] == [kind]
    assert external_id in result.leaks[0].detail


def test_a_document_outside_the_manifests_is_a_leak():
    result = matrix.PersonaResult(PERSONAS["a-r1"])
    stray = {"doc_id": str(uuid.uuid4()), "content": "someone else's text"}
    result.add_retrieval("q001", retrieval(stray), DOCS, TOKENS)
    assert [leak.kind for leak in result.leaks] == ["unknown_document"]


def test_a_forbidden_canary_in_allowed_content_is_a_leak():
    # The document is allowed, but its text carries another tenant's canary: the canary
    # check catches what the structural check can't (for example, a mis-tagged chunk).
    result = matrix.PersonaResult(PERSONAS["a-r3"])
    text = f"see {token_of('tb-canary-r1')} and CANARY-TA-R9-ffffff"
    result.add_retrieval("x01", retrieval(chunk("fda-ich-e6r3", text)), DOCS, TOKENS)
    assert sorted(leak.kind for leak in result.leaks) == ["canary", "unknown_canary"]


def test_degraded_and_empty_responses_are_counted():
    result = matrix.PersonaResult(PERSONAS["b-r1"])
    result.add_retrieval("q001", retrieval(degraded=["embedding"]), DOCS, TOKENS)
    assert (result.requests, result.degraded, result.empty, result.chunks) == (1, 1, 1, 0)


def test_the_report_has_one_row_per_persona_and_the_total():
    results = [matrix.PersonaResult(p) for p in PERSONAS.values()]
    results[0].add_retrieval("q001", retrieval(chunk("tb-canary-r1")), DOCS, TOKENS)
    report = matrix.render_report(results, 1)
    assert report.count("\n| a-r") == 3 and report.count("\n| b-r") == 3
    assert "**Total leaks: 1**" in report


# --- the committed embeddings -------------------------------------------------------------


def test_every_question_has_a_current_unit_length_embedding():
    vectors = matrix.eval_embeddings.load()
    assert set(vectors) == {q["id"] for q in matrix.questions()}
    assert len(vectors) == 190
    for vector in vectors.values():
        assert len(vector) == 1024
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, abs_tol=1e-3)


def test_an_edited_question_makes_the_embeddings_stale(tmp_path):
    data = json.loads(matrix.eval_embeddings.EMBEDDINGS.read_text())
    data["embeddings"]["q001"]["text_sha256"] = "0" * 64
    del data["embeddings"]["x10"]
    path = tmp_path / "embeddings.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="q001: the question changed.*x10: no embedding"):
        matrix.eval_embeddings.load(path)


def test_embeddings_for_another_model_are_refused(tmp_path):
    data = json.loads(matrix.eval_embeddings.EMBEDDINGS.read_text())
    path = tmp_path / "embeddings.json"
    path.write_text(json.dumps(data | {"model_id": "cohere.embed-v4:0"}))
    with pytest.raises(ValueError, match="not amazon.titan"):
        matrix.eval_embeddings.load(path)


def test_vectors_round_trip_as_float32():
    vector = [0.5, -0.25, 1 / 3]
    decoded = matrix.eval_embeddings.decode(matrix.eval_embeddings.encode(vector))
    assert decoded[:2] == [0.5, -0.25] and math.isclose(decoded[2], 1 / 3, rel_tol=1e-6)
