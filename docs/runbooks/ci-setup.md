# CI setup (steps 10c and 10d, once)

`.github/workflows/ci.yml` needs these GitHub settings for `chetanchandane/rag-data-plane`. None of the values goes in the repo.

| Name | Kind | Value | Used by |
| --- | --- | --- | --- |
| `NEON_PROJECT_ID` | variable | Neon console: project, Settings, General, Project ID | `db` job |
| `NEON_API_KEY` | secret | Neon API key (below) | `db` job: create and delete its branch |
| `DEV_APP_API_PASSWORD` | secret | `app_api`'s password on the `dev` branch (from `.env`) | `db` job |
| `DEV_APP_INGEST_PASSWORD` | secret | `app_ingest`'s password on the `dev` branch (from `.env`) | `db` job |
| `ALERT_EMAIL` | `prod` environment secret | The alarm and budget email | `deploy` job (`TF_VAR_alert_email`) |

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

## 5. Check

Push to `main`. In the Actions tab, `lint-and-unit`, then `db`, then `migrate`, then `deploy / deploy` should pass. `migrate`'s last step prints the production head (`0003 (head)`); `deploy`'s last step prints the worker's image, tagged with the pushed commit.

If you rotate the dev passwords (`db/bootstrap.py` with new values in `.env`), update the two `DEV_APP_*` secrets. Production rotations (`--production`) need nothing here.
