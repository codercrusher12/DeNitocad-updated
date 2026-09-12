#!/usr/bin/env bash
# Repeatable deploy: pull latest code, rebuild the app image, restart with
# zero-downtime-ish behavior (Caddy keeps serving while `app` restarts;
# there's a few seconds of 502s during the swap since this is a single
# instance, not a rolling deploy - fine for a solo/small-scale VPS
# deployment, not what you'd want at real scale).
#
# Run from the repo root on the VPS:
#   ./deploy/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "No .env found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

echo "==> Pulling latest code"
if [[ -d .git ]]; then
  git pull --ff-only
else
  echo "    (not a git checkout - skipping pull, assuming you've already synced files)"
fi

echo "==> Building app image"
docker compose build app

echo "==> Restarting stack"
docker compose up -d

echo "==> Waiting for /healthz to come back healthy"
for i in $(seq 1 30); do
  if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" >/dev/null 2>&1; then
    echo "    healthy after ${i}0s or less"
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    echo "    still not healthy after 5 minutes - check: docker compose logs app" >&2
    exit 1
  fi
  sleep 10
done

echo "==> Pruning old, now-unused images (keeps disk from filling up over repeated deploys)"
docker image prune -f >/dev/null

echo "==> Done. Recent logs:"
docker compose logs --tail=20 app
