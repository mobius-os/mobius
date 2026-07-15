#!/usr/bin/env bash
# Explicit, host-only Playwright runner over one committed, disposable revision.

set -euo pipefail

if [[ "${1:-}" != "--allow-local-e2e" ]]; then
  cat >&2 <<'EOF'
Local browser E2E is intentionally opt-in: it builds a full disposable
Möbius stack and can take several minutes. Prefer the GitHub PR checks.

Run this on a Docker-capable host, not inside the Möbius app container:
  scripts/playwright-local.sh --allow-local-e2e <spec or --grep arguments>
EOF
  exit 2
fi
shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

for command_name in git docker curl flock python3 sha256sum; do
  command -v "$command_name" >/dev/null || {
    echo "error: $command_name is required for isolated local E2E" >&2
    exit 2
  }
done

if [[ ! -x "$ROOT/node_modules/.bin/playwright" ]]; then
  echo "error: Playwright is not installed; run 'npm ci' from $ROOT" >&2
  exit 2
fi

# The disposable runtime intentionally serves a committed tree. Refuse tracked
# edits rather than running working-tree tests against an older backend/frontend
# from HEAD and reporting a false result. Ignored build artifacts do not affect
# the snapshot; non-ignored untracked source does.
if ! git diff --quiet \
    || ! git diff --cached --quiet \
    || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  cat >&2 <<'EOF'
error: local E2E requires a committed revision.
Commit or stash source changes first so the tests and runtime use identical code.
EOF
  exit 2
fi

head_sha="$(git rev-parse --verify HEAD)"
root_id="$(printf '%s' "$ROOT" | sha256sum | cut -c1-12)"
lock_path="${TMPDIR:-/tmp}/mobius-local-e2e-${root_id}.lock"
exec 9>"$lock_path"
if ! flock -n 9; then
  echo "error: another isolated local E2E run is active for $ROOT" >&2
  exit 2
fi
run_id="$(date +%s)-$$"
project="mobius-local-e2e-${run_id}"
image_name="mobius-local-e2e-${root_id}:test"
app_container="${project}-app"
recovery_container="${project}-recoveryd"
test_publish_port="${TEST_PORT:-0}"
recovery_publish_port="${RECOVERY_TEST_PORT:-0}"
run_dir="$(mktemp -d "${TMPDIR:-/tmp}/mobius-local-e2e.XXXXXX")"
snapshot_dir="$run_dir/source"
auth_file="$run_dir/auth-state.json"
compose_used=0

compose() {
  # The baked fallback clones GitHub and cannot fetch an unpublished local
  # commit. Leave that fallback unstamped; the mounted /workspace checkout and
  # runtime BUILD_SHA below still use the exact committed local revision.
  MOBIUS_CONTAINER="$app_container" \
  MOBIUS_RECOVERYD_CONTAINER="$recovery_container" \
  MOBIUS_IMAGE="$image_name" \
  TEST_PORT="$test_publish_port" \
  RECOVERY_TEST_PORT="$recovery_publish_port" \
  BUILD_SHA=unknown \
  GITHUB_SHA="$head_sha" \
    docker compose -p "$project" -f "$snapshot_dir/docker-compose.test.yml" \
      --project-directory "$snapshot_dir" "$@"
}

cleanup() {
  if [[ "$compose_used" == "1" ]]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$run_dir"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# A real standalone clone gives linked worktrees a self-contained .git
# directory inside /workspace. --no-local copies objects instead of writing an
# alternates path that would point outside the container mount.
git clone --quiet --no-local "$ROOT" "$snapshot_dir"
git -C "$snapshot_dir" checkout --quiet --detach "$head_sha"
snapshot_sha="$(git -C "$snapshot_dir" rev-parse --verify HEAD)"
if [[ "$snapshot_sha" != "$head_sha" ]]; then
  echo "error: revision snapshot mismatch ($snapshot_sha != $head_sha)" >&2
  exit 1
fi

# Playwright runs from the same committed snapshot as the disposable server.
# node_modules is host-only and excluded from the Docker build context.
node_modules_root="$(cd "$ROOT/node_modules" && pwd -P)"
ln -s "$node_modules_root" "$snapshot_dir/node_modules"

echo "Building disposable test stack for ${head_sha:0:12} (project: $project)..."
compose_used=1
compose build
compose up -d
test_port="$(docker port "$app_container" 8000/tcp | tail -1 | awk -F: '{print $NF}')"
if [[ -z "$test_port" ]]; then
  echo "error: Docker did not publish the isolated backend port" >&2
  exit 1
fi

echo "Waiting for isolated backend identity check..."
healthy=0
for _ in $(seq 1 60); do
  health="$(docker inspect --format='{{.State.Health.Status}}' "$app_container" 2>/dev/null || true)"
  if [[ "$health" == "healthy" ]]; then
    healthy=1
    break
  fi
  if [[ "$health" == "unhealthy" ]]; then
    docker inspect --format='{{range .State.Health.Log}}{{println .Output}}{{end}}' \
      "$app_container" >&2 || true
    compose logs --tail 200 app
    echo "error: isolated test backend is unhealthy" >&2
    exit 1
  fi
  sleep 2
done
if [[ "$healthy" != "1" ]]; then
  compose logs --tail 200 app
  echo "error: timed out waiting for the isolated test backend" >&2
  exit 1
fi

version="$(curl -fsS "http://127.0.0.1:${test_port}/api/version")"
python3 -c '
import json, sys
value = json.loads(sys.argv[1])
expected = sys.argv[2]
errors = []
if value.get("test_runtime") is not True:
    errors.append("test_runtime is not true")
serving_source = value.get("serving_source")
if serving_source != "platform":
    errors.append(f"serving_source={serving_source!r}")
frontend_source = value.get("frontend_source")
if frontend_source != "platform":
    errors.append(f"frontend_source={frontend_source!r}")
for field in ("sha", "served_sha", "platform_sha"):
    if value.get(field) != expected:
        errors.append(f"{field}={value.get(field)!r}, want {expected}")
if errors:
    raise SystemExit("refusing browser run: " + "; ".join(errors))
' "$version" "$head_sha"

echo "Running focused Playwright checks with one worker..."
cd "$snapshot_dir"
CI= \
MOBIUS_LOCAL_E2E=1 \
MOBIUS_AUTH_FILE="$auth_file" \
MOBIUS_URL="http://127.0.0.1:${test_port}" \
MOBIUS_USER=admin \
MOBIUS_PASS=admin \
  "$snapshot_dir/node_modules/.bin/playwright" test "$@" --workers=1
