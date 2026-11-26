# SLO metrics (step 25)

Both Lambdas print one CloudWatch EMF line per request or indexed document, and CloudWatch
Logs turns those lines into 8 custom metrics in namespace `ragdp`. The design, and why there
are 8, is in DECISIONS 2026-11-25, "Step 25".

| Metric | Dimensions | Unit | Stat to read |
| --- | --- | --- | --- |
| `Latency` | `Route` = retrieve, query | Milliseconds | p50, p95, p99 (2xx; query: not cached) |
| `Fault` | `Route` = retrieve, query, other | None (0/1) | Average = error rate, SampleCount = requests |
| `Degraded` | `Route` = query | None (0/1) | Average (not cached) |
| `CacheHit` | `Route` = query | None (0/1) | Average (every 2xx) |
| `IngestFreshness` | none | Seconds | p95 (documents up to 20 pages) |

## Check after a deploy

Generate one of each, then list them. New metrics can take several minutes to show up in
`list-metrics`.

```bash
cd ~/dev/rag-v2
aws sts get-caller-identity --profile ragdp --query Account --output text   # 905418224080
export PYTHONPATH=services AWS_PROFILE=ragdp AWS_REGION=us-east-2
export API_URL=$(terraform -chdir=infra/envs/prod output -raw api_url)
export TOKEN=$(uv run python scripts/loadtest.py token)

# Latency and Fault for retrieve (10 requests, Titan only)
k6 run -e API_URL="$API_URL" load/smoke.js

# Latency, Fault, Degraded and CacheHit for query: the same question twice, a miss then a
# hit (one Titan embedding, one Cohere rerank, one Nova 2 Lite answer: under $0.01)
for i in 1 2; do
  curl -s "$API_URL/v1/query" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"query": "What should a clinical trial monitoring plan include?"}' | jq '{cached, degraded, abstained}'
done

# IngestFreshness: a 1-page synthetic PDF in a throwaway tenant, then delete it
export CHAOS_API_URL=$API_URL CHAOS_TENANT=$(uuidgen | tr A-Z a-z)
uv run python scripts/chaos.py pdf /tmp/fresh-1.pdf --pages 1
uv run python scripts/chaos.py ingest /tmp/fresh-1.pdf --external-id freshness-check   # note doc_id
uv run python scripts/chaos.py delete --doc <doc_id>

# The metrics, and one statistic each
aws cloudwatch list-metrics --namespace ragdp \
  --query 'Metrics[].[MetricName, join(`,`, Dimensions[].Value)]' --output text | sort
aws cloudwatch get-metric-statistics --namespace ragdp --metric-name Latency \
  --dimensions Name=Route,Value=retrieve --statistics SampleCount --extended-statistics p95 \
  --start-time $(date -u -v-1H +%FT%TZ) --end-time $(date -u +%FT%TZ) --period 3600
```

Expected: exactly 8 metrics in `list-metrics`. The second query prints `"cached": true`.

## Logs Insights

The lines are also queryable, including the properties that aren't metrics. Log groups
`/aws/lambda/ragdp-api` and `/aws/lambda/ragdp-worker`:

```
# /v1/retrieve requests that fell back to lexical-only recall
fields @timestamp, status, latency_ms, degraded
| filter Route = "retrieve" and ispresent(degraded) and degraded.0 = "embedding"

# Freshness for every indexed document, large ones too (the metric has <= 20 pages only)
fields @timestamp, @message
| filter @message like /indexed .* s after upload/
```

## Results
