#!/usr/bin/env bash
# Explicit, disposable local Playwright runner. Hosted PR CI is the default.

set -euo pipefail

if [[ "${1:-}" != "--allow-local-e2e" ]]; then
  cat >&2 <<'EOF'
Local browser E2E is intentionally opt-in: it builds a full disposable
Möbius stack and can take several minutes. Prefer the GitHub PR checks.

For a focused local investigation:
  scripts/playwright-local.sh --allow-local-e2e <spec or --grep arguments>
EOF
  exit 2
fi
shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v docker >/dev/null || {
  echo "error: Docker is required for isolated local E2E" >&2
  exit 2
}

run_id="$(date +%s)-$$"
project="mobius-local-e2e-${run_id}"
image_name="${project}:test"
app_container="${project}-app"
recovery_container="${project}-recoveryd"
test_publish_port="${TEST_PORT:-0}"
recovery_publish_port="${RECOVERY_TEST_PORT:-0}"
head_sha="$(git rev-parse HEAD)"
auth_dir="$(mktemp -d "${TMPDIR:-/tmp}/mobius-e2e-auth.XXXXXX")"
auth_file="$auth_dir/state.json"

compose() {
  MOBIUS_CONTAINER="$app_container" \
  MOBIUS_RECOVERYD_CONTAINER="$recovery_container" \
  MOBIUS_IMAGE="$image_name" \
  TEST_PORT="$test_publish_port" \
  RECOVERY_TEST_PORT="$recovery_publish_port" \
  BUILD_SHA="$head_sha" \
  GITHUB_SHA="$head_sha" \
    docker compose -p "$project" -f docker-compose.test.yml \
      --project-directory "$ROOT" "$@"
}

cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$image_name" >/dev/null 2>&1 || true
  rm -rf "$auth_dir"
}
trap cleanup EXIT INT TERM

echo "Building disposable test stack (project: $project)..."
compose build
compose up -d
test_port="$(docker port "$app_container" 8000/tcp | tail -1 | awk -F: '{print $NF}')"
if [[ -z "$test_port" ]]; then
  echo "error: Docker did not publish the isolated backend port" >&2
  exit 1
fi

echo "Waiting for isolated backend identity check..."
for _ in $(seq 1 60); do
  health="$(docker inspect --format='{{.State.Health.Status}}' "$app_container" 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  [[ "$health" == "unhealthy" ]] && {
    compose logs --tail 200 app
    echo "error: isolated test backend is unhealthy" >&2
    exit 1
  }
  sleep 2
done

version="$(curl -fsS "http://127.0.0.1:${test_port}/api/version")"
python3 -c '
import json, sys
value = json.loads(sys.argv[1])
if value.get("test_runtime") is not True:
    raise SystemExit("refusing browser run: backend is not a test runtime")
' "$version"

echo "Running focused Playwright checks with one worker..."
MOBIUS_LOCAL_E2E=1 \
MOBIUS_AUTH_FILE="$auth_file" \
MOBIUS_URL="http://127.0.0.1:${test_port}" \
MOBIUS_USER=admin \
MOBIUS_PASS=admin \
  npx playwright test "$@" --workers=1
