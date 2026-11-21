# Token issuer keys

The stand-in token issuer (DECISIONS 2026-11-21, "Token issuer for Slice 2" and "Step 11") signs access tokens with an RSA key whose private half exists only in SSM. Verifiers find the public half through:

```text
https://<distribution>/.well-known/openid-configuration   ->  issuer, jwks_uri
https://<distribution>/.well-known/jwks.json               ->  the public keys
```

| What | Where |
| --- | --- |
| Private keys | SSM SecureString `/ragdp/issuer/signing_keys/<kid>` |
| Which key signs | SSM String `/ragdp/issuer/active_kid` |
| Public keys | `infra/envs/prod/jwks.json` (committed), published by Terraform (`infra/modules/issuer`) |
| Issuer URL | `terraform -chdir=infra/envs/prod output -raw issuer_url`, also `JWT_ISSUER` in `.env` |

A `kid` is the key's RFC 7638 thumbprint. Every command below checks the AWS account is 905418224080, and runs from the repo root with `export PYTHONPATH=services` and your `ragdp` sign-in.

## Caches decide the timing

A new or removed key is only seen after every cache in front of it has refreshed:

| Cache | How long |
| --- | --- |
| CloudFront and clients | 5 minutes (`Cache-Control: max-age=300` on both documents) |
| API Gateway's JWT authorizer | up to **2 hours**, best effort (AWS docs) |
| Tokens already issued | 15 minutes (their `exp`) |

So a new key must be published **at least 2 hours before** it starts signing, and an old key must stay published for **at least 15 minutes after** it stops.

## First key (once)

1. `uv run python scripts/issuer.py create-key` stores the key in SSM, adds its public key to `jwks.json` and activates it (it's the only one).
2. Commit `jwks.json` and push. The deploy publishes it.
3. Put the issuer URL in `.env`: `JWT_ISSUER=$(terraform -chdir=infra/envs/prod output -raw issuer_url)`.
4. Check: `uv run python scripts/issuer.py mint --tenant <uuid> --role-level 1 --check`. It prints `verified against ...` on stderr and the token on stdout.

## Minting a token

```bash
TOKEN=$(uv run python scripts/issuer.py mint --tenant <uuid> --role-level 2)
curl -H "Authorization: Bearer $TOKEN" ...
```

Options: `--scope <name>` (repeatable), `--subject <name>` (default `local-dev`), `--check`. Tokens last 15 minutes.

## Planned rotation

1. `uv run python scripts/issuer.py create-key`. It prints the new kid. The old key keeps signing.
2. Commit `jwks.json` and push. Both keys are now published.
3. **Wait at least 2 hours** after the deploy, for API Gateway's cache.
4. `uv run python scripts/issuer.py activate-key <new kid>`. New tokens are signed with it.
5. Check with `mint --check`.
6. **Wait at least 15 minutes**, for tokens signed with the old key to expire.
7. `uv run python scripts/issuer.py retire-key <old kid>` removes it from `jwks.json` and deletes its private key from SSM. Commit and push.

## The signing key leaked

Do the rotation without the waits: `create-key`, push, `activate-key <new>`, `retire-key <old>`, push.

Until API Gateway's cache refreshes (up to 2 hours), tokens signed with the leaked key can still pass API Gateway, and tokens signed with the new key may be refused. To shorten the first window, invalidate CloudFront too:

```bash
aws cloudfront create-invalidation --profile ragdp \
  --distribution-id "$(terraform -chdir=infra/envs/prod output -raw issuer_distribution_id)" \
  --paths "/.well-known/*"
```

API Gateway's own cache can't be flushed, and the API on Lambda doesn't verify tokens again (DECISIONS 2026-11-21, "Step 12"), so for up to 2 hours whoever has the leaked key can mint tokens that API Gateway accepts. If that window is too long, stop the API until the cache has refreshed: once step 13 exists, set the API Lambda's reserved concurrency to 0, then remove the setting afterwards. A locally run API (`api.local`) caches the JWKS for 5 minutes only.
