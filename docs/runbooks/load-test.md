# Load test (steps 24 and 26)

Step 24 gets everything ready: the Lambda quota, a `loadtest` tenant in production with the
12 FDA guidances, k6, and the stage throttle as a Terraform variable. Step 26 adds the real
runs (`retrieve_ramp`, `query_mix`) to this file.

Latencies measured here are client-side, from the laptop: they include the internet hop to
us-east-2 (DECISIONS 2026-11-25, "Slice 4 step list").

## 0. Setup

```bash
cd ~/dev/rag-v2
aws sts get-caller-identity --profile ragdp --query Account --output text   # 905418224080
export PYTHONPATH=services AWS_PROFILE=ragdp AWS_REGION=us-east-2
export API_URL=$(terraform -chdir=infra/envs/prod output -raw api_url)
```

## 1. Prerequisites

- **Lambda concurrency quota.** The account started at 10 concurrent executions, which the
  load test would hit (the ramp, `query_mix` and the worker's 2). Requested through Service
  Quotas (Lambda, "Concurrent executions", us-east-2). Check it:

  ```bash
  aws service-quotas get-service-quota --service-code lambda --quota-code L-B99A9384 \
    --query Quota.Value
  ```

- **k6:** `brew install k6`.

- **Stage throttle.** `api_throttle_rate_limit` and `api_throttle_burst_limit` in
  `infra/envs/prod` default to 10 and 20. `deploy.yml` passes neither, so every push to
  `main` puts them back. For the load window only (step 26), raise them from the laptop with
  the image tag that's deployed, then put them back:

  ```bash
  TAG=$(aws lambda get-function --function-name ragdp-api --query Code.ImageUri --output text | cut -d: -f2)
  cd infra/envs/prod   # TF_VAR_alert_email must be set
  terraform apply -var-file=prod.tfvars -var worker_image_tag=$TAG -var api_image_tag=$TAG \
    -var api_throttle_rate_limit=25 -var api_throttle_burst_limit=50
  # ... runs ...
  terraform apply -var-file=prod.tfvars -var worker_image_tag=$TAG -var api_image_tag=$TAG
  terraform plan  -var-file=prod.tfvars -var worker_image_tag=$TAG -var api_image_tag=$TAG   # No changes
  ```

  The raised plan changes one resource in place, `module.api.aws_apigatewayv2_stage.default`
  (checked 2026-11-25).

## 2. The load-test tenant's corpus

Tenant `c98bf2d7-f2a9-407c-9bbb-6498b96b3767` (`LOADTEST_TENANT` in `scripts/loadtest.py`),
every document at level 1. The same 12 guidances as the eval (`eval/golden/corpus.json`),
through the deployed API, S3 and worker. About $0.02 of Titan. Rerunning only reports.

```bash
uv run python scripts/eval_corpus.py fetch     # the PDFs, hash-checked (skips present files)
uv run python scripts/loadtest.py ingest --api-url "$API_URL" > /tmp/loadtest-ingest.json
```

Expected: exit 0, every document `indexed`. Each row's `seen_at_s.indexed` is seconds from
the upload to `indexed`, queueing included (the worker takes 2 at a time), except rows marked
`uploaded_earlier` (uploaded by an earlier run: counted from this run's start).

A `failed` document stays failed on a rerun (same bytes, same version), and a deleted
document's external id can't be registered again. Check its `last_error`, fix the cause, then
change `LOADTEST_TENANT` and ingest again.

## 3. Smoke run

```bash
export TOKEN=$(uv run python scripts/loadtest.py token)   # level 1, retrieve + query, 15 min
k6 run -e API_URL="$API_URL" load/smoke.js
```

Expected: 10 requests, every check passes (200, chunks returned, not degraded), exit 0.

## Results

### 2026-11-25, image `e1ccc26` (step 24)

- **Quota:** 1000 requested 2026-11-25 (case `179563756600605`), approved the same day:
  Service Quotas and `aws lambda get-account-settings` both show 1000 concurrent executions
  (1000 unreserved). It was still 10 during the runs below, which needed only a few.
- **Throttle variables:** `plan` with the deployed tag: no changes. With `-var 25/50`: one
  in-place update, `module.api.aws_apigatewayv2_stage.default` (20 -> 50, 10 -> 25). Not
  applied.
- **Ingest:** 12 of 12 `indexed`, 447 chunks, 0 transient errors. Every document was seen
  `indexed` within 54 s of its own upload (the 12 were uploaded back to back; worker
  concurrency 2). These are upper bounds after the first document: `watch` checks one
  document at a time, so most were already `indexed` when first looked at. Step 25's
  freshness metric measures this properly.

  | Document | Pages | Chunks |
  | --- | --- | --- |
  | fda-ich-e6r3 | 86 | 82 |
  | fda-informed-consent-2023 | 66 | 67 |
  | fda-ich-e3 | 55 | 40 |
  | fda-ich-e9 | 46 | 48 |
  | fda-ich-e8r1 | 31 | 42 |
  | fda-ich-e10 | 37 | 35 |
  | fda-electronic-systems-qa-2024 | 28 | 29 |
  | fda-ich-e17 | 24 | 28 |
  | fda-ich-e9r1 | 23 | 27 |
  | fda-rbm-2013 | 22 | 26 |
  | fda-rbm-qa-2023 | 13 | 13 |
  | fda-ich-e2a | 17 | 10 |

- **Smoke run:** 10 of 10 requests 200, 30 of 30 checks, exit 0. Warm (the ingest's polling
  had just used the API): p50 192 ms, p95 281 ms, max 331 ms, client-side. Ten requests are a
  wiring check, not a measurement; step 26 has the numbers.
