# Slice 1 end-to-end run (step 10b)

Runs the real upload and ingest path in production by hand. The API runs on this laptop and reads its DB URL from SSM; everything after the upload is the deployed stack: S3, then EventBridge, then SQS, then the worker Lambda, then Neon `main`.

Proves these Slice 1 boxes (SPEC: Slices):

| Box | Section |
| --- | --- |
| `POST /v1/documents` returns a presigned URL; an upload with the wrong checksum is rejected by S3 | 2, 3 |
| A 20-page PDF goes from upload to `indexed`, visible via `GET /v1/documents/{id}` | 4 |
| Duplicate upload creates no new version; duplicate message creates no duplicate chunks | 5, 6 |
| Corrupt PDF ends in the DLQ with status `failed`, and the alarm fires | 7 |
| Traces show parse, chunk and embed spans with durations (step 10e) | 4, then Honeycomb dataset `ragdp-worker` |

Cost: a few cents of Bedrock embeddings; the rest is inside the free tiers.

## 0. Setup (one terminal)

Sign in first: `aws login --profile ragdp` in Chrome, then check that `aws sts get-caller-identity --profile ragdp` shows `905418224080`. Make the 20-page sample if it's missing (`samples/README.md`).

```bash
cd ~/dev/rag-v2
export AWS_PROFILE=ragdp AWS_REGION=us-east-2
export BUCKET=$(terraform -chdir=infra/envs/prod output -raw raw_bucket)
export QUEUE=$(terraform -chdir=infra/envs/prod output -raw ingest_queue_url)
export DLQ=$(terraform -chdir=infra/envs/prod output -raw dlq_url)

# A fresh tenant for this run, and a token for it from the issuer (step 11). The API
# verifies it against JWT_ISSUER (.env). Tokens last 15 minutes: run `retoken` when a
# request gets a 401.
export PYTHONPATH=services TENANT_ID=$(uuidgen | tr A-Z a-z)
echo "tenant for this run: $TENANT_ID"
retoken() { export TOKEN=$(uv run python scripts/issuer.py mint --tenant "$TENANT_ID" --role-level 3 \
  --scope documents:read --scope documents:write); }
retoken

# The API, in the background, on 127.0.0.1:8000, against production.
uv run python -m api.local \
  --database-url-param /ragdp/prod/database_url_api --bucket "$BUCKET" > /tmp/ragdp-api.log 2>&1 &

# Helpers. register <file> <external_id> prints the API's JSON response.
api() { curl -sS -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' "$@"; }
register() {
  api -X POST http://127.0.0.1:8000/v1/documents -d "$(jq -n \
    --arg e "$2" --arg f "$(basename "$1")" \
    --arg s "$(shasum -a 256 "$1" | cut -d' ' -f1)" --argjson n "$(stat -f%z "$1")" \
    '{external_id: $e, filename: $f, content_sha256: $s, size_bytes: $n, min_role_level: 1}')"
}
put() { curl -sS -o /tmp/ragdp-put.xml -w '%{http_code}\n' -X PUT --upload-file "$1" -H "x-amz-checksum-sha256: $2" "$3"; }
docstatus() { api "http://127.0.0.1:8000/v1/documents/$1" | jq -c '.versions[] | {version, status, attempts, chunk_count, last_error}'; }
```

Check that the API is up: `tail -3 /tmp/ragdp-api.log` shows `Uvicorn running on http://127.0.0.1:8000`.

## 1. Watch the worker (optional, second terminal)

```bash
aws logs tail /aws/lambda/ragdp-worker --follow --profile ragdp --region us-east-2
```

## 2. Register the 20-page PDF

```bash
PDF=samples/NIST.SP.800-207-p1-20.pdf
R=$(register $PDF nist-20); echo "$R" | jq '{doc_id, version, status, duplicate}'
DOC=$(echo "$R" | jq -r .doc_id); URL=$(echo "$R" | jq -r .upload.url)
SUM=$(echo "$R" | jq -r '.upload.headers["x-amz-checksum-sha256"]')
```

Expected: `version` 1, `status` `pending_upload`, `duplicate` false, and an upload URL.

## 3. Wrong checksum: S3 rejects it

Same size, one byte changed, so only the checksum differs:

```bash
cp $PDF /tmp/tampered.pdf && printf 'X' | dd of=/tmp/tampered.pdf bs=1 seek=1000 conv=notrunc 2>/dev/null
put /tmp/tampered.pdf "$SUM" "$URL"; grep -o '<Code>[^<]*' /tmp/ragdp-put.xml
aws s3api head-object --bucket "$BUCKET" --key "raw/$TENANT_ID/$DOC/v1/source.pdf" 2>&1 | tail -1
```

Expected: `400`, `<Code>BadDigest`, and `head-object` reports `Not Found`: S3 stored nothing, so nothing was ingested.

## 4. Real upload: `indexed`

```bash
date; put $PDF "$SUM" "$URL"
for i in $(seq 1 24); do docstatus $DOC; sleep 5; done   # up to 2 minutes; Ctrl-C once it's indexed
```

Expected: `200` from S3, then `pending_upload`, then `parsing`, then `embedding`, then `indexed` with a `chunk_count`, `attempts` 1, and `last_error` null. Note the time from upload to `indexed`: the SLO is 95% of documents of up to 20 pages within 2 minutes (it includes a cold start).

## 5. Duplicate upload: no new version

```bash
register $PDF nist-20 | jq '{version, status, duplicate, upload}'
docstatus $DOC
```

Expected: `version` 1, `duplicate` true, `status` `indexed`, `upload` null; `docstatus` still lists one version.

## 6. Duplicate message: no new chunks

Send the worker the same job again, as if SQS had delivered it twice:

```bash
KEY="raw/$TENANT_ID/$DOC/v1/source.pdf"
ETAG=$(aws s3api head-object --bucket "$BUCKET" --key "$KEY" --query ETag --output text | tr -d '"')
aws sqs send-message --queue-url "$QUEUE" --message-body "$(jq -nc \
  --arg b "$BUCKET" --arg k "$KEY" --arg e "$ETAG" --argjson n "$(stat -f%z $PDF)" \
  --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{schema_version: 1, job_id: "e2e-duplicate-1", job_type: "ingest", source: "s3_event",
    bucket: $b, key: $k, size_bytes: $n, etag: $e, emitted_at: $t}')" --query MessageId
sleep 30; docstatus $DOC
```

Expected: the worker log shows `job e2e-duplicate-1: skipped`, and `docstatus` is unchanged: same `chunk_count`, `attempts` still 1. (Chunks are also unique on `(doc_id, version, chunk_index)` in the schema, so duplicates can't be written at all.)

## 7. Corrupt PDF: DLQ, `failed`, alarm

```bash
head -c 4096 /dev/urandom > /tmp/corrupt.pdf
R=$(register /tmp/corrupt.pdf corrupt-1); BAD=$(echo "$R" | jq -r .doc_id)
put /tmp/corrupt.pdf "$(echo "$R" | jq -r '.upload.headers["x-amz-checksum-sha256"]')" "$(echo "$R" | jq -r .upload.url)"
sleep 30; docstatus $BAD
aws sqs receive-message --queue-url "$DLQ" --message-attribute-names reason --visibility-timeout 0 \
  --query 'Messages[].MessageAttributes.reason.StringValue'
```

Expected: `status` `failed`, `attempts` 1, `last_error` starting `unparseable_pdf:`; the DLQ message has the same reason. One attempt only: a permanent error isn't retried.

The alarm needs a few minutes (SQS publishes queue depth once a minute):

```bash
aws cloudwatch describe-alarms --alarm-names ragdp-ingest-dlq-not-empty --query 'MetricAlarms[0].StateValue'
```

Expected: `ALARM`, and an email from AWS Notifications titled `ALARM: "ragdp-ingest-dlq-not-empty"`.

## 8. Clean up

```bash
kill %1   # stop the local API
```

- **Purge the DLQ once you've seen the alarm** (`aws sqs purge-queue --queue-url "$DLQ"`). While a message sits there the alarm stays in `ALARM`, and it emails only on a change of state, so a real failure later would send no new email. Purging deletes the message permanently.
- The run's rows (tenant `$TENANT_ID`) and S3 objects stay in production: there's no delete endpoint yet (Slice 2). They're small, and the tenant ID is printed at the start, so they can be found later.

## Results

Filled in after each run: date, image tag, time from upload to `indexed`, chunk count, anything unexpected.

### 2026-11-19, image `ef63fa3` (first run)

Run by Claude Code at Chetan's request; tenant `0646d226-024a-40d8-80f4-eed468bbf933`. Every section passed.

| Section | Result |
| --- | --- |
| 2. Register | 201, version 1, `pending_upload`, Regional URL (`ragdp-raw-prod-905418224080.s3.us-east-2.amazonaws.com`) |
| 3. Wrong checksum | `400 BadDigest`: "The SHA256 you specified did not match the calculated checksum." `head-object` 404 |
| 4. Upload to `indexed` | 200 from S3; `indexed` **16 s** after the upload (cold start included), 16 chunks, 1 attempt. Worker: init 5.4 s, duration 4.0 s, max memory 184 of 2048 MB |
| 5. Duplicate upload | 200, same `doc_id`, version 1, `duplicate: true`, `upload: null`; still one version |
| 6. Duplicate message | Worker log `job e2e-duplicate-1: skipped, 0 chunks`; still 16 chunks, 1 attempt |
| 7. Corrupt PDF | `failed` after 1 attempt, `unparseable_pdf: cannot open PDF (FileDataError)`, 4 s after upload; same reason on the DLQ message; alarm `OK` to `ALARM` about 2 minutes later, SNS action succeeded |

Noticed:
- The worker used 184 MB of its 2048 MB. Memory also sets Lambda's CPU, so don't lower it on this one number; measure with the 59-page PDF and larger files first (Slice 4).
- The alarm also notifies on its return to `OK` (`ok_actions`), so purging the DLQ sends an "OK" email. That's intended: it says the incident is over.

### 2026-11-21, image `dda62b1` (step 10e: traces)

Run by Claude Code at Chetan's request; tenant `73b7c720-9a87-44c3-a1c8-c0bd1611c54b`, document `8494d647-f05b-4f73-9cb0-91c5af4dd8f0`. Sections 2, 4 and 6 only (no corrupt PDF, so no alarm email).

| Section | Result |
| --- | --- |
| 2. Register | 201, version 1, `pending_upload` |
| 4. Upload to `indexed` | 200 from S3; `indexed` **25 s** after the upload (cold start included), 16 chunks, 1 attempt. Worker log `tracing on: ragdp-worker to https://api.honeycomb.io`, no export errors. Invocation 8.0 s, max memory 195 of 2048 MB |
| 6. Duplicate message | `job e2e-traces-duplicate-1: skipped, 0 chunks` in 134 ms; still 16 chunks, 1 attempt |
| Traces | Confirmed by Chetan in Honeycomb, dataset `ragdp-worker`: one `ingest` trace per message; the indexed one has `claim`, `load_and_verify`, `parse`, `chunk`, `embed` (`embed_batch`, `insert_chunks`) and `activate`, each with its duration |

Noticed:
- **The cold start's init phase timed out** (`INIT_REPORT Init Duration: 9999.61 ms Status: timeout`), so Lambda re-ran init inside the invocation; the message still succeeded. 10b's init was 5.4 s. Not the new code: the whole handler imports in 0.52 s locally (OpenTelemetry 0.04 s), and the image grew only 2.1 MB (234.4 to 236.5 MB). Most likely the first cold start of a just-deployed image, whose layers Lambda hadn't cached yet. Measure again on the next cold start of the same image; if it repeats, look at image size and lazy-importing PyMuPDF before anything that costs money (provisioned concurrency).
