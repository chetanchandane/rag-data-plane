# Eval corpus and golden set

Step 16 (DECISIONS 2026-11-22, "Step 16"). Everything the leak matrix (step 17) and the quality eval (step 18) run on.

| File | What it is |
| --- | --- |
| `golden/corpus.json` | Tenant A: 12 final FDA guidances (ICH E-series and FDA clinical-trial guidance), 448 pages, each pinned by URL and SHA-256 |
| `golden/synthetic.json`, `golden/synthetic/*.md` | Tenant B's five fictional SOPs, six canary documents (one per tenant and role level) and the prompt-injection document |
| `golden/personas.json` | The two tenants' fixed ids and the six personas (tenant × role level 1 to 3) |
| `golden/questions.jsonl` | 150 answerable questions: question, reference answer, source (`external_id`, pages, section path, verbatim quote) |
| `golden/unanswerable.jsonl` | 30 on-topic questions the corpus can't answer (abstain precision) |
| `golden/extraction.jsonl` | 10 questions that try to extract canaries, restricted text or every document |
| `golden/question_embeddings.json` | Every question's Titan embedding (base64 float32) and text hash, for the leak matrix in CI (step 17) |
| `corpus/` | Downloaded and rendered PDFs (gitignored) |

```bash
uv run python scripts/eval_corpus.py fetch                       # download tenant A's PDFs, check hashes
uv run python scripts/eval_corpus.py render                      # render the synthetic documents (optional; load does it)
PYTHONPATH=services uv run python scripts/eval_corpus.py load    # index all 24 documents on the eval branch
uv run pytest -m corpus                                          # every golden quote is in its document's chunks
uv run pytest -m leak                                            # the leak matrix (step 17), on the eval branch
PYTHONPATH=services uv run python scripts/eval_embeddings.py     # after editing any question
EVAL_LIMIT=3 uv run pytest -m eval -s                            # eval smoke test: 3 + 3 questions, a few cents
uv run pytest -m eval -s                                         # the eval run and gate (step 18), about $1
```

## Eval run and quality gate (step 18)

`pytest -m eval` asks the 150 answerable and 30 unanswerable questions through the real `/v1/query` (in-process, as persona a-r1, on the `eval` branch), scores the answers with the judge (DeepSeek V3.2), saves `results/<date>_<sha>.json` and applies the gate in `gate.json`. What each metric means and why: DECISIONS 2026-11-24, "Step 18 eval baseline".

- **A new baseline** must come from a full run on a clean commit (no `_dirty` or `_sample` in the file name; those are gitignored). Commit the results file, then point `gate.json`'s `baseline` at it.
- **In CI** the same run is `eval.yml`, started by hand from the Actions tab on any branch. The results file is attached to the run as an artifact.
- **Latency is measured where the run happens** (laptop or GitHub runner, not Lambda), with the query embedding taken from the fixture, so `embed` measures nothing.

**The `eval` branch** is a child of `dev` (same roles and passwords) and must have **no expiry**: Neon refuses to copy an expiring branch, so CI's `leak` job would fail, and an expired `eval` takes the corpus with it (DECISIONS 2026-11-24). To create it: Neon console, Branches, New branch, parent `dev`, name `eval`, and don't set an expiration (auto-delete). Then set `NEON_EVAL_ENDPOINT_ID` and the host in both `EVAL_DATABASE_URL_*` lines to its endpoint, and run `load`.

`load` needs `EVAL_DATABASE_URL_INGEST`, `NEON_EVAL_ENDPOINT_ID`, `EMBEDDING_MODEL_ID`, `AWS_REGION` and an `aws login` session; it refuses any database but the `eval` branch and is safe to rerun. Regenerating the golden set (`scripts/eval_golden.py generate`, Mistral Large 3 on Bedrock, about $0.20) replaces the questions; review a sample afterwards.
