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

## Baseline (2026-11-25, step 19)

`results/2026-11-25_ce7f4b49934f.json`, the baseline in `gate.json`. Run from a laptop with the code at `ce7f4b4`. The models, judge and answer prompt are the same as in step 18. What changed is the parser (footnotes moved to the end of each document, a paragraph split by a page break kept whole), the LLM timeouts (6 s for Nova, 14 s for Qwen instead of 10 s each), and two golden quotes (q027 and q112, shortened to the part inside one chunk). DECISIONS 2026-11-24, "Step 19".

The golden set changed, so the gate couldn't compare this run with step 18's. It ran on the fixed rules only, and passed. Compared by hand:

| Metric | Step 18 (`94a08e6`) | Step 19 (`ce7f4b4`) | Gate from now on |
| --- | ---: | ---: | --- |
| Citation validity (what users get) | 96.7% | **98.0%** | below 98% fails (SPEC; the temporary 95% floor is gone) |
| Primary's citation validity | 90.7% | 92.7% | drop > 3 points fails |
| Citation retries / still failed | 14 / 5 | 11 / 3 | reported |
| Context recall | 99.3% | 98.0% | drop > 3 points fails |
| Recall@30 | 99.3% | 98.0% | reported |
| Faithfulness (judge) | 99.5% | 99.6% | drop > 3 points fails |
| Answer relevance (judge) | 96.9% | 95.2% | drop > 3 points fails |
| Answered rate (answerable) | 96.7% | 98.0% | reported |
| Abstain precision | 85.7% | 90.9% | below 80% fails |
| Abstain recall | 100% | 100% | reported |
| End to end p50 / p95 | 2.5 s / 10.4 s | 2.5 s / 7.4 s | reported |
| LLM cost per 1,000 queries | $2.09 | $2.09 | reported |

**What the numbers say:**
- **98.0% is 147 of 150, exactly SPEC's line.** One more failure would be 97.3%, and run-to-run noise is about ±2 failures, so a later run of unchanged code can fail the gate by chance.
- **The 3 failures:** q009 and q076 were misquoted by both models. On q134 Qwen hit its 14 s limit.
- **Context recall fell by 2 questions (q043, q078).** Their quotes are now in chunks that span pages 10-11 and start on another topic, so the question no longer recalls them. Both were still answered from other chunks.
- **Answer relevance is 1.7 points lower,** lower on 7 questions and higher on 5. That's within judge noise and the 3-point rule.

## Previous baseline (2026-11-24, step 18)

`results/2026-11-24_94a08e6ee5e1.json`, the baseline in `gate.json` until step 19. Run from a laptop with the code at `94a08e6`. Answers come from Nova 2 Lite, with Qwen3 235B as the backup; the judge is DeepSeek V3.2; answer prompt `2026-11-24.1`.

| Metric | Value | Gate |
| --- | ---: | --- |
| Context recall (the golden quote is in the 5 chunks the model saw) | 99.3% | drop > 3 points fails |
| Recall@30 | 99.3% | reported |
| Faithfulness (judge) | 99.5% | drop > 3 points fails |
| Answer relevance (judge) | 96.9% | drop > 3 points fails |
| Primary's citation validity (before the retry) | 90.7% | drop > 3 points fails |
| Citation validity (what users get) | 96.7% | below 95% fails (temporary; SPEC's target is 98%) |
| Answered rate (answerable questions) | 96.7% | reported |
| Abstain precision | 85.7% | below 80% fails |
| Abstain recall (30 unanswerable questions) | 100% | reported |
| End to end p50 / p95 | 2.5 s / 10.4 s | reported (laptop, in-process; p95 includes the backup model on retries) |
| LLM cost per 1,000 queries | $2.09 | reported (Cohere rerank, one search per query, not included) |

**Latency by stage** (ms; laptop, in-process, Neon's pooled endpoint over the internet):

| Stage | p50 | p95 | Note |
| --- | ---: | ---: | --- |
| query (end to end) | 2,469 | 10,368 | |
| embed | 0.4 | 1.0 | the fixture, not Titan: production adds a Titan call, not measured here |
| sql_recall | 349 | 775 | |
| rerank (Cohere) | 355 | 518 | |
| abstain_gate | 0.06 | 0.08 | |
| llm_generate | 1,881 | 9,454 | both calls on the 14 citation retries |
| citation_check | 0.7 | 1.9 | |

**Warm or cold:** the run's first queries woke the `eval` branch's Neon compute (it scales to zero), so the p95 of `sql_recall` includes that cold start. Everything after it was warm. Lambda and API Gateway hops aren't in these numbers.

**How it got here.** Two earlier full runs are kept next to it. Run 1 (`fb3a82c`) had 89.9% citation validity and 2 timeouts. Run 2 (`11be342`) used the short-quote prompt and had 90.7%, with no timeouts. Run 3 added the citation retry. What changed each time and why: DECISIONS 2026-11-24, "Citation failure fails over".

### Next to ClinRAG (not like for like)

ClinRAG is the earlier Qdrant pipeline. These are its published numbers. Its corpus and its 294 questions no longer exist (DECISIONS 2026-11-22, "Step 16"), so this compares two systems on **different documents, different questions and different judges** (ClinRAG used RAGAS; this uses hand-written DeepSeek prompts). Read it as "the same kind of measurement, both healthy", not as a controlled A/B.

| | ClinRAG (Qdrant, 294 questions) | This system (pgvector + RLS, 180 questions) |
| --- | ---: | ---: |
| Faithfulness | 0.79 | 0.995 |
| Answer relevance | 0.75 | 0.969 |
| Context recall | 0.89 | 0.993 (a verbatim golden quote in the model's context: stricter than RAGAS' judged recall) |
| Context precision | 0.80 | not measured |
| Median latency | 15.2 s | 2.5 s (laptop, in-process, fixture embedding: no Titan, Lambda or API Gateway; how ClinRAG measured its 15.2 s isn't recorded) |
| Unanswerable questions refused | not measured | 30 of 30 |
| Citations checked against the source text | no | yes: a quote that isn't verbatim never reaches the user |
