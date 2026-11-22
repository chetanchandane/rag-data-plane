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
| `corpus/` | Downloaded and rendered PDFs (gitignored) |

```bash
uv run python scripts/eval_corpus.py fetch                       # download tenant A's PDFs, check hashes
uv run python scripts/eval_corpus.py render                      # render the synthetic documents (optional; load does it)
PYTHONPATH=services uv run python scripts/eval_corpus.py load    # index all 24 documents on the eval branch
uv run pytest -m corpus                                          # every golden quote is in its document's chunks
```

`load` needs `EVAL_DATABASE_URL_INGEST`, `NEON_EVAL_ENDPOINT_ID`, `EMBEDDING_MODEL_ID`, `AWS_REGION` and an `aws login` session; it refuses any database but the `eval` branch and is safe to rerun. Regenerating the golden set (`scripts/eval_golden.py generate`, Mistral Large 3 on Bedrock, about $0.20) replaces the questions; review a sample afterwards.
