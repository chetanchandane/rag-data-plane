#!/usr/bin/env bash
# Fail with a readable reason unless the Neon `eval` branch exists and never expires.
# Neon refuses to copy a branch that has an expiry, and the branch action reports only "400"
# (DECISIONS 2026-11-24, "The `eval` branch never expires"). Used by ci.yml's leak job and
# eval.yml before they copy the branch. Needs NEON_API_KEY and PROJECT_ID.
set -euo pipefail

branches=$(curl -sSf -H "Authorization: Bearer ${NEON_API_KEY}" \
  "https://console.neon.tech/api/v2/projects/${PROJECT_ID}/branches") \
  || { echo "::error::Neon API request failed (key or project id?)"; exit 1; }
branch=$(jq -c '.branches[] | select(.name == "eval")' <<<"$branches")
if [ -z "$branch" ]; then
  echo "::error::No 'eval' branch. Create it from dev with no expiry, then reload the corpus (eval/README.md)."
  exit 1
fi
if [ -n "$(jq -r '.expires_at // empty' <<<"$branch")" ]; then
  echo "::error::The 'eval' branch has an expiry, so Neon refuses to copy it. Remove the expiry in the Neon console (eval/README.md)."
  exit 1
fi
echo "eval branch $(jq -r .id <<<"$branch") has no expiry"
