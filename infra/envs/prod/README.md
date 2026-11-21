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
  --scope documents:read --scope documents:write)
curl -sS -H "Authorization: Bearer $TOKEN" "$API_URL/v1/whoami"
```

API Gateway refuses a missing, expired or wrong-audience token (401) and a token without the route's scope (403) before the Lambda runs. The access log shows each request's `authorizerError`, and an empty `integrationStatus` when the Lambda wasn't invoked:

```bash
aws logs tail "$(terraform -chdir=infra/envs/prod output -raw api_access_log_group)" \
  --since 15m --profile ragdp --region us-east-2
```
