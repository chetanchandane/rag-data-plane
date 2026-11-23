"""The leak matrix's ground truth and checks (step 17; SPEC: Proof, "Leak matrix";
DECISIONS 2026-11-22, "Step 17").

Ground truth comes from the committed manifests in eval/golden, never from the database: a
document's tenant and role level are what the manifests say. A broken RLS policy therefore
can't vouch for the rows it lets through.
"""

import importlib.util
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]

# Any canary-shaped string, including one that isn't in the manifest.
CANARY = re.compile(r"CANARY-T[A-Z]+-R\d+-[0-9a-f]+")


def load_script(name: str) -> ModuleType:
    """A module from scripts/ (they aren't a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_corpus = load_script("eval_corpus")
eval_embeddings = load_script("eval_embeddings")


@dataclass(frozen=True)
class Persona:
    name: str
    tenant: str  # "A" or "B"
    tenant_id: uuid.UUID
    role_level: int

    def may_see(self, tenant: str, min_role_level: int) -> bool:
        return tenant == self.tenant and min_role_level <= self.role_level


@dataclass(frozen=True)
class Owner:
    """Who a document or canary belongs to, per the manifests."""

    external_id: str
    tenant: str
    min_role_level: int


@dataclass(frozen=True)
class Leak:
    persona: str
    question_id: str
    kind: str  # structural, unknown_document, canary, unknown_canary
    detail: str


def personas() -> list[Persona]:
    data = json.loads((eval_corpus.GOLDEN / "personas.json").read_text())
    return [
        Persona(p["persona"], p["tenant"], uuid.UUID(p["tenant_id"]), p["role_level"])
        for p in data["personas"]
    ]


def documents() -> dict[uuid.UUID, Owner]:
    """doc_id -> owner, for all 24 eval documents (the ids eval_corpus.py loads them under)."""
    tenant_keys = {tenant_id: key for key, tenant_id in eval_corpus.tenants().items()}
    return {
        doc.doc_id: Owner(doc.external_id, tenant_keys[doc.tenant_id], doc.min_role_level)
        for doc, _, _ in eval_corpus.all_documents()
    }


def canaries() -> dict[str, Owner]:
    """canary token -> the document (tenant, level) that carries it."""
    return {
        d["canary"]: Owner(d["external_id"], d["tenant"], d["min_role_level"])
        for d in eval_corpus.synthetic_manifest()
        if d["canary"]
    }


def allowed_canaries(persona: Persona) -> set[str]:
    return {
        token
        for token, owner in canaries().items()
        if persona.may_see(owner.tenant, owner.min_role_level)
    }


def questions() -> list[dict]:
    """[{"id", "question"}]: golden, unanswerable and extraction questions."""
    return eval_embeddings.all_questions()


# --- checks -------------------------------------------------------------------------------


def check_text(
    persona: Persona, question_id: str, text: str, tokens: dict[str, Owner]
) -> tuple[list[Leak], set[str]]:
    """Canaries in text: (leaks, the allowed canaries seen)."""
    leaks, seen = [], set()
    for token in CANARY.findall(text):
        owner = tokens.get(token)
        if owner is None:
            leaks.append(Leak(persona.name, question_id, "unknown_canary", token))
        elif persona.may_see(owner.tenant, owner.min_role_level):
            seen.add(token)
        else:
            leaks.append(Leak(persona.name, question_id, "canary", token))
    return leaks, seen


def check_document(
    persona: Persona, question_id: str, doc_id: str, docs: dict[uuid.UUID, Owner]
) -> list[Leak]:
    """The structural check: tenant == persona's tenant and min_role_level <= its role."""
    owner = docs.get(uuid.UUID(doc_id))
    if owner is None:
        # Not an eval document, so not this persona's tenant's: no eval tenant has others.
        return [Leak(persona.name, question_id, "unknown_document", doc_id)]
    if not persona.may_see(owner.tenant, owner.min_role_level):
        detail = f"{owner.external_id} (tenant {owner.tenant}, level {owner.min_role_level})"
        return [Leak(persona.name, question_id, "structural", detail)]
    return []


@dataclass
class PersonaResult:
    persona: Persona
    requests: int = 0
    chunks: int = 0
    empty: int = 0  # responses with no chunks
    degraded: int = 0
    documents: set[str] = field(default_factory=set)
    canaries_seen: set[str] = field(default_factory=set)
    leaks: list[Leak] = field(default_factory=list)

    def add_retrieval(
        self,
        question_id: str,
        response: dict,
        docs: dict[uuid.UUID, Owner],
        tokens: dict[str, Owner],
    ) -> None:
        """Record one /v1/retrieve response and check every chunk in it."""
        self.requests += 1
        self.degraded += bool(response["degraded"])
        self.empty += not response["chunks"]
        for chunk in response["chunks"]:
            self.chunks += 1
            self.leaks += check_document(self.persona, question_id, chunk["doc_id"], docs)
            owner = docs.get(uuid.UUID(chunk["doc_id"]))
            if owner is not None:
                self.documents.add(owner.external_id)
            leaks, seen = check_text(self.persona, question_id, chunk["content"], tokens)
            self.leaks += leaks
            self.canaries_seen |= seen

    @property
    def missing_canaries(self) -> set[str]:
        """Allowed canaries never seen: the positive control."""
        return allowed_canaries(self.persona) - self.canaries_seen


def render_report(results: list[PersonaResult], questions_asked: int) -> str:
    """The per-persona table (CI job summary and README)."""
    lines = [
        f"Leak matrix: {questions_asked} questions × {len(results)} personas "
        f"= {sum(r.requests for r in results)} `/v1/retrieve` calls (top 30, no reranking).",
        "",
        "| Persona | Chunks returned | Documents seen | Canaries seen (all allowed) "
        "| Allowed canaries missed | Leaks |",
        "| --- | ---: | ---: | --- | --- | ---: |",
    ]
    for r in results:
        seen = ", ".join(sorted(r.canaries_seen)) or "none"
        missed = ", ".join(sorted(r.missing_canaries)) or "none"
        lines.append(
            f"| {r.persona.name} | {r.chunks} | {len(r.documents)} | {seen} | {missed} "
            f"| {len(r.leaks)} |"
        )
    total = sum(len(r.leaks) for r in results)
    lines += ["", f"**Total leaks: {total}**"]
    return "\n".join(lines)
