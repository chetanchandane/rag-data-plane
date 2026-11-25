# Chaos table (step 23)

SPEC: Proof, "Chaos tests". The dependency rows run in CI; the queue and worker rows run here,
on the deployed stack, as one dedicated test tenant (DECISIONS 2026-11-25, "Step 23").

| Fault | Expected behavior | Where |
| --- | --- | --- |
| Worker crashes halfway through embedding | Retry resumes; chunk count matches a clean run; no duplicates | Deployed, section 3 |
| Same message delivered twice | One version, one set of chunks | Deployed, section 4 |
| Corrupt PDF | 1 attempt, then DLQ; status `failed` with error; alarm fires | Deployed, section 6 |
| v2 finishes before v1 | `active_version` stays 2; v1 ends `superseded` | Deployed, section 5 |
| Reranker unreachable | 200 with `degraded: ["reranker"]`; quality delta recorded | CI: `tests/api/test_chaos.py`; delta: `uv run pytest -m chaos -s` |
| Primary LLM returns 5xx | Secondary answers; both down gives an abstain, not a 500 | CI: `tests/api/test_chaos.py` |
| DB statement timeout | 503 with `Retry-After` | CI: `tests/api/test_chaos.py` |

Costs: Titan embeddings for about 1,000 chunks (under $0.02), plus pennies of Lambda, S3 and
SQS. The crash section changes the production worker's timeout for about a minute, out of
band: **confirm with Chetan in the session before running it**, and check with
`terraform plan` afterwards.

## 0. Setup

Sign in (`aws login --profile ragdp`) and check `aws sts get-caller-identity --profile ragdp`
shows `905418224080`. `JWT_ISSUER` comes from `.env`.

```bash
cd ~/dev/rag-v2
export AWS_PROFILE=ragdp AWS_REGION=us-east-2 PYTHONPATH=services
export CHAOS_TENANT=$(uuidgen | tr A-Z a-z); echo "tenant for this run: $CHAOS_TENANT"
export CHAOS_API_URL=$(terraform -chdir=infra/envs/prod output -raw api_url)
export CHAOS_BUCKET=$(terraform -chdir=infra/envs/prod output -raw raw_bucket)
export CHAOS_QUEUE_URL=$(terraform -chdir=infra/envs/prod output -raw ingest_queue_url)
export DLQ=$(terraform -chdir=infra/envs/prod output -raw dlq_url)
chaos() { uv run python scripts/chaos.py "$@"; }

# The synthetic PDFs: 210 pages (305 chunks) and 1 page. Seeded, so the same bytes every time.
chaos pdf /tmp/chaos-300.pdf --pages 210
chaos pdf /tmp/chaos-small.pdf --pages 1
```

Before starting, record: the worker's timeout (`aws lambda get-function-configuration
--function-name ragdp-worker --query Timeout`: 300), the DLQ depth (0) and the alarm state
(`OK`).

## 1. Clean run (the reference)

```bash
chaos ingest /tmp/chaos-300.pdf --external-id chaos-clean     # prints doc_id and a timeline
chaos check --doc <clean doc_id>
```

Expected: `indexed`, `rows` = `distinct` = `chunk_count`. Note `content_md5`: the crash run
must end with the same hash. The timeline gives when the version reached `embedding` and
`indexed`, measured from the upload. The worker's REPORT line (CloudWatch,
`/aws/lambda/ragdp-worker`) gives the invocation's duration, which excludes the cold start.

## 2. Pick the crash timeout

The only way to kill a running Lambda from outside is its own timeout. Parsing is about 15x
slower than embedding (PyMuPDF's table finder reads every character), so there's no PDF where
embedding dominates. Instead the timeout is set from the clean run:

```
timeout = (time in the invocation when embedding starts) + (embedding time) / 2
```

## 3. Worker crashes halfway through embedding

Run it as one block. The parentheses make a subshell, so its `EXIT` trap fires when the block
ends, however it ends (a failed command, Ctrl-C), and the timeout goes back to 300 s, the
value Terraform sets. A trap in your interactive shell would only fire when the terminal
closed.

```bash
(
  set -e
  restore() { aws lambda update-function-configuration --function-name ragdp-worker \
    --timeout 300 --query Timeout && aws lambda wait function-updated --function-name ragdp-worker; }
  trap restore EXIT
  aws lambda update-function-configuration --function-name ragdp-worker --timeout <seconds> --query Timeout
  aws lambda wait function-updated --function-name ragdp-worker
  chaos ingest /tmp/chaos-300.pdf --external-id chaos-crash --limit 75
)
aws lambda get-function-configuration --function-name ragdp-worker --query Timeout   # 300
chaos check --doc <crash doc_id>
```

Expected right after the kill: the REPORT line ends `Status: timeout`; `check` shows
`embedding`, `attempts` 1, a live lease, and `0 < rows < chunk_count` with `rows` =
`distinct`. If `rows` is 0 the kill landed in parsing, and if the version is `indexed` it
landed too late: delete that document (section 8), adjust the timeout and repeat.

Then wait. Lambda's failed invocation leaves the message invisible for the queue's 1800 s
visibility timeout; the version's 15-minute lease has expired by then, so the redelivery
re-claims it (`attempts` 2) and embeds only the missing chunks.

```bash
chaos check --doc <crash doc_id>      # about 31 minutes after the kill
```

Expected: `indexed`, `attempts` 2, `rows` = `distinct` = the clean run's count, and the clean
run's `content_md5`.

Then confirm the stack matches Terraform (the timeout is back): `terraform plan` as in
`infra/envs/prod/README.md`, with the deployed image tags and `TF_VAR_alert_email` set to the
`ragdp-alerts` subscription's endpoint. Expected: `No changes`.

## 4. Same message delivered twice

```bash
chaos duplicate --doc <clean doc_id>     # re-sends v1's S3 job, waits 30 s, then checks
```

The command waits until the worker's log shows that job id, so "unchanged" can't mean "not
delivered yet". Expected: the log line `job chaos-duplicate-…: skipped, 0 chunks`, and `check`
unchanged (same `rows`, `attempts` 1, same `content_md5`).

## 5. v2 finishes before v1

```bash
chaos out-of-order /tmp/chaos-300.pdf /tmp/chaos-small.pdf --external-id chaos-ooo
chaos check --doc <ooo doc_id>
```

v1 (305 chunks, about 45 s) is uploaded 3 s before v2 (1 page, about 3 s). The worker runs
two at once, so v2 is indexed while v1 is still parsing. Expected: `active_version` 2; v2
`indexed`; v1 `superseded`, never indexed, and its chunks cleaned up (`rows` 0).

## 6. Corrupt PDF

```bash
head -c 4096 /dev/urandom > /tmp/corrupt.pdf
chaos ingest /tmp/corrupt.pdf --external-id chaos-corrupt --limit 60
aws sqs receive-message --queue-url "$DLQ" --message-attribute-names All --visibility-timeout 0 \
  --query 'Messages[].MessageAttributes.reason.StringValue'
aws cloudwatch describe-alarms --alarm-names ragdp-ingest-dlq-not-empty --query 'MetricAlarms[0].StateValue'
```

Expected: `failed`, `attempts` 1, `last_error` starting `unparseable_pdf:`; the DLQ message
has the same reason; the alarm goes `ALARM` within a few minutes and emails.

## 7. Clean up

- **Purge the DLQ once the alarm has fired** (`aws sqs purge-queue --queue-url "$DLQ"`), or a
  real failure later would send no new email. The alarm then returns to `OK` and emails that.
- **Delete the run's documents** through the deployed API, which also runs the step 21 purge
  on the deployed worker:

  ```bash
  chaos delete --doc <clean> --doc <crash> --doc <ooo> --doc <corrupt>
  chaos check --doc <clean> ...     # after a minute: no versions left
  ```

## Results

### 2026-11-25, image `83c37e3` (steps 20 to 22 deployed)

Run by Claude Code at Chetan's request; tenant `937ed595-d3a8-4f8f-859b-209b47d70511`.

Before: worker timeout 300 s, DLQ 0, alarm `OK`, max concurrency 2. After: the same, and
`terraform plan` showed `No changes`. Every row passed.

| Row | Result |
| --- | --- |
| 1. Clean run | 210-page synthetic PDF: `indexed` 56 s after the upload (cold start: init 8.5 s, invocation 45.7 s). In the invocation: about 38 s before embedding (load, verify, parse, chunk), about 7.5 s to embed 305 chunks in 10 batches. `rows` = `distinct` = 305, `content_md5` `4d4e67cf…`, the same as a local parse of the same file |
| 3. Worker crash | Timeout set to **41 s**. REPORT: `Duration: 41000.00 ms … Status: timeout`. Right after: `embedding`, `attempts` 1, a live lease, **160 of 305 rows** (5 of 10 batches), 160 distinct. Timeout back to 300 s at once. SQS redelivered the same message (`receive 2`) at 19:28:13, exactly 30 min after the first receive; the worker re-claimed it and logged `indexed, 305 chunks`. After: `attempts` 2, `rows` = `distinct` = 305, **`content_md5` equal to the clean run's**. The resumed invocation took 41.2 s against the clean run's 45.7 s, which fits embedding the 145 missing chunks (5 batches) rather than all 305 |
| 4. Duplicate message | `job chaos-duplicate-31b6b4d8: skipped, 0 chunks`; still 305 rows, `attempts` 1, same hash. Repeated after the review fix, which waits for the worker's log line: `job chaos-duplicate-018abf3f: skipped, 0 chunks`, unchanged |
| 5. v2 before v1 | v2 (1 page) `indexed` 3.5 s after its upload while v1 was parsing; v1 reached `embedding` at 38 s and ended `superseded` at 46 s, never indexed, its 305 chunks cleaned up (0 rows). `active_version` 2 throughout |
| 6. Corrupt PDF | `failed` 2.3 s after the upload, `attempts` 1, `unparseable_pdf: cannot open PDF (FileDataError)`; the DLQ message carries the same reason. Alarm `OK` to `ALARM` about 2 min after the upload, SNS action succeeded. DLQ purged: `ALARM` to `OK` 2 min later, also emailed |
| 7. Clean up | 4 DELETEs, all 202. The deployed worker purged each one (`PurgeResult(objects=1, versions=1, chunks=305)` and so on; the out-of-order document 2 objects, 2 versions); `check` found no versions and no S3 objects were left under the tenant |

Noticed:
- **The kill window is narrow.** The embed phase is about 7.5 s of a 46 s invocation, because parsing dominates. The 41 s timeout landed after 5 of 10 batches on the first try.
- **`lease_until` is left in the future after a version is indexed** (`lease_live: true` on `indexed` rows). Harmless: the claim only re-claims `parsing`/`embedding` with an expired lease, and nothing reads the lease of an `indexed` version.
- **Not verified here:** `ingest.chunks_to_embed` on the resumed trace in Honeycomb (expected 145). The resume logic itself is step 6's tested guarantee; this run shows it on the deployed path (SQS redelivery after a real kill, lease expiry, re-claim).
