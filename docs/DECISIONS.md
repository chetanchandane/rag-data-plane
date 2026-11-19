# Decisions log

Changes to `docs/SPEC.md` that were agreed after it was written. Newest first. Format: date, decision, why.

## 2026-11-19: Step 10 is split into 10a to 10e

- **Decision** (approved 2026-11-19): step 10 becomes five steps, each with its own "done when" and commit: 10a production database and first deploy, 10b end-to-end run by hand, 10c CI, 10d deploys, 10e traces. Listed in `CLAUDE.md`.
- **Why:** step 10's "done when" is every Slice 1 box, and three of them need work no earlier step built: a deploy workflow ("a push to `main` deploys through OIDC"), CI migrations ("migrated on Neon by CI") and telemetry ("traces show parse, chunk and embed spans"). Moving them to Slice 2 was considered and rejected: they are what makes Slice 1 worth posting.
- **Found while planning:** the Neon project has only its default branch, `main`. The endpoint used as "the dev branch" since 2026-11-17 is therefore probably `main`'s, so bootstrap, migrations and every DB test ran there. No real data is at risk, but `main` can't become production as is: its role passwords are in `.env`, and step 5's local ingest left rows behind. 10a fixes it: create `dev` from `main`, move `.env` to it, then give `main` new passwords that exist only in SSM and empty its tables.

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
