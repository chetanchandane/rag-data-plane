"""scripts/eval_corpus.py and the committed eval files (step 16). No network, no database:
fetch runs against a fake opener, and the load guard is tested on its own."""

import hashlib
import importlib.util
import io
import json
import re
import uuid
from pathlib import Path

import pymupdf
import pytest

from worker.chunking import chunk
from worker.pdf import parse

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_corpus.py"
_spec = importlib.util.spec_from_file_location("eval_corpus", _PATH)
eval_corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_corpus)

GOLDEN = eval_corpus.GOLDEN
CANARY = re.compile(r"CANARY-T[AB]-R[123]-[0-9a-f]{6}")


# --- the committed files ------------------------------------------------------------------


def test_the_corpus_manifest_pins_twelve_https_pdfs_by_hash():
    docs = eval_corpus.corpus_manifest()
    assert len(docs) == 12
    assert len({d["external_id"] for d in docs}) == 12
    for d in docs:
        assert d["url"].startswith("https://www.fda.gov/media/")
        assert re.fullmatch(r"[0-9a-f]{64}", d["sha256"])
        assert d["filename"] == f"{d['external_id']}.pdf"
        assert d["min_role_level"] == 1


def test_six_personas_cover_both_tenants_and_every_role_level():
    data = json.loads((GOLDEN / "personas.json").read_text())
    grid = {(p["tenant"], p["role_level"]) for p in data["personas"]}
    assert grid == {(t, r) for t in "AB" for r in (1, 2, 3)}
    for p in data["personas"]:
        assert p["tenant_id"] == data["tenants"][p["tenant"]]["tenant_id"]
    assert len({t["tenant_id"] for t in data["tenants"].values()}) == 2


def test_one_canary_per_tenant_and_role_level_each_in_its_own_file_only():
    docs = eval_corpus.synthetic_manifest()
    canaries = {(d["tenant"], d["min_role_level"]): d for d in docs if d["kind"] == "canary"}
    assert set(canaries) == {(t, r) for t in "AB" for r in (1, 2, 3)}
    texts = {d["external_id"]: (GOLDEN / d["file"]).read_text() for d in docs}
    for (tenant, level), doc in canaries.items():
        token = doc["canary"]
        assert CANARY.fullmatch(token) and token.startswith(f"CANARY-T{tenant}-R{level}-")
        holders = [external_id for external_id, text in texts.items() if token in text]
        assert holders == [doc["external_id"]]
    # Nothing else carries a canary-shaped string.
    others = [d for d in docs if d["kind"] != "canary"]
    assert not any(CANARY.search(texts[d["external_id"]]) for d in others)


def test_there_is_one_prompt_injection_document_tenant_a_level_1():
    (doc,) = [d for d in eval_corpus.synthetic_manifest() if d["kind"] == "injection"]
    assert (doc["tenant"], doc["min_role_level"]) == ("A", 1)
    assert "Ignore all prior instructions" in (GOLDEN / doc["file"]).read_text()


def test_every_document_has_a_stable_id_per_tenant():
    docs = [doc for doc, _, _ in eval_corpus.all_documents()]
    assert len(docs) == 24  # 12 FDA + 5 SOPs + 6 canaries + 1 injection
    assert len({d.doc_id for d in docs}) == 24
    again = [doc for doc, _, _ in eval_corpus.all_documents()]
    assert [d.doc_id for d in docs] == [d.doc_id for d in again]
    first = docs[0]
    assert first.doc_id == uuid.uuid5(
        eval_corpus.DOC_NAMESPACE, f"{first.tenant_id}/{first.external_id}"
    )


# --- render -------------------------------------------------------------------------------


def test_rendering_is_byte_for_byte_repeatable():
    text = (GOLDEN / "synthetic" / "tb-sop-cm-011-monitoring.md").read_text()
    assert eval_corpus.render_pdf(text) == eval_corpus.render_pdf(text)


def test_rendered_text_parses_without_ligatures_and_keeps_canaries_whole():
    for doc in eval_corpus.synthetic_manifest():
        pdf = eval_corpus.render_pdf((GOLDEN / doc["file"]).read_text())
        content = " ".join(c.content for c in chunk(parse(pdf)))
        assert not re.search("[ﬀ-ﬆ]", content), doc["external_id"]
        assert "fictional" in content or doc["tenant"] == "A"
        if doc["canary"]:
            assert doc["canary"] in content


def test_headings_become_bookmarks_and_long_text_spills_onto_new_pages():
    text = (
        "# Title\n\n## Section one\n\n"
        + "\n\n".join(["word " * 120] * 12)
        + "\n\n## Section two\n\nEnd."
    )
    pdf = pymupdf.open(stream=eval_corpus.render_pdf(text), filetype="pdf")
    assert pdf.page_count > 1
    assert [entry[1] for entry in pdf.get_toc()] == ["Title", "Section one", "Section two"]


# --- fetch --------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def entry(data: bytes, name: str = "doc") -> dict:
    return {
        "external_id": name,
        "url": f"https://www.fda.gov/media/1/{name}",
        "filename": f"{name}.pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_fetch_downloads_and_checks_the_hash(tmp_path):
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(b"%PDF-1.7 good")

    eval_corpus.fetch([entry(b"%PDF-1.7 good")], tmp_path, opener)
    assert (tmp_path / "doc.pdf").read_bytes() == b"%PDF-1.7 good"
    assert requests[0].get_header("User-agent") == eval_corpus.USER_AGENT

    eval_corpus.fetch([entry(b"%PDF-1.7 good")], tmp_path, opener)  # already there: no request
    assert len(requests) == 1


def test_fetch_refuses_a_changed_document(tmp_path):
    with pytest.raises(RuntimeError, match="doesn't match the manifest"):
        eval_corpus.fetch([entry(b"expected")], tmp_path, lambda r, timeout: FakeResponse(b"other"))
    assert not (tmp_path / "doc.pdf").exists()


def test_fetch_refuses_plain_http(tmp_path):
    plain = entry(b"x") | {"url": "http://www.fda.gov/media/1/doc"}
    with pytest.raises(ValueError, match="https"):
        eval_corpus.fetch([plain], tmp_path, lambda r, timeout: FakeResponse(b"x"))


# --- the load guard -----------------------------------------------------------------------

EVAL = "ep-eval-branch-12345678"
DEV = "ep-dev-branch-87654321"


def url(endpoint: str) -> str:
    return f"postgresql://app_ingest:pw@{endpoint}-pooler.c-2.us-east-2.aws.neon.tech/neondb"


def test_load_accepts_only_the_eval_endpoint():
    eval_corpus.check_eval_endpoint(url(EVAL), EVAL, DEV)
    for bad, endpoint in [(url(DEV), EVAL), (url("ep-prod-main-1"), EVAL), (url(EVAL), "")]:
        with pytest.raises(SystemExit, match="refusing"):
            eval_corpus.check_eval_endpoint(bad, endpoint, DEV)


def test_load_refuses_when_eval_and_dev_endpoints_are_the_same():
    with pytest.raises(SystemExit, match="dev branch"):
        eval_corpus.check_eval_endpoint(url(DEV), DEV, DEV)
