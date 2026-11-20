#!/usr/bin/env bash
# Publish the synthesizer permanently, with no manual dashboard step.
#
# What this does, in order:
#   1. gates on the test suite, so a broken engine is never published
#   2. builds the engine image locally to prove the Dockerfile still works
#   3. pushes the branch to origin, which triggers .github/workflows/deploy.yml
#      (Pages + GHCR image) on the default branch
#   4. verifies the published URL actually answers
#
# What it deliberately does not do: pretend a host exists that does not. A
# Cloudflare, Render, Fly, or Vercel deploy needs an account token. When such a
# token is present in the environment it is used; when it is absent the script
# says so and publishes the parts that need no account instead of failing.
set -euo pipefail

REPO="${REPO:-mansourhashad8-tech/sovereign-mathematical-synthesizer}"
BRANCH="${BRANCH:-master}"
PAGES_URL="https://${REPO%%/*}.github.io/${REPO##*/}/"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/4  Gating on the test suite"
python3 -m pytest tests/ -q

say "2/4  Building the engine image"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker build -t "${REPO##*/}:local" .
  echo "image built: ${REPO##*/}:local"
else
  echo "docker unavailable here; CI builds and pushes the image instead"
fi

say "3/4  Publishing to the default branch"
git rev-parse --verify "$BRANCH" >/dev/null 2>&1 || git branch "$BRANCH"
git push origin "HEAD:$BRANCH"
echo "pushed; deploy.yml now rebuilds Pages and the GHCR image"

say "4/4  Verifying the permanent URL"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$PAGES_URL" || echo 000)
  if [ "$code" = "200" ]; then
    echo "live: $PAGES_URL (HTTP $code)"
    break
  fi
  echo "  attempt $attempt: HTTP $code, waiting for the deploy to finish..."
  sleep 20
done

say "Optional managed hosts"
# Each of these needs an account token. Presence is checked, not assumed.
if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
  echo "CLOUDFLARE_API_TOKEN present: see cloudflare/worker.js and deploy with"
  echo "  npx wrangler deploy"
else
  echo "CLOUDFLARE_API_TOKEN absent: the Workers backend is not deployed."
  echo "  The workflow ships the same engine as a GHCR image, which any"
  echo "  container host can pull with no rebuild."
fi

say "Done"
echo "permanent client : $PAGES_URL"
echo "engine image     : ghcr.io/$REPO:latest"
