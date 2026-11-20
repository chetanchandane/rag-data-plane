# infra/envs/prod

Slice 1 production stack: raw bucket, EventBridge rule, ingest queue and DLQ, worker image repository, worker Lambda and its SQS trigger, DLQ alarm, and the monthly budget. Needs `infra/bootstrap` applied first, because the state bucket and the deploy role come from there.

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
4. **Apply everything:** `terraform apply -var-file=prod.tfvars -var worker_image_tag=<sha>`.
5. **Confirm the subscription email** AWS sends to `alert_email`. Alarms aren't delivered until you do.

## Every deploy after that

A push to `main` deploys by itself (`.github/workflows/deploy.yml`, called by `ci.yml` after the production migrations). It builds and pushes the image tagged with the commit SHA, plans, and applies that plan.

The deploy **refuses any plan that deletes or replaces a resource**. For one of those, review and apply it from the laptop with the tag that's currently deployed (the plan error lists the resources):

```bash
TAG=$(aws lambda get-function --profile ragdp --function-name ragdp-worker \
  --query Code.ImageUri --output text | sed 's/.*://')
AWS_PROFILE=ragdp TF_VAR_alert_email=you@example.com \
  terraform apply -var-file=prod.tfvars -var worker_image_tag=$TAG
```

Then push again (or rerun the failed job) so CI deploys the new image.
