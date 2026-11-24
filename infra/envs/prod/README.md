# infra/envs/prod

Production stack: raw bucket, EventBridge rule, ingest queue and DLQ, worker image repository, worker Lambda and its SQS trigger, DLQ alarm, the monthly budget (Slice 1), and the token issuer (step 11): a private bucket and a CloudFront distribution serving the OIDC discovery document and `jwks.json`. Needs `infra/bootstrap` applied first, because the state bucket and the deploy role come from there.

## Plan

```bash
aws sts get-caller-identity --profile ragdp          # must show 905418224080
cd infra/envs/prod
AWS_PROFILE=ragdp terraform init
AWS_PROFILE=ragdp TF_VAR_alert_email=you@example.com \
  terraform plan -var-file=prod.tfvars -var worker_image_tag=<tag>
```

`alert_email` stays out of the repo. In GitHub it's the `prod` environment's secret `ALERT_EMAIL`, which `deploy.yml` passes as `TF_VAR_alert_email`.

## First deploy (once)

The Lambda needs an image in ECR, and the ECR repository is part of this stack. So the order is:

1. **Create only the repository:** `terraform apply -var-file=prod.tfvars -var worker_image_tag=none -target=module.ingest.aws_ecr_repository.worker`.
2. **Build and push the worker image** (Docker via OrbStack or Colima), tagged with the git SHA:
   ```bash
   docker build --platform linux/amd64 --provenance=false -f services/worker/Dockerfile -t <repo_url>:<sha> .
   aws ecr get-login-password --profile ragdp | docker login --username AWS --password-stdin <repo_url>
   docker push <repo_url>:<sha>
   ```
3. **Set up the production database and its URL parameters**, outside Terraform so no secret enters its state. In your own terminal (it prompts, hidden, for the Neon owner URL of the production branch `main`, direct endpoint):
   ```bash
   uv run python db/bootstrap.py --production --aws-account-id 905418224080
   ```
   It rotates the three role passwords, stores the role URLs as SecureStrings under `/ragdp/prod/` (`database_url_ingest` is the worker's), and runs the migrations. Nothing else keeps the passwords. Re-running it rotates them again; a worker that's running picks up the new URL at its next cold start.
   The Honeycomb ingest key for traces (step 10e) is stored the same way, out of band. The prompt hides it:
   ```bash
   uv run python -c "import boto3, getpass; boto3.Session(profile_name='ragdp').client('ssm', region_name='us-east-2').put_parameter(Name='/ragdp/prod/honeycomb_api_key', Type='SecureString', Value=getpass.getpass('Honeycomb ingest key: '), Overwrite=True)"
   ```
   Without it the worker still runs, with tracing off (it logs `tracing off: setup failed`).
4. **Apply everything:** `terraform apply -var-file=prod.tfvars -var worker_image_tag=<sha>`.
5. **Confirm the subscription email** AWS sends to `alert_email`. Alarms aren't delivered until you do.

## Adding the API (step 13, once)

Same reason as above: the API Lambda needs an image in a repository this stack creates. CI builds the image once the repository exists.

1. **Apply bootstrap** (the deploy role gains HTTP APIs): `AWS_PROFILE=ragdp terraform -chdir=infra/bootstrap apply`.
2. **Create only the API's repository**, with the worker tag that's deployed now:
   ```bash
   TAG=$(aws lambda get-function --profile ragdp --function-name ragdp-worker \
     --query Code.ImageUri --output text | sed 's/.*://')
   AWS_PROFILE=ragdp TF_VAR_alert_email=you@example.com terraform apply -var-file=prod.tfvars \
     -var worker_image_tag=$TAG -var api_image_tag=none -target=module.api.aws_ecr_repository.api
   ```
3. **Push to `main`.** CI builds and pushes both images and applies the rest of the stack.

## Adding answers (step 15, once)

`POST /v1/query` reranks with Cohere and answers with Amazon Nova 2 Lite, falling back to Qwen3 235B (DECISIONS 2026-11-22, "Step 15", and 2026-11-24, "LLMs without Anthropic"). Both are AWS-billed Bedrock models, so no model-access form or Marketplace subscription is needed. Do these before the push that deploys it; without them the API still runs, degraded.

1. **(Only if switching back to Claude.)** This account is refused Anthropic models (`AccessDeniedException … not available for this account`), even though `get-foundation-model-availability` reports them as available. Check with one real call, not the availability API, before changing `prod.tfvars`. Anthropic models are billed through Marketplace: AWS creates the subscription on the first call, so make that call from your own login (the API's role can't subscribe).
2. **Store the Cohere Production key** the same way as the other keys. The prompt hides it:
   ```bash
   uv run python -c "import boto3, getpass; boto3.Session(profile_name='ragdp').client('ssm', region_name='us-east-2').put_parameter(Name='/ragdp/prod/cohere_api_key', Type='SecureString', Value=getpass.getpass('Cohere key: '), Overwrite=True)"
   ```
   Without it the API logs `reranker off` and answers with RRF order and `degraded: ["reranker"]`. A new key reaches the API at its next cold start.
3. **Apply bootstrap:** the workload boundary gains the US inference profiles and the models in the Regions they route to (it already covers every model in us-east-2, so Qwen needs nothing more). `AWS_PROFILE=ragdp terraform -chdir=infra/bootstrap apply`.
4. **Push to `main`.**

## Every deploy after that

A push to `main` deploys by itself (`.github/workflows/deploy.yml`, called by `ci.yml` after the production migrations). It builds and pushes the worker and API images tagged with the commit SHA, plans, and applies that plan.

The deploy **refuses any plan that deletes or replaces a resource**. For one of those, review and apply it from the laptop with the tag that's currently deployed (the plan error lists the resources):

```bash
TAG=$(aws lambda get-function --profile ragdp --function-name ragdp-worker \
  --query Code.ImageUri --output text | sed 's/.*://')
AWS_PROFILE=ragdp TF_VAR_alert_email=you@example.com \
  terraform apply -var-file=prod.tfvars -var worker_image_tag=$TAG -var api_image_tag=$TAG
```

Both images carry the same tag, so one `TAG` serves both. Then push again (or rerun the failed job) so CI deploys the new images.

## Token issuer

`jwks.json` here holds the issuer's public keys; `scripts/issuer.py` writes it and Terraform publishes it, so planning this stack needs the file. Creating, rotating and retiring keys, and the waits between those steps: `docs/runbooks/issuer-keys.md`.

## Calling the API

```bash
export PYTHONPATH=services API_URL=$(terraform -chdir=infra/envs/prod output -raw api_url)
TOKEN=$(uv run python scripts/issuer.py mint --tenant <uuid> --role-level 2 \
  --scope documents:read --scope documents:write --scope retrieve --scope query)
curl -sS -H "Authorization: Bearer $TOKEN" "$API_URL/v1/whoami"
curl -sS -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query": "policy enforcement point", "top_k": 5}' "$API_URL/v1/retrieve"
curl -sS -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query": "What does a policy enforcement point do?"}' "$API_URL/v1/query"
```

API Gateway refuses a missing, expired or wrong-audience token (401) and a token without the route's scope (403) before the Lambda runs. The access log shows each request's `authorizerError`, and an empty `integrationStatus` when the Lambda wasn't invoked:

```bash
aws logs tail "$(terraform -chdir=infra/envs/prod output -raw api_access_log_group)" \
  --since 15m --profile ragdp --region us-east-2
```
