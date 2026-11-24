# rag-data-plane

The data plane for `rag-control-plane`: a multi-tenant RAG ingestion and retrieval API on AWS (Lambda, S3, SQS, API Gateway) with Postgres + pgvector on Neon. Tenant and role isolation is enforced by Postgres row-level security, and the design is in [`docs/SPEC.md`](docs/SPEC.md), with agreed changes in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Leak matrix

Every eval question (150 answerable, 30 unanswerable, 10 extraction attempts) is asked as each of six personas (2 tenants × role levels 1 to 3) through `/v1/retrieve`, connected as `app_api` with RLS on and reranking off. Every returned chunk is checked two ways:

- **Structural:** its document belongs to the persona's tenant and is at or below the persona's role level, according to the committed manifests rather than the database.
- **Canary:** no `CANARY-…` token from another tenant or a higher level appears in its text.

Each persona must also retrieve every canary it *is* allowed to see, so an empty result can't pass as "no leaks". It runs in CI on every push and pull request, and any leak fails the build (`tests/leak/`, DECISIONS 2026-11-22 "Step 17").

Latest run (2026-11-24, on the recreated `eval` branch; the same matrix passes in CI, run 45434358713). A deliberately broken policy on a throwaway pull request made it fail with 1,170 leaks (DECISIONS 2026-11-22, "Step 17").

| Persona | Chunks returned | Documents seen | Canaries seen (all allowed) | Allowed canaries missed | Leaks |
| --- | ---: | ---: | --- | --- | ---: |
| a-r1 | 5700 | 14 | CANARY-TA-R1-142f96 | none | 0 |
| a-r2 | 5700 | 15 | CANARY-TA-R1-142f96, CANARY-TA-R2-79192d | none | 0 |
| a-r3 | 5700 | 16 | CANARY-TA-R1-142f96, CANARY-TA-R2-79192d, CANARY-TA-R3-07a2a8 | none | 0 |
| b-r1 | 1140 | 6 | CANARY-TB-R1-a0d6a4 | none | 0 |
| b-r2 | 1330 | 7 | CANARY-TB-R1-a0d6a4, CANARY-TB-R2-2d4814 | none | 0 |
| b-r3 | 1520 | 8 | CANARY-TB-R1-a0d6a4, CANARY-TB-R2-2d4814, CANARY-TB-R3-c59343 | none | 0 |

**1,140 requests, 0 leaks.** Tenant B returns fewer chunks because its corpus is small: five one-page SOPs plus its canaries.
