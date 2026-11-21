# CLAUDE.md

## What this repo is

The **data plane** for `rag-control-plane`: a domain-agnostic RAG ingestion and retrieval API on AWS. It exists to show production engineering, not a demo: failure-tolerant ingestion, tenant and role isolation enforced by Postgres RLS, measured quality, and graceful degradation, for under $30/month.

The full design is in `docs/SPEC.md`. **It is the source of truth.** Before starting any task, read the SPEC section that covers it. If the SPEC and this file disagree, stop and ask.

## How we work (read this every session)

1. **One step at a time.** Do only the task you are given. When its "done when" check passes, stop and summarize: files changed, what you verified, anything you were unsure about. Do not start the next step.
2. **Stay inside scope.** SPEC "Non-goals" are out of scope (UI, agentic router, multimodal parsing, fine-tuning, multi-region, streaming). Do not add features, layers or abstractions the step did not ask for.
3. **Ask, don't guess.** If the SPEC is ambiguous or seems wrong, say so and propose a fix before writing code. Record agreed changes in `docs/DECISIONS.md` (date, decision, why). SPEC edits are fine when they record already-approved decisions and are listed in your report. They never introduce new design.
4. **Explainable code.** Chetan must be able to explain every line in an interview. Prefer plain, explicit code over clever code. Add a short comment only where the *why* is not obvious (RLS gotchas, idempotency, ordering).
5. **New dependencies:** name the package and why before adding it.

## Hard rules (never break these)

**Security**
- The app connects to Postgres only as `app_api` or `app_ingest`. Never as `app_owner` or Neon's default owner role (it may bypass RLS). Only migrations run as `app_owner`.
- `tenant_id` and `role_level` come only from verified token claims (Slice 1: the dev auth stub). Never from request bodies, query strings or S3 keys alone.
- Set request context only with `set_config('app.tenant_id', $1, true)` and `set_config('app.role_level', $2, true)` inside a transaction. The `true` (transaction-local) is required because of Neon's pooled endpoint.
- Every new tenant table gets `ENABLE` + `FORCE ROW LEVEL SECURITY`, policies, grants, and RLS tests in the same change.
- SQL is always parameterized. No string formatting of values into SQL.
- No secrets in code, commits, logs or plaintext Lambda env vars. Local dev uses `.env` (gitignored); AWS uses SSM Parameter Store.

**Ingestion correctness** (SPEC: Ingestion)
- Every job must be safe to deliver twice, safe to crash halfway and safe to arrive out of order: conditional claim with lease, `ON CONFLICT DO NOTHING` chunk inserts, forward-only activation.
- Verify the file's SHA-256 against the stored value before parsing.

**Infrastructure and cost**
- Never run `terraform apply`, `terraform destroy`, or anything that creates, changes or deletes cloud resources without explicit confirmation in that session. `fmt`, `validate` and `plan` are fine.
- Never `git push`. Commits are fine when asked.
- No VPC, no NAT gateway, no RDS, no always-on compute (SPEC: Infrastructure).
- Unit tests never call paid APIs. Mock Bedrock, Cohere and the LLM. Integration tests that call them must be opt-in (marked `@pytest.mark.paid`).
- Cohere trial keys allow about 1,000 calls/month total. The leak matrix runs through `/v1/retrieve` with **reranking turned off**: RLS filters before the reranker, so the reranker cannot affect leaks.
- This machine is an older MacBook Air: no local embedding or reranker models. Use API-based models. For Docker, prefer OrbStack or Colima over Docker Desktop. Build Lambda images for `linux/amd64`.

## Stack and conventions

- **Python 3.12**, managed with `uv`. Lint and format with `ruff`. Type hints on all public functions.
- **FastAPI** + **Pydantic v2** for the API; **Mangum** to run it on Lambda.
- **asyncpg** for Postgres, with `pgvector.asyncpg.register_vector` on pool init. No ORM.
- **Alembic** for migrations, written as raw SQL via `op.execute(...)` so the SQL matches the SPEC exactly.
- **pytest** + **pytest-asyncio**. Unit tests need no network. DB tests run against a **Neon `dev` branch**, never production.
- **Terraform >= 1.10** (S3 native state locking), AWS provider, region `us-east-2` (see DECISIONS, "AWS Region is us-east-2").
- **Embeddings:** Bedrock `amazon.titan-embed-text-v2:0`, 1024 dimensions, auth via IAM (local AWS credentials in dev).
- **PDF parsing:** PyMuPDF only, including its table finder; pinned `<2` because 2.x has no Intel Mac wheel (SPEC: Open decisions #4, DECISIONS 2026-11-17).
- Shared code (the `IngestJob` model, DB pool and RLS context manager, telemetry) lives in `services/shared/`. Don't duplicate it in `api` or `worker`.

## Repository layout

Follow SPEC: Infrastructure, "Repository layout". Create folders when a step needs them, not ahead of time.

## Commands

```bash
uv sync                               # install deps
uv run ruff check . && uv run ruff format .
uv run pytest                         # unit tests (no network)
uv run pytest -m db                   # DB tests against the Neon dev branch
uv run python db/bootstrap.py         # once per dev branch: roles, schema grants, vector extension
uv run alembic upgrade head           # migrations (uses DATABASE_URL_OWNER)
PYTHONPATH=services uv run python -m worker.local <pdf> --tenant <uuid> --doc <uuid> --version 1  # one local ingest (real Bedrock)
terraform -chdir=infra/envs/prod fmt -recursive && terraform -chdir=infra/envs/prod validate
```

## Environment variables (`.env.example`)

```bash
NEON_ADMIN_URL=         # Neon default owner, dev branch direct endpoint; db/bootstrap.py only
NEON_DEV_ENDPOINT_ID=   # e.g. ep-sparkling-heart-bdl5i7aa; bootstrap refuses any other endpoint
DATABASE_URL_OWNER=     # migrations only
DATABASE_URL_API=       # app_api role, pooled endpoint
DATABASE_URL_INGEST=    # app_ingest role, pooled endpoint
AWS_REGION=us-east-2
EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
RAW_BUCKET=
DEV_AUTH_TOKEN=         # Slice 1 only, see below
DEV_TENANT_ID=
DEV_ROLE_LEVEL=3
```

## Auth in Slice 1

Real JWT verification (control-plane JWKS + API Gateway authorizer) arrives in Slice 2. Until then, a FastAPI dependency `get_claims()` accepts `Authorization: Bearer $DEV_AUTH_TOKEN` and returns `DEV_TENANT_ID` / `DEV_ROLE_LEVEL`. Every endpoint must take claims only from `get_claims()`, so swapping in real JWTs later changes one function.

## Build plan: Slice 1 (ingestion)

Work these in order. Each one is a separate session or prompt. Tick them off here as they pass.

- [x] **1. Scaffold.** `uv` project, `ruff` + `pytest` config, `.gitignore`, `.env.example`, folders `services/{api,worker,shared}`, `db/migrations`, `tests/`.
  *Done when:* `uv run pytest` runs (zero tests is fine) and `ruff check` is clean.
- [x] **2. Migration 0001.** Schema, indexes, roles, `app_tenant()` / `app_role_level()`, RLS policies and grants on every tenant table, exactly per SPEC: Data model and RLS.
  *Done when:* `alembic upgrade head` then `alembic downgrade base` both succeed on the Neon dev branch.
- [x] **3. RLS tests.** The 5 tests in SPEC: Proof, "RLS tests", connecting as `app_api` / `app_ingest`.
  *Done when:* `uv run pytest -m db tests/rls` passes, including "no context set returns 0 rows".
- [x] **3b. Close step-2 gaps.** Migrations 0002 and 0003 (column-level INSERT for `app_api` on `documents` and `document_versions`); behaviour tests for every step-2 policy and grant change, each asserting the specific error; "context gone" test pinned to one server session via the direct endpoint.
  *Done when:* `uv run pytest -m db tests/rls` passes.
- [x] **4. Shared DB layer.** asyncpg pool factory and an `rls_transaction(conn, tenant_id, role_level)` context manager (with `statement_timeout`). The `IngestJob` Pydantic model per SPEC: Ingestion.
  *Done when:* unit tests for the model (valid, unknown `schema_version`, bad key) pass, and the RLS tests use the new context manager.
- [x] **5. Worker logic, local.** Plain functions: `claim`, `verify_hash`, `parse`, `chunk`, `embed_batch`, `insert_chunks`, `activate`, `mark_failed`. Entry point `python -m worker.local <pdf> --tenant --doc --version`.
  *Done when:* a sample PDF reaches `indexed` with chunks in the DB.
- [x] **6. Prove the guarantees.** Tests for: run twice = no new work; crash mid-embed then rerun = same chunk count, no duplicates; v2 then v1 = v2 stays active; wrong hash = `failed`.
  *Done when:* all four pass.
- [x] **7. Terraform bootstrap.** `infra/bootstrap`: state bucket, GitHub OIDC provider, deploy role.
  *Done when:* `plan` is clean. Chetan applies it himself.
- [x] **8. Terraform storage + ingest.** S3 bucket, EventBridge rule + input transformer, SQS + DLQ, ECR repo, worker Lambda (image), event source mapping, DLQ alarm, Budget alerts. Thin SQS handler wrapping step 5's functions: permanent errors go straight to the DLQ with a reason, transient errors are raised for SQS to retry (SPEC: Ingestion, "Failure handling"). The worker role needs `sqs:SendMessage` on the DLQ.
  *Done when:* `plan` is clean and matches SPEC queue settings. Chetan applies it.
- [x] **9. API endpoints.** `POST /v1/documents` (register + presigned PUT requiring `x-amz-checksum-sha256`) and `GET /v1/documents/{id}`.
  *Done when:* endpoint tests pass, including the duplicate-upload case, a body carrying another tenant's `tenant_id` (the claims' tenant must be used), and a concurrency test: two simultaneous registrations of new content for the same document both succeed with distinct version numbers (see DECISIONS, "Concurrent version allocation").
- [ ] **10. End to end.** Register, upload with `curl`, watch status reach `indexed`; upload a corrupt PDF and watch it reach the DLQ. Split into 10a to 10e (DECISIONS 2026-11-19, "Step 10 is split").
  *Done when:* every Slice 1 box in SPEC: Slices is ticked.
  - [x] **10a. Production database and first deploy.** Neon's `main` branch becomes production (dev tests stay on the `dev` branch): roles, migrations and role passwords that exist only in SSM (`db/bootstrap.py --production`). Then the first deploy in `infra/envs/prod/README.md`.
    *Done when:* the DB suite passes on the `dev` branch, the prod `plan` shows no changes after Chetan's apply, and the alert email is confirmed.
  - [x] **10b. End-to-end run, by hand.** API served locally (reads its DB URL from SSM). Register, upload with `curl`, wrong checksum rejected by S3, a 20-page PDF reaches `indexed` via `GET`, duplicate upload and duplicate message, corrupt PDF reaches the DLQ and the alarm fires. Commands written up as a runbook.
    *Done when:* those Slice 1 boxes are ticked.
  - [x] **10c. CI.** `ci.yml`: lint, unit tests, DB tests on a Neon branch; migrations on production as `app_owner`.
    *Done when:* a CI run passes and production's migrations are applied by CI.
  - [x] **10d. Deploys.** `deploy.yml`: a push to `main` builds and pushes the worker image and runs `terraform apply` through OIDC.
    *Done when:* a push to `main` deploys.
  - [ ] **10e. Traces.** OpenTelemetry spans for parse, chunk and embed, sent to a free backend (SPEC: Architecture, Telemetry).
    *Done when:* a trace shows those spans with durations, and every Slice 1 box is ticked.

Slices 2 to 4 get their own step list here once Slice 1 is done.

<!-- BEGIN AWS Agent Toolkit rules -->

> **Project override, read first.** This block is AWS's generic guidance for new-experience projects. Where it conflicts with anything above it in this file (especially "Hard rules" and "How we work"), the rules above win. In particular: nothing that creates, changes or deletes AWS resources runs without Chetan's explicit confirmation in that session, whether through Terraform, the AWS CLI or the `aws-mcp` server. The help level is HIGH; LOW and MEDIUM do not apply in this repo. "You CAN update this rule file" covers only the text inside this block. This project's Region is `us-east-2` (see DECISIONS).
>
> **Account:** since 2026-11-19 this project runs in the standard AWS account `905418224080` (`ragdp` profile, `aws login` as IAM user), not a new-experience project (see DECISIONS, "AWS account is 905418224080"). So the new-experience constraints below do not apply to it: the single assigned Region, Free/Paid plan service lists, spend limits, and "project" terminology. `us-east-2` is still the Region by choice. The help-level rules still apply. Before any AWS work, check that `aws sts get-caller-identity --profile ragdp` shows `905418224080`, and never deploy into the new-experience organization's accounts (`056645029578`, `416340982310`, `654740195715`).

# AWS Guidance for the new AWS experience

This user has signed up for the new AWS experience. This experience lets you sign into AWS using a social provider and requires the following additional context.

Where this guidance conflicts with the project's own instructions, the project's instructions take precedence.

## Context

### Terminology:

- Say "project" instead of "account" — a project contains an AWS account and settings for sharing with other collaborators
- Say "team member" instead of "IAM user" — users are invited by email, not created or federated in IAM
- Say "AWS Settings" when referring to management tasks at [settings.aws.com](https://settings.aws.com/) (project management, billing, team members, spend limits). Users view their actual AWS resources in the AWS Management Console.
- Say "selected Region" when referring to the user's Region — not "home Region"
- The user has a managed IAM experience. This includes a managed service control policies (SCP) and resource control policies (RCP) that govern the use of AWS. They will still need to use IAM to create policies to let services work with each other. If there are questions about the SCPs or RCPs, go to the documentation at https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html

### Constraints:

- All projects share a single AWS Region determined by the user's contact address. Resources cannot be created in other Regions
- When developing:
  - MUST create all Regional resources in the project's assigned Region
  - You CAN create AWS WAF and Cloudwatch Logs resources in us-east-1 when there are global resources (like a global WAF instance) that require a connection to dependencies in us-east-1. You should not use these for any other reason, because resources in the selected Region will provide lower cost (due to no cross-Region traffic), increased availability (due to no cross-Region traffic), and easier manageability (due to not needing to look in another Region). When you need to do an inventory of resources, you need to look in both the selected Region and us-east-1 for Cloudwatch Logs or WAF resources.
  - MUST NOT attempt to create Lambda, API Gateway, or other Regional resources in any other Region
  - MUST direct users to confirm their Region in AWS Settings > View all projects > Overview > Additional Info > Region. If the user cannot confirm their Region, check in ~/.aws/config
  - MUST NOT use Lambda@Edge — excluded from both Lambda and CloudFront
  - MUST NOT use CloudFormation StackSets — no multi-account or multi-Region deployments
  - MUST NOT attempt cross-Region actions — no cross-Region replication for DynamoDB/S3/RDS, no multi-Region KMS keys
  - MUST NOT use Route 53 cross-Region routing — geolocation, latency-based, and failover routing policies are not available
  - CloudFront is a global service and its actions ARE allowed in `us-east-1`. A user can create a CloudFront distribution pointing to their project-region Lambda function URL or API Gateway. However, Lambda and API Gateway themselves MUST NOT be created in `us-east-1` — they must be in the project Region.
  - Reduced availability in `eu-north-1` specifically: Amazon Rekognition, Amazon Textract, Amazon Personalize, AWS App Runner are not available in that Region.
- IAM permissions for human access are managed by AWS. Don't assign roles to team members unless absolutely necessary
- The user may have a spend limit if they are on the paid plan. The limit that pauses their project if it's exceeded. If resources suddenly become inaccessible, ask if they have a spend limit configured. Only project owners can modify a spend limit.
- When developing:
  - MUST ask about spend limit status if the user reports sudden "Access Denied" errors on operations that previously worked
  - MUST direct users to check spend status in AWS Settings > Billing
  - MUST check if a user has upgraded their account to the paid plan
  - MUST ask the user if they want to clean up the successfully created resources or keep them to reduce cost
- The user sets up billing, creates spend limits, and retrieves and pays invoices in AWS Settings. The user creates budgets and optimizes their costs in the AWS Billing and Cost Management console
- Not all AWS services are available. If a service isn't working, do the following:
  1. Run the command `aws freetier get-account-plan-state`
  2. If accountPlanType": "FREE", check the [Free Tier supported services list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#supported-services-free-tier) next,
  3. If accountPlanType": "PAID", check the [Paid Tier supported services list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#supported-services-paid-plan).
  4. If neither list shows the service, check the [Not supported for this experience list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#unsupported-services). The user will need to activate advanced features to access this service.
- Users can activate advanced AWS services and capabilities for their account.
- For any AWS task or question — building, configuring, migrating, debugging, or operating a service, writing SDK code, authoring CDK/CloudFormation, or answering how a service works — first call `aws___search_documentation` with topic `agent_skills` and load the matching skill with `aws___retrieve_skill`. Do this even when you believe you know the answer: skills carry current service behavior that your training may not. Prefer the skill's guidance over general knowledge. Skip it only for work that does not involve AWS.

### Help level

**Saved help_level: HIGH** (chosen by Chetan, 2026-11-17).

- help_level (required): LOW, MEDIUM, or HIGH. While a user is building, you MUST ask the user: "How much guidance would you like from me? Low (I only flag security risks), medium (I ask a couple of clarifying questions if something seems off), or high (I explain what I'm doing, suggest alternatives, and flag best practices)."

You CAN update this rule file to save a user's help_level.

Constraints for each level:

**LOW:**

- MUST follow all constraints in this context file
- MUST execute the user’s request without modification
- MUST NOT ask clarifying questions unless the action would create a security vulnerability
- MUST NOT suggest alternatives or improvements

**MEDIUM:**

- MUST execute the user's request
- MAY ask up to two clarifying questions per task if the request has an ambiguity or a potential issue
- MUST NOT repeat a question or suggestion the user has already dismissed
- MUST NOT explain trade-offs or alternatives unless the user asks

**HIGH:**

- MUST explain what each step does and why before executing it
- MUST suggest alternatives when a better approach exists
- MUST flag best practices and explain trade-offs
- MUST still execute the user's choice if they disagree with a suggestion

<!-- END AWS Agent Toolkit rules -->
