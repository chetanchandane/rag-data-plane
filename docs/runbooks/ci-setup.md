# CI setup (steps 10c and 10d, once)

`.github/workflows/ci.yml` needs these GitHub settings for `chetanchandane/rag-data-plane`. None of the values goes in the repo.

| Name | Kind | Value | Used by |
| --- | --- | --- | --- |
| `NEON_PROJECT_ID` | variable | Neon console: project, Settings, General, Project ID | `db` and `leak` jobs |
| `NEON_API_KEY` | secret | Neon API key (below) | `db` and `leak` jobs: create and delete their branches; `leak` also checks the `eval` branch |
| `DEV_APP_API_PASSWORD` | secret | `app_api`'s password on the `dev` branch (from `.env`) | `db` job; `leak` job (the `eval` branch, a child of `dev`, has the same passwords) |
| `DEV_APP_INGEST_PASSWORD` | secret | `app_ingest`'s password on the `dev` branch (from `.env`) | `db` job |
| `ALERT_EMAIL` | `prod` environment secret | The alarm and budget email | `deploy` job (`TF_VAR_alert_email`) |

The `leak` job also needs a Neon branch named `eval`, a child of `dev` with **no expiry**, holding the indexed eval corpus. Neon refuses to copy an expiring branch, and the job's first step fails if `eval` is missing or expires (`eval/README.md`, DECISIONS 2026-11-24).

The `db` job gets `app_owner`'s password from Neon's branch action, so it needs no secret. The production `migrate` job needs none either: it signs in to AWS with OIDC (`ragdp-github-migrate`) and reads the owner URL from SSM.

## 1. Neon API key

In the Neon console, create an API key. If your account offers a **project-scoped** key (organization settings, API keys), make it for this project only: then a leaked key reaches no other project. Copy it once; Neon won't show it again.

## 2. Set the values with the GitHub CLI

The CLI reads each value from standard input, so no value is shown on screen or saved in shell history.

```bash
brew install gh && gh auth login        # once
cd ~/dev/rag-v2
R=chetanchandane/rag-data-plane

gh variable set NEON_PROJECT_ID --repo $R --body '<project id>'
gh secret set NEON_API_KEY --repo $R    # paste the key, then Enter, then Ctrl-D

# The two dev passwords, read from .env without printing them.
for role in API INGEST; do
  uv run python -c "from dotenv import dotenv_values; from urllib.parse import urlsplit; \
print(urlsplit(dotenv_values('.env')['DATABASE_URL_$role']).password, end='')" \
  | gh secret set DEV_APP_${role}_PASSWORD --repo $R
done

gh secret list --repo $R && gh variable list --repo $R   # names only, never values
```

Then the deploy's environment secret. `gh` asks for the value and hides it:

```bash
gh secret set ALERT_EMAIL --repo $R --env prod
gh secret list --repo $R --env prod
```

## 3. The `prod` environment

- **Deployment branches:** `main` only (Settings, Environments, `prod`). The AWS roles trust any job that names `prod`, so this setting is what keeps other branches and pull requests out.
- **No required reviewer:** a push to `main` that passes the tests migrates and deploys by itself. The deploy refuses plans that delete or replace anything (DECISIONS 2026-11-20, "Step 10d").

## 4. AWS role for the migrate job

Apply `infra/bootstrap` (it adds `ragdp-github-migrate`: 1 role, 1 inline policy). It trusts the same `prod` environment as the deploy role.

## 4b. The eval workflow (step 18)

`.github/workflows/eval.yml` runs only when started by hand (Actions, Eval, Run workflow), about $1 a run.

1. **Apply `infra/bootstrap`:** it adds `ragdp-github-eval` (1 role, 1 inline policy). It can call only the answering models (Nova 2 Lite through its profile, Qwen3 235B) and the judge (DeepSeek V3.2), and read `/ragdp/prod/cohere_api_key`. It trusts only the `eval` environment.
2. **Create the `eval` environment** (Settings, Environments), with no deployment branch restriction, so any branch can be measured against `main`'s baseline. Or: `gh api -X PUT repos/$R/environments/eval`.
3. **No new secrets.** It uses the leak job's `NEON_API_KEY`, `DEV_APP_API_PASSWORD` and `NEON_PROJECT_ID`, and reads the Cohere key from SSM.

## 5. Check

Push to `main`. In the Actions tab, `lint-and-unit`, then `db`, then `migrate`, then `deploy / deploy` should pass. `migrate`'s last step prints the production head (`0003 (head)`); `deploy`'s last step prints the worker's image, tagged with the pushed commit.

If you rotate the dev passwords (`db/bootstrap.py` with new values in `.env`), update the two `DEV_APP_*` secrets. Production rotations (`--production`) need nothing here.
