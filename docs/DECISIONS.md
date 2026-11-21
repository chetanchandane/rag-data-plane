# Decisions log

Changes to `docs/SPEC.md` that were agreed after it was written. Newest first. Format: date, decision, why.

## 2026-11-21: Step 13 API on Lambda

- **Routes need scopes, checked by API Gateway before the Lambda runs** (Chetan, 2026-11-21). `POST /v1/documents` needs `documents:write` and `GET /v1/documents/{id}` needs `documents:read`. A token passes if its `scope` claim includes the route's scope.
  - **Why:** scopes are coarse API permissions at the edge. Which rows a caller sees is still decided by `role_level` and RLS. Adding scopes later would break every client token.
  - **Locally (`api.local`) scopes aren't checked:** the API's own verifier checks the token, not routes. Mint with `--scope documents:read --scope documents:write` for the deployed API.
- **`GET /v1/whoami`** returns `{tenant_id, role_level}` from `get_claims()`. It needs a valid token but no scope and no database. It proves the token path end to end, and lets a client check a token. It stays after Slice 2.
- **The HTTP API has only explicit routes,** so an unknown path gets a 404 from API Gateway and never invokes the Lambda. Every route uses the JWT authorizer, whose issuer is `module.issuer`'s URL and whose audience is `rag-data-plane`. There's no CORS (no UI) and no custom domain.
- **The stage throttles all routes to 10 requests/s, burst 20** (SPEC: Auth at the edge).
- **Access logs are one JSON line per request** in `/aws/apigateway/ragdp-api`, kept 14 days. They include `authorizerError` and `integrationStatus`: an empty `integrationStatus` means the Lambda wasn't invoked. They also include the claims `sub`, `tenant_id` and `jti`, never the token.
- **Only this API may invoke the function** (a resource policy on its execution ARN). That's what makes trusting the event's claims safe (step 12).
- **The Lambda has 1024 MB and a 15 s timeout, with no reserved concurrency** (Chetan, 2026-11-21).
  - **Memory:** more memory means more CPU, so a faster cold start, for pennies at this traffic.
  - **Timeout:** 15 s covers today's routes; step 15 may raise it for the LLM, up to API Gateway's 30 s.
  - **Concurrency:** the account limit of 10 allows no reservation (step 8); the stage throttle caps the rate instead.
- **Mangum 0.22 is added as a runtime dependency** (Chetan, 2026-11-21). It puts the event in the ASGI scope as `aws.event`, which `get_claims()` reads.
  - **Its lifespan support is off.** Mangum runs the app's lifespan around every invocation (`adapter.py`), which would open and close the DB pool per request.
  - **Instead, `api/handler.py` opens the resources once per container,** on the first invocation, on one event loop it owns. If opening fails, the invocation fails, and the next one tries again.
  - **Rejected:** AWS Lambda Web Adapter. It passes the request context in a header, not the scope, so step 12's `get_claims()` would have to change.
- **The DB pool is reused across warm invocations, as the SPEC says, and fails fast** (Chetan, 2026-11-21). The worker opens a fresh pool per invocation instead (step 8).
  - **Why reuse:** it saves a TLS and auth handshake on every warm request, which matters for `/v1/retrieve`'s 400 ms p95.
  - **The risk:** a connection can die while Lambda freezes the container.
  - **After an idle gap of more than 60 s, the handler calls `pool.expire_connections()`** before the request, so every connection is replaced on its next acquire. This doesn't rely on asyncpg's idle timer, which can't run while the container is frozen.
  - **A backstop:** the API pool has a client-side `command_timeout` of 3 s (above the 2 s `statement_timeout`) and a 10 s connect timeout.
  - **Connection failures and timeouts get a 503 with `Retry-After: 1`,** logged with the error type: `PostgresConnectionError`, `CannotConnectNowError`, `TooManyConnectionsError`, `QueryCanceledError`, `TimeoutError` and `ConnectionError`. Everything else stays a 500. The rest of SPEC's timeouts and fallbacks are Slice 3.
- **The deploy role gains HTTP APIs** in bootstrap, which Chetan applies.
  - **Creating an API** (`apigateway:POST` on `/apis`) requires the request tag `Project = ragdp`.
  - **Changing an API** and its stages, routes, integrations, authorizers and tags is allowed on any HTTP API in us-east-2. AWS's IAM reference doesn't confirm that those sub-resources carry the API's tags, and this account runs only this project.
  - **Logs:** the role gets the `/aws/apigateway/ragdp-*` log groups, plus the log-delivery actions AWS lists for turning on access logging, which can't be scoped to a resource.
  - **Tagging is also authorized as `apigateway:TagResource` and `apigateway:UntagResource`.** The first deploy was refused `apigateway:TagResource` when creating the tagged stage, although AWS's IAM reference maps tagging to the HTTP verbs. Both are allowed on the same resources as the verbs.
- **`deploy.yml` builds and pushes both images** with the same SHA tag, and checks that both functions run it.
  - **First deploy:** a targeted apply creates the `ragdp-api` ECR repository before the first push (`infra/envs/prod/README.md`).
  - **Image size:** the API image still installs the worker-only PyMuPDF, because the runtime dependencies are one list. It's left as is: image Lambdas load lazily, so size matters little for cold starts.

## 2026-11-21: Step 12 claims in the API

- **On Lambda, the API trusts the claims API Gateway's JWT authorizer verified** (Chetan, 2026-11-21). It doesn't verify the token again.
  - **What API Gateway checks before the function runs** (AWS docs): the signature against the issuer's JWKS (RSA only), `kid`, `iss`, `aud`, `exp`, `nbf` and `iat`. It then passes the claims to the integration at `requestContext.authorizer.jwt.claims` in the payload format 2.0 event. Mangum puts that event in the ASGI scope as `aws.event`.
  - **Why it's safe:** only API Gateway can invoke the function, through the function's resource policy (step 13). Anyone who could invoke it directly can already change it.
  - **Trade-off:** a retired key is refused only once API Gateway's JWKS cache refreshes (up to 2 hours). A second check in the Lambda would use the same published JWKS, so it would only shorten that window to its own cache's lifetime. For a leak, the runbook's CloudFront invalidation does more.
- **Where the claims come from is decided per request, not by a setting.** A request with `aws.event` in its scope came through Lambda: its authorizer claims are used, and a request without them gets a 401, even with a valid bearer token. Any other request (`api.local`, tests) is verified by the API itself. The ASGI scope is built by the server, so a client can't add `aws.event`.
  - **Rejected:** an `AUTH_MODE` setting. It's one more thing to misconfigure, and the wrong value would either trust nothing or verify nothing.
- **Without API Gateway in front, the API verifies tokens with PyJWT** (`api/token_verifier.py`), making the same checks. RS256 only, so `alg: none` and HS256 are refused. It also requires `exp`, `iat`, `iss`, `aud` and `sub`.
  - **The JWKS is found the way API Gateway finds it:** through the issuer's discovery document, fetched once at startup. So a wrong `JWT_ISSUER` fails at startup.
  - **Keys are cached by PyJWT's `PyJWKClient`.** It caches the JWKS for 5 minutes and fetches it again for an unknown `kid`, at most once every 30 seconds. So junk `kid`s can't make it fetch on every request.
- **On both paths the claims are then checked:** `tenant_id` must be a UUID and `role_level` 1, 2 or 3. API Gateway may pass every claim value as a string (its docs don't say), so the strings `"1"` to `"3"` are accepted too. `true`, `2.0` and `"02"` aren't. Step 13 confirms the real event's shape.
- **Every failure is the same 401** (`invalid or missing bearer token`, `WWW-Authenticate: Bearer`). The reason goes only to the server log.
- **`pyjwt[crypto]` moves to the dev dependencies.** Only `api/token_verifier.py` (imported only when `JWT_ISSUER` is set), `shared/tokens.py` and `scripts/issuer.py` import it, and none of them run on Lambda. The Lambda images lose PyJWT, cryptography, cffi and pycparser. A unit test checks that importing the API and the worker loads neither `jwt` nor `cryptography`.
- **The dev auth stub is gone,** with `DEV_AUTH_TOKEN`, `DEV_TENANT_ID` and `DEV_ROLE_LEVEL` (it replaces the 2026-11-16 "Dev auth stub" entry). Local runs use `scripts/issuer.py mint` tokens.

## 2026-11-21: Step 11 token issuer hosting and keys

- **Hosting: CloudFront in front of a private S3 bucket** (approved 2026-11-21). The bucket keeps every Block Public Access setting on, and only this distribution can read it, through origin access control (OAC).
  - **The bucket policy** lets the CloudFront service principal `GetObject` exactly two keys (`.well-known/openid-configuration` and `.well-known/jwks.json`), on condition that `AWS:SourceArn` is this distribution. Like the other buckets, it also refuses any request that isn't over TLS.
  - **Bucket encryption is SSE-S3** (AES256), not KMS: the objects are public keys, and KMS would cost money and need its own key policy for CloudFront.
  - **The distribution** is HTTPS only, GET and HEAD only, `PriceClass_100`, and uses the default `*.cloudfront.net` certificate.
  - **Caching uses the managed CachingOptimized policy.** It honors the origin's `Cache-Control`, so both objects carry `public, max-age=300` (and `Content-Type: application/json`). A new key reaches CloudFront within 5 minutes without an invalidation.
  - **Why:** no public bucket in the account, so account-wide Block Public Access can be turned on later.
  - **Rejected:** public-read S3 objects (a public bucket, flagged by every security scan), serving the documents from the HTTP API itself (the authorizer would depend on the API it protects), and GitHub Pages (outside AWS and Terraform).
- **The issuer URL is `https://<distribution domain>`, with no trailing slash.** Terraform builds the discovery document from the distribution's `domain_name`.
  - **The discovery document is partial on purpose:** `issuer` and `jwks_uri` only. This issuer has no authorization endpoint, and verifiers need nothing else.
  - **The JWKS is committed at `infra/envs/prod/jwks.json`.** Terraform checks that every key is a public RSA signing key with a `kid`.
- **The deploy role gains CloudFront** in bootstrap, which Chetan applies:
  - **Creating a distribution** requires the request tag `Project = ragdp`; `default_tags` sets it.
  - **Reading, updating, tagging and deleting** are allowed only on distributions tagged `Project = ragdp`.
  - **Origin access controls** can't be tagged or named in IAM, so the role manages any in the account.
  - **The cache policy lookup** is read-only, on `*`.
  - **S3:** `s3:*` on `ragdp-issuer-*` buckets, like the raw bucket.
- **Signing keys live under `/ragdp/issuer/`, not `/ragdp/prod/`.** This changes the issuer entry below.
  - **Layout:** each private key is a SecureString at `/ragdp/issuer/signing_keys/<kid>`, and the String `/ragdp/issuer/active_kid` names the one that signs.
  - **Why:** the workload permissions boundary allows reading `/ragdp/prod/*`, so a Lambda role the deploy role writes could have been granted the key. Nothing in bootstrap reaches `/ragdp/issuer/`, so only a human sign-in can mint.
- **The `kid` is the key's RFC 7638 thumbprint** (SHA-256 of its `e`, `kty` and `n`), so a `kid` can't be paired with the wrong key. Keys are RSA 2048.
- **One script, `scripts/issuer.py`,** with the subcommands `create-key`, `activate-key`, `retire-key` and `mint` (`mint --check` verifies the new token against the published JWKS). It replaces the planned `issuer_keys.py` and `mint_token.py`, which would have shared most of their code.
  - **A private key never touches disk, stdout or Terraform state:** it goes from memory straight to SSM.
  - **Every subcommand checks** that the AWS account is 905418224080.
- **Tokens carry the OAuth `scope` claim** (space-separated), not SPEC's `scopes`, because API Gateway's authorizer reads `scope` or `scp`. They also carry a `jti` for tracing a token in logs. There's no `nbf`, since it adds nothing to `iat` here. Scope names are settled with the routes in step 13.

## 2026-11-21: Token issuer for Slice 2 (Open decisions #1 and #2)

- **Decision #1** (approved 2026-11-21): rag-control-plane doesn't exist yet, so this repo builds a minimal stand-in issuer for the data plane to trust:
  - **RS256** with one RSA key pair. The private key is an SSM SecureString (under `/ragdp/issuer/`, see the step 11 entry above); the public key is published as a JWKS with a `kid`.
  - An **OIDC discovery document** (`/.well-known/openid-configuration`) and the **JWKS** it points to, served publicly over HTTPS.
  - **`scripts/mint_token.py`** signs tokens with `iss`, `aud` = `rag-data-plane`, `sub`, `tenant_id`, `role_level`, `scope`, `iat` and `exp` (the claim is `scope`, see the step 11 entry above). Reading the private key needs Chetan's MFA sign-in; no CI role can read it.
  - **The issuer URL and audience are Terraform variables.** A real control plane, Cognito or Auth0 later replaces the stand-in without a code change.
- **Why:**
  - SPEC's boundary is that the control plane decides who may do what, and the data plane only verifies the claims it's handed. What Slice 2 needs is a real issuer to verify against, not a login system.
  - API Gateway's JWT authorizer does the real verification (signature, `iss`, `aud`, `exp`), so the production path is exercised, not mocked.
  - It costs nothing to run: a few public objects and no compute.
- **Decision #2:** access tokens live **15 minutes**. There is no revocation, so the expiry is the limit on how long a leaked token works.
- **Trade-offs, accepted:**
  - Whoever can read the signing key can mint a token for any tenant. That's what an issuer is; today it's only Chetan.
  - Key rotation is by hand, with a runbook: publish the new key beside the old one, switch the signer, and remove the old key only after the last old token has expired *and* API Gateway's JWKS cache (up to 2 hours) has refreshed.
- **Rejected:**
  - **Cognito user pool.** User management is control-plane work. Custom claims in *access* tokens need a pre-token-generation Lambda and a paid feature tier, and machine-to-machine tokens are billed.
  - **Auth0 or Clerk.** Same trade as Cognito, plus an outside account. No gain here.
  - **A Lambda authorizer with HS256.** A shared secret and a Lambda in front of every request; the reason #1 leaned to RS256.
  - **Keeping the dev stub.** It fails the Slice 2 box "API Gateway JWT authorizer verifies control-plane tokens via JWKS".
- **Checked in AWS's HTTP API JWT authorizer docs (2026-11-21):**
  - Only RSA-based algorithms are supported.
  - The token's `kid` header must match a key in the issuer's `jwks_uri`.
  - `iss` must equal the configured issuer. `aud` must match one of the configured audiences, and `client_id` is checked only when there is no `aud`.
  - `exp` must be in the future; `nbf` and `iat` must be in the past.
  - A route's `authorizationScopes` are matched against the `scope` or `scp` claim.
  - The public key is cached for up to two hours.
  - **Not stated:** that the `jwks_uri` is found through `{issuer}/.well-known/openid-configuration`. Publishing the discovery document covers that path, and the step that deploys the authorizer proves it with a real token.
- **Dependency:** `PyJWT[crypto]` (approved 2026-11-21), which pulls in `cryptography`. It is a runtime dependency, not dev-only: the API verifies tokens itself when it runs locally without API Gateway. It also signs tokens in the minting script and in tests, which use a throwaway key and need no network.
  - **`cryptography` 50.0.2 is built from source on Intel Macs** (approved 2026-11-21). From version 49 it ships no macOS x86_64 wheel, so `uv` compiles it locally (about 3 minutes, cached until the next upgrade). The build backend downloaded a Rust toolchain into `~/Library/Caches/puccinialin`, and the result links Homebrew's `openssl@3`. CI and Lambda (Linux x86_64) install the published wheel.
    - **Why:** the same version runs on the laptop and in production. Pinning `<49` only on Intel Macs was rejected because local and production would then run different versions of a security library. Pinning `<49` everywhere was rejected because production would miss security fixes.

## 2026-11-19: Step 10 is split into 10a to 10e

- **Decision** (approved 2026-11-19): step 10 becomes five steps, each with its own "done when" and commit: 10a production database and first deploy, 10b end-to-end run by hand, 10c CI, 10d deploys, 10e traces. Listed in `CLAUDE.md`.
- **Why:** step 10's "done when" is every Slice 1 box, and three of them need work no earlier step built: a deploy workflow ("a push to `main` deploys through OIDC"), CI migrations ("migrated on Neon by CI") and telemetry ("traces show parse, chunk and embed spans"). Moving them to Slice 2 was considered and rejected: they are what makes Slice 1 worth posting.
- **Checked while planning:** whether the endpoint used as "the dev branch" since 2026-11-17 was really `main`'s. It wasn't. The project has a `dev` child branch (`ep-sparkling-heart-bdl5i7aa`), and `main` has its own endpoint (`ep-wandering-math-bdgo4xwq`). Every DB test ran on `dev`.

## 2026-11-21: Step 10e traces

- **Backend: Honeycomb, US region** (approved 2026-11-21; SPEC allowed Grafana Cloud or Honeycomb). OTLP/HTTP to `https://api.honeycomb.io/v1/traces` with the `x-honeycomb-team` header. The dataset is the service name: `ragdp-worker` for the Lambda, `ragdp-worker-local` for `worker.local`, so dev runs never mix with production.
- **Dependencies** (approved 2026-11-21): `opentelemetry-sdk` (provider, batch processor, in-memory exporter for tests) and `opentelemetry-exporter-otlp-proto-http`. No auto-instrumentation packages: the manual spans are what SPEC asks for, and every span can be explained.
- **One trace per SQS message.** The handler's `ingest` span (message id, receive count, job id, outcome, DLQ reason) has the pipeline stages as children: `claim`, `load_and_verify`, `parse`, `chunk`, `embed` (with an `embed_batch` and an `insert_chunks` per batch), `activate`. Parse and chunk were timed together before; they're separate stages now, in `IngestResult.seconds` too.
- **Attributes are ids, counts and outcomes, never document text.** A DB test checks that no attribute contains the document's text.
- **The key is an SSM SecureString,** `/ragdp/prod/honeycomb_api_key`, stored out of band like the DB URLs. The Lambda gets only its name (`HONEYCOMB_API_KEY_PARAM`) and the endpoint; the worker role may read that one parameter.
- **Tracing never breaks ingestion.** No key, an unreadable key or any setup error leaves tracing off with a log line. The handler flushes spans in `finally` with a 3-second limit, because Lambda freezes the container as soon as it returns; export errors are logged and dropped.

## 2026-11-20: Step 10d deploys

- **A push to `main` deploys with no approval** (approved 2026-11-20). `ci.yml` calls `deploy.yml` after `migrate`, so new code never meets an old schema. The `prod` environment's required reviewer is removed.
  - **Why:** every change has already passed lint, unit tests, the migration round trip and the DB tests, and only Chetan pushes to `main`. Keeping the reviewer would have meant two approvals per push (migrate and deploy are separate jobs with separate roles); merging them into one job to need one approval would put both roles' credentials in one job.
  - **The guard instead:** the deploy fails if the Terraform plan deletes or replaces any resource. Those are applied by hand after review (`infra/envs/prod/README.md`).
- **`prod` accepts only `main`** (deployment branch policy, set 2026-11-20). It had no branch restriction, and any job naming `prod` gets the `sub` both roles trust, so until now only the reviewer kept other branches out. The step 7 entry's "branches can't assume it" and step 10c's comments were wrong about this; the bootstrap comment is corrected.
- **The image is tagged with the full commit SHA** and built on every push to `main`. ECR tags are immutable, so a rerun finds the image and skips the push. The lifecycle policy keeps the last 3 images, which is the rollback window.
- **The plan that's applied is the one that was checked** (`plan -out`, then `apply tfplan`). `init -lockfile=readonly` uses the committed provider hashes; the lock file now has `linux_amd64` hashes too.
- **The deploy role gains tag permissions on event source mappings** (`lambda:ListTags`, `TagResource`, `UntagResource` on `event-source-mapping:*`). Found before the first run: CloudTrail showed Terraform calling `ListTags` with the mapping's ARN, and the IAM policy simulator showed it denied. Mappings have random ids and no condition key naming their function, so the resource can't be narrower. (The simulator also denied `sns:GetSubscriptionAttributes`, but it denies that even for `sns:*` on `*`; AWS's service reference authorizes it against the topic, which the role already covers.)
- **`alert_email` is the `prod` environment secret `ALERT_EMAIL`,** passed as `TF_VAR_alert_email`, so it stays out of the repo and is masked in logs.
  - **Passed by name from `ci.yml`** (fix after the first deploy run, 2026-11-21). A reusable workflow gets an environment secret only if the caller passes it; otherwise it's an empty string with no error. Run 44907641677 got an empty email, planned to replace the alarm subscription (the guard stopped it), and printed the old address unmasked in its log. `deploy.yml` now declares the secret `required` and fails first if it's empty. Not `secrets: inherit`, which would also give the deploy job the Neon key and dev passwords.

## 2026-11-20: Step 10c CI

- **DB tests run on a throwaway Neon branch per CI run** (approved 2026-11-20), created from `dev` by `neondatabase/create-branch-action` and deleted at the end. It also expires after 2 hours, in case the delete never runs.
  - **Why:** runs can't interfere with each other, and a pull request's new migration is tested on a copy instead of changing shared `dev` before review.
  - The copy inherits `dev`'s roles and passwords. `app_owner`'s password comes from the action's output; `app_api`'s and `app_ingest`'s are GitHub secrets. Plus `NEON_API_KEY` (secret) and `NEON_PROJECT_ID` (variable). Setup: `docs/runbooks/ci-setup.md`.
  - The job runs the migrations as a round trip (upgrade, downgrade to base, upgrade), then `pytest -m "db and not paid"`.
- **Production migrations run in a `migrate` job on pushes to `main`,** in the `prod` environment, after lint, unit and DB tests pass. Never two at once (`concurrency: migrate-prod`). 10d's deploy job will run after it, so the schema is ready before new code.
- **The migrate job reads the owner URL from SSM with a new role, `ragdp-github-migrate`** (approved 2026-11-20). Its only permissions: `ssm:GetParameter` on `/ragdp/prod/database_url_owner`, and `kms:Decrypt` only via SSM for that parameter's encryption context.
  - **Why:** SSM stays the only copy, so `bootstrap.py --production` rotations need no GitHub change, and no DB credential is stored in GitHub. The deploy role keeps no SSM access.
  - **Limit, stated plainly:** both roles trust the same `sub` (`environment:prod`), so a job in that environment could assume either. This separates credentials per job; it isn't a hard boundary. Telling them apart would need a customized `sub` claim, which isn't worth it for one repo.
  - The role is at IAM path `/`, so the deploy role (which manages only `/ragdp/`) can't edit it.
- **OIDC trust uses GitHub's immutable subject** (found in the first CI run, 2026-11-20). This repo's tokens carry `sub` = `repo:chetanchandane@75742100/rag-data-plane@1464421272:environment:prod` (numeric owner and repo IDs; `gh api repos/chetanchandane/rag-data-plane/actions/oidc/customization/sub` shows `use_immutable_subject: true`). Both roles trusted the name-only form from step 7, so `migrate` got AccessDenied (the `sub` read from CloudTrail). Bootstrap now takes `github_subject_prefix` and both roles trust the immutable form. This replaces the `sub` in the step 7 entry below.
  - **Why not switch the repo back to name-only subjects:** the IDs never change, so a repo deleted and recreated under the same name, or a renamed and re-registered account, can't assume the roles. The stricter option costs one variable.
- **Third-party actions are pinned by commit SHA** with the tag in a comment, because a tag can be moved. The workflow defaults to `permissions: contents: read`; only `migrate` gets `id-token: write`.
- **CI's URLs use Ubuntu's CA bundle** (`/etc/ssl/certs/ca-certificates.crt`); the stored URLs name `/etc/ssl/cert.pem`, which Ubuntu lacks.
- **The workflow is linted with `actionlint`** (a Homebrew tool, not a project dependency).

## 2026-11-19: Step 10b end-to-end run

- **The API runs on the laptop for Slice 1** (`python -m api.local`), on `127.0.0.1` only, because the auth stub is one shared token. `uvicorn` is a dev dependency for it; the API Lambda (Mangum) comes in Slice 2.
- **The API's DB URL comes from exactly one source:** `DATABASE_URL_API` (a URL, for dev) or `DATABASE_URL_API_PARAM` (an SSM name, for production; read once at startup, kept in memory). Both or neither stops startup. `api.local`'s `--database-url-param` and `--bucket` flags override `.env`, so a production run can't fall back to the dev branch.
- **Presigned URLs are signed with the `aws login` session,** so they also stop working when that session ends.
- **The run is a runbook,** `docs/runbooks/slice1-e2e.md`, with its results recorded under "Results". First run 2026-11-19 on image `ef63fa3`: all four boxes passed, and the 20-page PDF was `indexed` 16 s after upload.
- **The 20-page PDF is the first 20 pages of NIST SP 800-207** (`samples/README.md` has the command), so no new download or license question.
- **The run's data stays in production** (one test tenant, two documents, their S3 objects) until there's a delete path (Slice 2). The DLQ is purged after the alarm is seen, so the alarm can fire again.

## 2026-11-19: Step 10a production database and first deploy

- **Production is Neon's default branch `main`** (endpoint `ep-wandering-math-bdgo4xwq`); dev tests stay on the `dev` branch.
- **`db/bootstrap.py --production --aws-account-id <id>`** sets up production. It:
  - takes the owner URL at a hidden prompt, never from `.env`;
  - refuses the dev endpoint (the mirror of the dev-mode guard) and any other AWS account, then asks for the endpoint ID to be typed before changing anything;
  - generates new passwords on every run (so a re-run is a rotation), and stores the three role URLs as SecureStrings in SSM before setting them on the roles, so a password is never set without being stored;
  - runs `alembic upgrade head` with the owner URL set only in its own process.
  - **Why:** the hard rule says production passwords live only in SSM. This keeps them out of `.env`, shell history, logs and Terraform state.
- **SSM names:** `/ragdp/prod/database_url_{owner,api,ingest}`. All three names are defined in `infra/modules/secrets`, and a unit test checks that the script uses the same names. The owner URL is there for migrations; 10c decides how CI reads it.
- **The worker image is built with `--provenance=false`.** Without it, buildx wraps the image in an index with an attestation manifest, which Lambda refuses to deploy. In ECR the image is a single Docker v2 manifest.
- **The Lambda base image (Amazon Linux 2023) has `/etc/ssl/cert.pem`,** so production URLs use the same `sslrootcert` as dev (checked with `docker run`).
- **First deploy done 2026-11-19,** in the order in `infra/envs/prod/README.md`: production bootstrap (migrations at 0003), the ECR repository alone, the image `ef63fa3` pushed, the full apply, the SNS email confirmed. The plan afterwards showed no changes.

## 2026-11-19: Step 9 registration endpoint choices

- **A duplicate whose version is still `pending_upload` gets a fresh upload URL** (approved 2026-11-19; SPEC upload flow updated). Any other status returns the existing version with `upload: null`.
  - **Why:** a client that crashed or let its URL expire would otherwise have a version that can never be uploaded. Safe: the new URL is for the same key and requires the same checksum and size.
  - Status codes: 201 for a new version, 200 for a duplicate (`"duplicate": true`).
- **Versions are allocated under a row lock** (`SELECT ... FROM documents ... FOR UPDATE`), the second option in "Concurrent version allocation". The lock is held until COMMIT, so concurrent registrations for one document queue up instead of retrying. The presigned URL is signed after COMMIT to keep the lock short.
  - Verified: with the lock removed, the 4-way concurrency test fails with `UniqueViolationError` on `document_versions_pkey`.
- **One 409 for "hidden" and "different level"** (approved 2026-11-19). An `external_id` whose document is above the caller's role (RLS hides it) and one whose visible document has a different `min_role_level` get the same 409 body, so the caller learns neither whether a hidden document exists at a higher level nor what that level is.
  - It does reveal that the `external_id` is taken. Avoiding that would need a `SECURITY DEFINER` function, which is more surface than this is worth.
  - The level can't change through the API (column grant): chunks carry a copy of it.
- **`min_role_level` above the caller's role is a 403** before any query, instead of RLS's WITH CHECK error surfacing as a 500.
- **Upload limits** (approved 2026-11-19): at most 50 MiB (`MAX_UPLOAD_BYTES`), URLs valid for 15 minutes. The presigned PUT signs `x-amz-checksum-sha256` and `content-length`, so S3 rejects other bytes and other sizes.
  - URLs use the Regional virtual-hosted endpoint (`<bucket>.s3.us-east-2.amazonaws.com`); the global endpoint can redirect for a new bucket, and an upload doesn't follow redirects.
- **Request body:** unknown fields (such as `tenant_id` or `role_level`) are ignored, never read; tenant and role come only from `get_claims()`. Fields are strict (no `"2"` to 2, no `true` to 1). The hash is hex and lowercased, so one file has one spelling under `UNIQUE (doc_id, sha256)`.
- **`filename` becomes the document's `title`** at creation. Later versions don't change it.
- **`GET /v1/documents/{id}`** returns the document and every version, newest first (status, attempts, last error, chunk count). A document that's unknown, deleted, in another tenant or above the caller's role gets the same 404.
- **Dependencies:** `fastapi` (runtime) and `httpx` (dev: async test client). Mangum and a local server come when the API runs somewhere (step 10 and Slice 2).
- **Not handled yet:** a DB timeout returns 500, not the SPEC's 503 with `Retry-After` (Slice 3). Re-registering an older file's bytes returns that old (superseded) version, so the API can't roll a document back to earlier content; that belongs with Open decision #8 (reindex).

## 2026-11-19: Step 8 ingest infrastructure choices

- **Worker concurrency is capped by the SQS trigger, not reserved concurrency** (approved 2026-11-19; SPEC queue table updated). The worker never reserves concurrency, so it can't lock other functions out of the account's limit during test runs.
  - **Why:** the account's Lambda concurrency limit is 10, and AWS keeps at least 10 unreserved, so no reservation is possible. `scaling_config.maximum_concurrency = 2` on the event source mapping caps the worker at 2. That still protects Neon and Bedrock. It's also safer than a reservation: the trigger stops polling instead of being throttled, so waiting messages don't use up their 3 receives.
  - 2 is the lowest value AWS allows for `maximum_concurrency`, so ingest never uses more than 2 of the account's 10 concurrent executions. That leaves 8 for the API and test runs. The worker's Terraform has no reserved-concurrency setting at all.
  - Load tests (Slice 4) will need more than 10. Ask AWS to raise the limit (Service Quotas, Lambda "Concurrent executions") before then.
- **Secrets are names only.** `modules/secrets` defines the parameter names and ARNs. The SecureString parameters and their values are created out of band with the CLI (`infra/envs/prod/README.md`). No secret passes through Terraform or lands in its state, and the deploy role has no SSM access at all.
- **The raw bucket is `ragdp-raw-prod-905418224080`,** not the SPEC example's `ragdp-raw-prod`, because bucket names are global.
- **SQS handler (`services/worker/handler.py`):**
  - One record per invocation. Permanent errors go to the DLQ with a `reason` message attribute, then return normally; anything else raises.
  - An invalid message's reason says where validation failed, never the input.
  - A job naming another bucket is permanent (`wrong_bucket`). `reindex` and `delete` jobs go to the DLQ as `unsupported_job_type` until Slice 2 adds the endpoints that produce them.
  - The DB URL is read from SSM once per cold start. A fresh asyncpg pool is created per invocation, because connections held while Lambda freezes the container may be dead when it thaws.
- **The worker image** is built on `public.ecr.aws/lambda/python:3.12` and installs runtime dependencies from `uv.lock` with `--require-hashes`. The ECR repository has immutable tags (the git SHA), scans on push and keeps the last 3 images.
- **EventBridge rule:** `Object Created` in the raw bucket with key prefix `raw/`, then the SPEC's input transformer verbatim, then SQS. The queue policy accepts messages only from that rule (`aws:SourceArn`).
- **Observability for Slice 1:** the DLQ alarm (any message, 1-minute period) to the SNS topic `ragdp-alerts` (email), plus the monthly budget `ragdp-monthly` ($30 limit, actual-spend alerts at $15 and $25). The alert email is kept out of the repo (`TF_VAR_alert_email`). The API alarms come in Slice 2.
- **Deferred: lifecycle for superseded objects** (SPEC storage module; decided 2026-11-19). S3 lifecycle rules can't see which versions are superseded; it would need the worker to tag those objects. It isn't needed yet: the old PDFs cost almost nothing at this scale. Add it if storage cost or retention ever calls for it. Only an abort-incomplete-uploads rule exists for now.

## 2026-11-19: Step 7 bootstrap choices

- **The deploy role is `ragdp-github-deploy`.** It trusts `repo:chetanchandane/rag-data-plane:environment:prod` exactly (SPEC: "Deploy identity"), with no wildcard, so branches, pull requests and forks can't assume it. Sessions last at most 1 hour.
- **Its permissions cover Slice 1's modules only:** storage, ingest, secrets, observability. Each statement is scoped to `ragdp-*` names in `905418224080` and `us-east-2`; only list and describe calls that can't be resource-scoped use `*`. API Gateway is added when the Slice 2 API module exists. Step 8's first real `plan`/`apply` is where any missing permission shows up.
- **Workload roles get a permissions boundary.** The deploy role can create Lambda roles only under the IAM path `/ragdp/`, and only with `ragdp-workload-boundary` attached. It can pass them only to Lambda.
  - **Why:** a CI role that can create IAM roles could otherwise create one more powerful than itself (privilege escalation).
  - The boundary is the most a workload role can ever do: its own logs, `ragdp-*` queues, raw-bucket objects, `/ragdp/prod/*` parameters (and decrypting them via SSM only), and Bedrock `InvokeModel`. Only bootstrap can change it.
- **State bucket `ragdp-tfstate-905418224080`:** versioned (old versions expire after 90 days), SSE-S3, public access blocked, ACLs off, TLS-only policy, `prevent_destroy`. The deploy role can reach only `envs/prod/terraform.tfstate` and its `.tflock` file.
- **Bootstrap's own state** starts local, because the bucket doesn't exist yet. After the first apply it moves to `bootstrap/terraform.tfstate` in the bucket with `init -migrate-state` (steps in `infra/bootstrap/README.md`).
- **`allowed_account_ids = ["905418224080"]`** in the provider makes Terraform refuse to run against any other account.

## 2026-11-19: AWS account is 905418224080

- **Decision:** all AWS work (Bedrock now; S3, Lambda, SQS, ECR and Terraform in steps 7 and 8) goes in the standard AWS account `905418224080`, Region `us-east-2`. The `ragdp` CLI profile signs in to it with `aws login` as the IAM user, not a static access key. `.env` and the code are unchanged (`AWS_PROFILE=ragdp`).
- **Why:** the new-experience project `654740195715` has Bedrock authorized but 92 of 93 per-minute Bedrock quotas set to 0, so every `InvokeModel` call is throttled (checked 2026-11-18, after the Paid upgrade and activating advanced features). The quotas aren't adjustable in Service Quotas, and no fix date is known. `905418224080` has the default Titan v2 quotas (6,000 requests and 300,000 tokens per minute). Keeping everything in one account avoids cross-account roles. Switching to a non-Bedrock embedding API was considered and rejected: it changes the SPEC's design for a temporary account problem.
- **Region stays `us-east-2`.** On this account it's a choice, not an assigned Region; it keeps the AWS services next to the Neon project, which is already in AWS `us-east-2`.
- **Not used:** the organization created by activating advanced features (`056645029578` management, `416340982310` Identity Delegated Admin, `654740195715` Proof of Concept). Nothing of this project runs there.

## 2026-11-17: Step 5 worker choices

- **PDF parsing is PyMuPDF only** (Open decision #4). Tables come from PyMuPDF's own table finder (ported from pdfplumber's algorithm), not pdfplumber.
  - **Why:** one parser and one pass; table positions share the text's coordinates, so table text is skipped from the prose and emitted once as markdown; one fewer dependency in the Lambda image.
  - **Pinned `pymupdf>=1.28.2,<2`:** 2.x ships no macOS x86_64 wheel and builds MuPDF from source on this machine. Linux wheels exist for both, so revisit when the dev machine changes.
  - **Markdown tables are built from the raw cells**, not `to_markdown()`, which copies merged cells into every column they span. Columns empty in every row are dropped.
  - **Cleaned out before chunking:** running headers, footers and page numbers (margin lines repeated on at least half the pages), rotated text (watermarks), and table-of-contents lines (dot leaders followed by a page number).
  - **Heading paths come from the PDF's bookmarks**, which carry each heading's position on its page. No bookmarks means an empty path; no font-size guessing.
- **Token counts are estimated as characters / 4.** Titan v2's tokenizer isn't published, so any count is an estimate; this one needs no dependency. It only affects chunk sizes.
- **Chunking rules (`CHUNKER_VERSION = headings-500-800-v1`):** at most 800 tokens; at a heading, close a chunk that has at least 500; never cross a top-level heading; long paragraphs split at sentence ends, long tables by rows with the header repeated. Deterministic, because resume matches chunks by `chunk_index`.
- **The claim's `attempts` is a fencing token.** `start_embedding` and `mark_failed` include `AND attempts = <claimed>`, so a worker whose lease expired and was re-claimed can't overwrite the newer attempt. If the stored `chunker_version` differs from the current one, the version's chunks are deleted before resuming.
- **Bedrock retries use botocore's standard mode with `total_max_attempts = 5`:** exponential backoff with full jitter on throttling and 5xx, 5 tries in total. (`max_attempts = 5` would mean 6 tries.) Embeddings run 8 calls in parallel within each batch of 32.
- **Activation clears `last_error`** on the version it makes live (added 2026-11-19). An earlier attempt's error would otherwise show on a healthy, indexed document in `GET /v1/documents/{id}`. The SPEC doesn't say either way.
- **Local runner** (`python -m worker.local`): registers the document and version as `app_ingest` with status `uploaded` (stand-in for step 9's API), then runs the same `ingest_version()` the Lambda will run. Dev only; `botocore[crt]` is a dev dependency needed for `aws login` sessions.

## 2026-11-17: AWS Region is us-east-2

- **Decision:** every AWS resource goes in `us-east-2`: Terraform provider, Lambdas, S3, SQS, EventBridge, ECR, Bedrock calls. The `ragdp` CLI profile, `AWS_REGION` in `.env` / `.env.example`, and `CLAUDE.md` now say `us-east-2`. The Neon project moves to AWS `us-east-2` as well (a new Neon project; the region of an existing one can't change), then bootstrap and migrations are re-run on its dev branch.
- **Why:** the AWS account is a new-experience "project" whose assigned Region is `us-east-2`. Read-only checks on 2026-11-16: `lambda list-functions` is AccessDenied in `us-east-1` and allowed in `us-east-2`; Bedrock `amazon.titan-embed-text-v2:0` is ACTIVE in `us-east-2`. Keeping Neon in `us-east-1` would add a cross-Region round trip to every query from the Lambdas.
- **Not affected:** the dev DB in `us-east-1` keeps working for local tests until the new Neon project exists. It holds only the empty schema, so nothing is migrated.
- **Done 2026-11-17:** new Neon project in AWS `us-east-2`, dev branch endpoint `ep-sparkling-heart-bdl5i7aa` (Postgres 18.7, pgvector 0.8.7). Bootstrap, `alembic upgrade head` (0003) and the full DB suite (41 tests) passed on it. The old `us-east-1` project is no longer used.

## 2026-11-16: Permanent and transient errors (for steps 5 and 8)

- **Decision:** ingestion errors are split in two. Permanent errors are raised as `PermanentError(code, message)` from `services/shared/errors.py`.
  - **Permanent:** invalid message (`ValidationError`, which already covers an S3 job that sets identity fields), a key that matches no stored version, sha256 mismatch, unparseable PDF. No retries.
  - **Transient:** Bedrock 429/5xx, DB timeout or connection error, network errors. Raise, so SQS retries up to `maxReceiveCount` = 3.
  - **Default:** anything not listed as permanent is transient.
- **Permanent path, in order:**
  1. Mark the version `failed` with `last_error`, only if this worker claimed it.
  2. Send the original message to the DLQ with the reason as a message attribute.
  3. Return success, so Lambda deletes the message from the source queue.

  If the DLQ send fails, raise, so SQS retries. The worker's IAM role needs `sqs:SendMessage` on the DLQ.
- **No claim, no row changes.** For an invalid message, or a key that matches no stored version, the worker sends to the DLQ and touches no rows. Step 5's claim must therefore tell "no matching version" (permanent) apart from "done, superseded or held by another worker" (delete the message and exit cleanly).
- **Transient path:** if claimed, record `status = 'failed'` and `last_error`, then raise. This keeps the SPEC's earlier behaviour, and the claim SQL already re-claims `failed` versions with `attempts < 3`.
- **Re-claiming:** a permanently failed version can be re-claimed while `attempts < 3`, by a duplicate delivery or a manual redrive. "No retries" means the worker does not ask SQS to redeliver.
- **Reason attribute:** an error code plus one line, capped at 256 characters (`PermanentError.reason`). Never document content or secrets. For a `ValidationError`, step 8 builds the reason without the input values (`errors(include_input=False)`).
- **Chaos table:** "Corrupt PDF" is now "1 attempt, then DLQ; status `failed` with error; alarm fires".
- **Why:**
  - With a 1800 s visibility timeout, three receives of a corrupt PDF take about an hour before the DLQ alarm fires. Sending it at once makes the alarm fire in seconds.
  - Defaulting to transient means a misclassified error costs wasted attempts, not a lost upload.
  - Marking `failed` without a claim would let a forged message fail someone's legitimate version.
- **To check in step 8:** whether SQS "redrive to source" recognises messages the worker sent to the DLQ itself, as opposed to ones SQS moved there. The redrive may need an explicit destination queue.

## 2026-11-16: Step 4 approved; standing rule on SPEC edits

- **Decision:** step 4 is approved, including the nesting guard, rejecting a `delete` job that names a version, and the SPEC edits. Standing rule, now in `CLAUDE.md` "How we work": SPEC edits are fine when they record already-approved decisions and are listed in the report. They never introduce new design.
- **Why:** the SPEC is what each step's session reads first, so it has to match what was agreed. Listing every edit keeps the changes reviewable.

## 2026-11-16: OPEN, reindex semantics are undefined

- **Status:** open. Do not build `POST /v1/documents/{id}/reindex` until this is settled. Also SPEC Open decisions #8.
- **Problem:** the current schema blocks both obvious meanings of "reindex":
  - **A new version of the same file** (for example after a chunker change): `UNIQUE (doc_id, sha256)` rejects a second version with the same hash.
  - **Re-running the live version in place:** forward-only activation (`active_version IS NULL OR active_version < $2`) returns no row for the version that is already active, so the worker would mark it `superseded`.
- **Until then:** `ReindexJob` exists in the message contract (tenant, document, version required) so the queue shape is stable, but nothing produces or handles it.

## 2026-11-16: IngestJob contract and rls_transaction, step 4 sign-off

1. **Invalid messages go to the DLQ with a logged reason, never a silent drop.**
   - An S3 job that sets `tenant_id`, `doc_id` or `version` is rejected, as is any job whose `source` doesn't match its `job_type`. An ingest job's identity comes only from its key.
   - The queue handler (step 8) logs the validation error and lets the message reach the DLQ.
   - **Why:** a dropped message is an upload that silently never gets indexed. The DLQ keeps it inspectable and the alarm fires.
2. **`IngestJob` is a Pydantic discriminated union on `job_type`,** one model per job type with its own required fields:
   - `ingest` (source `s3_event`): `bucket`, `key`, `size_bytes`, `etag` required. `tenant_id`, `doc_id`, `version` must be null.
   - `reindex` (source `api`): `tenant_id`, `doc_id`, `version` required.
   - `delete` (source `api`): `tenant_id`, `doc_id` required. No `version`; a delete that names one is rejected as ambiguous.
   - **Why:** one model with optional everything can't say which fields a job type needs. A delete covers the whole document, so it has no version.
3. **`job_id` is a string of 1 to 128 characters.** EventBridge supplies its event id; the API generates a UUIDv4 (step 9).
   - **Why:** the two producers don't share an id format. The bound keeps a bad producer from pushing arbitrarily long ids into logs.
4. **`role_level` is optional in `rls_transaction`, but has no default.**
   - With `None`, `app.role_level` is not set, so `app_role_level()` returns 0 and every role-checked policy matches nothing (fail closed).
   - The worker passes `None`; `app_ingest`'s policies check the tenant only. The API's claims dependency must always pass a role level from 1 to 3 (step 9).
   - `rls_transaction` refuses to nest. Nested, it would be a savepoint, and an inner `None` would inherit the outer transaction's role level instead of failing closed.
   - **Test:** `test_api_without_a_role_level_sees_nothing`. An `app_api` connection with role `None` sees zero rows from `documents` and `chunks`.
   - **Why:** making the worker pass a fake level (such as 3) would read as a privilege it doesn't use, and a default would let a caller forget the role silently.
5. **`statement_timeout_ms` must be between 100 and 60000.** 0 is rejected because it disables the timeout in Postgres. Named defaults: `API_STATEMENT_TIMEOUT_MS = 2000`, `WORKER_STATEMENT_TIMEOUT_MS = 30000`. `rls_transaction` defaults to the API value, so the shorter limit applies unless a caller asks for more.
   - **Why:** the worker's bulk inserts need more than the API's 2 s, but no caller should be able to switch the limit off.

Also from step 4:
- The pool refuses to open a connection unless `current_user` is `app_api` or `app_ingest`.
- `pgvector` 0.5.1 has no `numpy` dependency; vectors go in as lists and come back as `pgvector.Vector`.

## 2026-11-16: Concurrent version allocation (for step 9)

- **Decision:** `POST /v1/documents` must handle two registrations of the same document arriving at once. Both can compute the same next version number, for example `max(version) + 1`. The primary key `(doc_id, version)` stops corruption, but the second request would fail with a unique violation. The endpoint must either:
  - retry on a unique violation (re-read the max, try again, bounded attempts), or
  - allocate the number under a row lock: `SELECT ... FROM documents WHERE id = $1 FOR UPDATE`, then compute the next version in the same transaction.
- **Test:** step 9's "done when" includes a concurrency test. Two simultaneous registrations of different content for the same document both succeed, with distinct version numbers.
- **Why:** the database guarantees integrity, but a 500 on a legitimate concurrent upload is still a bug. Same-content duplicates are a separate case and still return the existing version, via `UNIQUE (doc_id, sha256)`.

## 2026-11-16: RLS suite timing

- **Decision:** leave the per-test seeding as is (about 75 s for 36 tests). Revisit only if the suite passes about 2 minutes, and then with `pytest-xdist` (parallel workers) rather than shared fixtures.
- **Why:** fresh random tenants per test keep the tests independent. Parallelism buys speed without giving that up.

## 2026-11-16: Migration 0003, document_versions INSERT columns

- **Decision:** `app_api`'s INSERT on `document_versions` is limited to `(doc_id, version, tenant_id, sha256, s3_key)`. `status` gets `DEFAULT 'pending_upload'`; `attempts` already defaulted to 0. The WITH CHECK (`status = 'pending_upload' AND attempts = 0`) stays as a second layer.
- **Why:** the WITH CHECK pinned `status` and `attempts`, but the API could still set worker-owned columns at insert: `lease_until`, `last_error`, `parser`, `chunker_version`, `embedding_model`, `chunk_count`, `indexed_at`.
- **Testing consequence:** `app_api` can no longer write `status` or `attempts` at all, so the WITH CHECK can't be triggered through it. `test_version_insert_policy_keeps_its_second_layer` asserts from `pg_policies` that the policy still requires both, so the layer can't be dropped silently.
- **Verified:** with 0003 rolled back, all 8 new tests fail. The 7 protected-column inserts don't raise, and the minimal insert fails because `status` has no default.

## 2026-11-16: asyncpg prepared statements work over Neon's pooler

- **Decision:** keep asyncpg's default statement cache. `statement_cache_size=0` is not needed.
- **Why:** measured 2026-11-16. The full RLS suite (31 tests, asyncpg over the pooled endpoint with the default cache) ran with no prepared-statement errors. Revisit if Neon's pooler config changes or errors like "prepared statement ... does not exist" appear.

## 2026-11-16: "Context is gone in the next transaction" runs on the direct endpoint

- **Decision:** the test connects as `app_api` to the direct endpoint and asserts `pg_backend_pid()` is the same in both transactions. A control test sets the context with `is_local = false` and shows it does survive, so the main test can fail.
- **Why:** through the transaction-mode pooler, two transactions from one client may run on different server sessions, so the test could pass without proving anything. Measured at idle, the pooler kept one client on one server session for 30 transactions, but nothing guarantees that under load.

## 2026-11-16: Step 3b, documents INSERT columns and behaviour tests for step-2 policies

- **Decision:** migration 0002 replaces `app_api`'s table-level INSERT on `documents` with `INSERT (tenant_id, external_id, title, min_role_level)`. `tests/rls/test_rls_write_limits.py` adds a behaviour test for every step-2 policy and grant change.
  - **Covered:** column-level UPDATE, column-level INSERT, the role check on document writes, the version-insert check, no UPDATE on versions, `query_cache` role isolation, tenant-consistent FKs, and `feedback` being INSERT-only.
  - **Error checks:** negative cases assert the specific error: InsufficientPrivilegeError with "permission denied for table X", the RLS WITH CHECK message, or ForeignKeyViolationError with the constraint name.
- **What the gap allowed:** RLS still blocked other tenants and higher role levels, but the API could insert a document with any column filled in:
  - `active_version` already set, which makes forward-only activation reject every real version, so they all end up `superseded`
  - `deleted_at` set at birth
  - a caller-chosen `id` or `created_at`
- **Verified:** with 0002 rolled back, the four protected-column INSERT tests fail (`DID NOT RAISE`).

## 2026-11-16: "tenant_id in the request body is ignored" tested at two levels

- **Decision:** the step 3 test runs at the DB level. With tenant A's context, queries filtered by tenant B's `tenant_id` or a B document id return nothing. Step 9 adds the API-level test: POST a body containing another tenant's `tenant_id` and assert the claims' tenant is the one used.
- **Why:** no API exists in step 3. The DB-level test proves RLS holds even if a handler wrongly uses a body value. The API-level test proves handlers take tenant only from `get_claims()`.

## 2026-11-16: Migration tooling and connection strings (step 2)

- **Decision:** Alembic + `psycopg[binary]` + `python-dotenv` as dev dependencies (kept out of Lambda images). `env.py` reads `DATABASE_URL_OWNER` and sets `lock_timeout = '5s'` and `statement_timeout = '60s'` on the migration connection. Each SQL statement in a migration is its own `op.execute` call.
- **Connection strings:** every URL uses `sslmode=verify-full&sslrootcert=<CA bundle file>` (`/etc/ssl/cert.pem` on macOS, `/etc/ssl/certs/ca-certificates.crt` on Ubuntu CI).
  - The owner URL (psycopg/libpq) points at the direct endpoint and adds `channel_binding=require`.
  - The app-role URLs (asyncpg) point at the pooled endpoint and omit `channel_binding`, which asyncpg doesn't understand.
  - `sslrootcert=system` was the original plan, but it fails certificate verification with psycopg-binary's bundled OpenSSL on macOS, so the bundle file is named explicitly.
- **Why:** migrations should fail fast on a lock rather than hang; `verify-full` is what the SPEC requires; one URL format per driver avoids runtime parse errors.

## 2026-11-16: Catalog test for RLS coverage

- **Decision:** `tests/rls/test_rls_catalog.py` (marked `db`) fails if any table in `public` except `alembic_version` lacks `relrowsecurity` and `relforcerowsecurity`, or if any table grants privileges to PUBLIC. It also checks the six expected tables exist, so it can't pass vacuously.
- **Why:** behavioural RLS tests only cover tables someone remembered to test. This catches a future migration that adds a table without RLS.

## 2026-11-16: Policies and grants completed; tenant-consistent foreign keys

- **Decision:** the SPEC said "same pattern" for tables other than `chunks`. Agreed full set (now in SPEC "Data model and RLS"):
  - **Tenant-consistent FKs.** `UNIQUE (id, tenant_id)` on `documents` and `UNIQUE (doc_id, version, tenant_id)` on `document_versions`. FKs are `document_versions(doc_id, tenant_id) -> documents(id, tenant_id)` and `chunks(doc_id, version, tenant_id) -> document_versions(doc_id, version, tenant_id)`.
  - **`documents`, `app_api`:** tenant + role-level check on reads and writes; UPDATE only on `(title, deleted_at)`.
  - **`document_versions`, `app_api`:** SELECT and INSERT only, no UPDATE. INSERT requires `status = 'pending_upload' AND attempts = 0` and a visible parent document. Reads require the parent document to be visible (EXISTS under `documents`' RLS).
  - **`tenant_index_state`:** ENABLE + FORCE RLS (missing from the SPEC). Tenant-only policy for both app roles; `app_api` gets SELECT, INSERT, UPDATE.
  - **`query_cache`:** new `role_level smallint NOT NULL` column. Policy `tenant_id = app_tenant() AND role_level = app_role_level()`. `app_api` gets SELECT, INSERT, UPDATE, DELETE.
  - **`feedback`:** `app_api` gets INSERT only.
  - `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC`. EXECUTE on `app_tenant()` / `app_role_level()` revoked from PUBLIC and granted to `app_api` and `app_ingest` only.
  - FORCE RLS on all six tables.
- **Deletes changed:** the API sets `deleted_at` and bumps the epoch; the `delete` job clears `active_version`. Recall already filters `deleted_at IS NULL`, so the document still disappears immediately.
- **Why:**
  - FK checks bypass RLS, so a plain FK lets a row reference another tenant's parent.
  - The API must not change `min_role_level`, because chunks carry a copy and would go out of sync. It must not change `active_version` either, because activation belongs to the worker.
  - Cache isolation should not depend on getting the cache key right.
  - The hard rule says every tenant table gets RLS.

## 2026-11-16: Roles created by a bootstrap script, not by migrations

- **Decision:** `db/bootstrap.py`, run once per dev branch as Neon's default owner (`NEON_ADMIN_URL`), creates `app_owner`, `app_api` and `app_ingest` with SQL as `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`. It also runs `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, grants CREATE on `public` to `app_owner`, and creates the `vector` extension.
  - **Passwords:** reused from `.env` or generated with `secrets.token_urlsafe(32)`, and written to `.env`, never printed. `CREATE ROLE ... PASSWORD` can't take bind parameters, so the password is quoted with psycopg's `sql.Literal`.
  - **Dev-branch guard:** the script refuses to run unless the endpoint ID in `NEON_ADMIN_URL` equals `NEON_DEV_ENDPOINT_ID`, or `--i-know-this-is-dev` is passed. Neon hostnames don't contain the branch name, so the endpoint ID is the reliable check.
  - **Production:** role passwords live in SSM Parameter Store, never in `.env`. This script is dev-only.
- **Why:**
  - `app_owner` can't create itself, and migrations run as `app_owner`.
  - `alembic downgrade base` must not drop roles and their passwords.
  - Passwords must never be committed.
  - Console-created Neon roles join `neon_superuser`, which is wider than the app roles should be.

## 2026-11-16: Planned experiment, decision model in the query path (after Slice 4)

- **Decision:** build and measure the full system as specified first. After Slice 4, try Gemini Decision 1 (`gemini-decision-1`) in two places and re-measure: (1) replace Cohere rerank + score-threshold abstain with per-chunk relevance + one answerability question in a single call; (2) add a choice question that routes simple lookups to a smaller LLM. Keep the current path as the fallback (`degraded` flag on timeout).
- **Why:** unanswerable questions could skip the LLM call entirely, easy questions could use a faster model, and calibrated probabilities make the abstain threshold meaningful. It also removes the Cohere trial limit. The before/after comparison is planned as a LinkedIn post.
- **What this requires now, so the baseline is comparable later:**
  - Every query emits per-stage spans: `embed`, `sql_recall`, `rerank`, `abstain_gate`, `llm_generate`, `citation_check`, plus `abstained`, `abstain_reason`, model used and cost.
  - Eval runs save results to `eval/results/<date>_<git-sha>.json` with the golden-set version, so the same questions can be rerun later.
  - The baseline report states p50/p95 per stage and end-to-end, abstain precision and recall, recall@5 and cost per 1,000 queries, measured warm and with Neon cold start noted.
- **Do not build any of this before Slice 4 is done.**

## 2026-11-16: Leak matrix runs without the reranker

- **Decision:** the leak matrix calls `/v1/retrieve` with reranking turned off. CI reranks only a sample of about 30 questions per PR; the full reranked eval is run by hand before publishing results.
- **Why:** a Cohere trial key allows about 1,000 calls/month total and 10 rerank calls/minute. The full matrix (294 questions × 6 personas = 1,764 calls) would exhaust it in one run. RLS filters rows before the reranker sees them, so the reranker cannot affect leak results.
- **Affects:** SPEC Proof ("Leak matrix") and Open decisions #7.

## 2026-11-16: Dev auth stub for Slice 1

- **Decision:** until Slice 2, endpoints take claims from a `get_claims()` dependency that accepts a fixed `DEV_AUTH_TOKEN`.
- **Why:** real JWT verification depends on Open decision #1 (RS256 + JWKS in rag-control-plane). The stub keeps Slice 1 unblocked, and isolating it in one function means the swap later touches one place.
- **Replaced** in step 12 (2026-11-21, "Step 12 claims in the API").
