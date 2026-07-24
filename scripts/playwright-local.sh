#!/usr/bin/env bash
# Explicit, host-only Playwright runner over one committed, disposable revision.

set -euo pipefail

if [[ "${1:-}" != "--allow-local-e2e" ]]; then
  cat >&2 <<'EOF'
Local browser E2E is intentionally opt-in: it builds a full disposable
Möbius stack and can take several minutes. Prefer the GitHub PR checks.

Run this on a Docker-capable host, not inside the Möbius app container:
  scripts/playwright-local.sh --allow-local-e2e <spec or --grep arguments>

Focused checks use one browser worker by default. Set
MOBIUS_LOCAL_E2E_WORKERS=2 to reproduce hosted-CI concurrency.

The runner deletes its test image by default. Set
MOBIUS_LOCAL_E2E_KEEP_CACHE=1 only for an intentionally retained per-checkout
image; BuildKit already owns the ordinary build cache.

Runs are serialized across this user's worktrees because concurrent full-stack
builds can exhaust the Docker host. A second run waits for the shared slot;
MOBIUS_LOCAL_E2E_ADMISSION_WAIT (default 1800s) bounds that wait.
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

# The disposable runtime intentionally serves a committed tree. Refuse source
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

# The checkout-scoped lock above prevents duplicate runs in one tree. This
# second gate closes the cross-worktree race: without it, several checkouts can
# all pass the same free-space snapshot and then consume multiple GiB in
# parallel. Keep the lock outside TMPDIR because per-session temp directories
# would give each run an independent gate. XDG_RUNTIME_DIR is private to the
# current user; the UID suffix keeps the /tmp fallback isolated on multi-user
# hosts.
admission_lock="${XDG_RUNTIME_DIR:-/tmp}/mobius-local-e2e-admission-${UID}.lock"
admission_wait="${MOBIUS_LOCAL_E2E_ADMISSION_WAIT:-1800}"
if [[ ! "$admission_wait" =~ ^[0-9]+$ ]]; then
  echo "error: MOBIUS_LOCAL_E2E_ADMISSION_WAIT must be a non-negative integer" >&2
  exit 2
fi
exec 8>"$admission_lock"
if ! flock -n 8; then
  echo "another checkout holds the local E2E build slot; waiting up to ${admission_wait}s..."
  if ! flock -w "$admission_wait" 8; then
    cat >&2 <<EOF
error: timed out after ${admission_wait}s waiting for the local E2E build slot.
Another checkout is still building. Wait for it to finish, or raise
MOBIUS_LOCAL_E2E_ADMISSION_WAIT. Runs are serialized to protect shared Docker
disk capacity.
EOF
    exit 2
  fi
fi

run_id="$(date +%s)-$$"
project="mobius-local-e2e-${run_id}"
image_name="${project}:test"
# Keep one bounded image reference per checkout after a run. Checkout scoping
# prevents concurrent worktrees from replacing each other's dependency cache,
# while the stable numeric suffix keeps the default tag Docker-safe.
checkout_id="$(printf '%s' "$ROOT" | cksum | awk '{print $1}')"
cache_image="${MOBIUS_LOCAL_E2E_CACHE_IMAGE:-mobius-local-e2e-cache-${checkout_id}:test}"
keep_cache="${MOBIUS_LOCAL_E2E_KEEP_CACHE:-0}"
if [[ "$keep_cache" != "0" && "$keep_cache" != "1" ]]; then
  echo "error: MOBIUS_LOCAL_E2E_KEEP_CACHE must be 0 or 1" >&2
  exit 2
fi
e2e_workers="${MOBIUS_LOCAL_E2E_WORKERS:-1}"
if [[ ! "$e2e_workers" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: MOBIUS_LOCAL_E2E_WORKERS must be a positive integer" >&2
  exit 2
fi
min_free_gb="${MOBIUS_LOCAL_E2E_MIN_FREE_GB:-20}"
if [[ ! "$min_free_gb" =~ ^[0-9]+$ ]]; then
  echo "error: MOBIUS_LOCAL_E2E_MIN_FREE_GB must be a non-negative integer" >&2
  exit 2
fi
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
disk_path="${docker_root:-$ROOT}"
available_kb="$(df -Pk "$disk_path" | awk 'NR == 2 {print $4}')"
minimum_kb="$((min_free_gb * 1024 * 1024))"
if (( available_kb < minimum_kb )); then
  available_gb="$((available_kb / 1024 / 1024))"
  cat >&2 <<EOF
error: isolated local E2E needs at least ${min_free_gb} GiB free for Docker;
only ${available_gb} GiB is available on $disk_path.
Review usage with 'docker system df'. Remove unused build cache or set
MOBIUS_LOCAL_E2E_MIN_FREE_GB=0 only when this checkout already completed a
cached build that is known to fit.
EOF
  exit 2
fi
# Retention is opt-in. Remove a legacy cache tag from this checkout before the
# build when the owner did not request it; image removal is reference-aware and
# fails harmlessly if a container still uses the image.
if [[ "$keep_cache" == "0" ]]; then
  docker image rm "$cache_image" >/dev/null 2>&1 || true
fi
app_container="${project}-app"
caddy_container="${project}-caddy"
recovery_container="${project}-recoveryd"
free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}
test_publish_port="${TEST_PORT:-$(free_port)}"
internal_publish_port="${INTERNAL_TEST_PORT:-$(free_port)}"
recovery_publish_port="${RECOVERY_TEST_PORT:-$(free_port)}"
run_dir="$(mktemp -d "${TMPDIR:-/tmp}/mobius-local-e2e.XXXXXX")"
snapshot_dir="$run_dir/source"
auth_file="$run_dir/auth-state.json"
compose_used=0

compose() {
  # A committed local revision may not be fetchable from the public origin
  # during the Docker build. The runtime identity gate below seeds the exact
  # standalone snapshot, so keep the baked fallback unstamped for local runs.
  MOBIUS_CONTAINER="$app_container" \
  MOBIUS_CADDY_CONTAINER="$caddy_container" \
  MOBIUS_RECOVERYD_CONTAINER="$recovery_container" \
  MOBIUS_IMAGE="$image_name" \
  TEST_PORT="$test_publish_port" \
  INTERNAL_TEST_PORT="$internal_publish_port" \
  RECOVERY_TEST_PORT="$recovery_publish_port" \
  BUILD_SHA=unknown \
  GITHUB_SHA="$head_sha" \
    docker compose -p "$project" -f "$snapshot_dir/docker-compose.test.yml" \
      --project-directory "$snapshot_dir" "$@"
}

cleanup() {
  if [[ "$compose_used" == "1" ]]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    # Refresh the bounded tag after Compose releases its containers. Doing
    # this again at teardown keeps the cache reference authoritative even if
    # a failed run or an engine cleanup dropped the earlier tag.
    if [[ "$keep_cache" == "1" ]]; then
      docker image tag "$image_name" "$cache_image" >/dev/null 2>&1 || true
    fi
    docker image rm "$image_name" >/dev/null 2>&1 || true
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
if [[ "$keep_cache" == "1" ]]; then
  docker image tag "$image_name" "$cache_image"
  echo "Retained bounded build cache as $cache_image"
else
  echo "Build cache retention disabled for this run."
fi
if ! compose up -d; then
  compose logs --tail 200 app caddy recoveryd || true
  echo "error: isolated test stack failed to start" >&2
  exit 1
fi
test_port="$(docker port "$caddy_container" "$test_publish_port/tcp" | tail -1 | awk -F: '{print $NF}')"
internal_test_port="$(docker port "$app_container" 8000/tcp | tail -1 | awk -F: '{print $NF}')"
recovery_test_port="$(docker port "$recovery_container" 8001/tcp | tail -1 | awk -F: '{print $NF}')"
if [[ -z "$test_port" || -z "$internal_test_port" || -z "$recovery_test_port" ]]; then
  echo "error: Docker did not publish the isolated proxy, backend, and recovery ports" >&2
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

version=""
for _ in $(seq 1 30); do
  if version="$(curl -fsS "http://localhost:${test_port}/api/version" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
if [[ -z "$version" ]]; then
  compose logs --tail 200 caddy || true
  echo "error: timed out waiting for isolated browser proxy" >&2
  exit 1
fi
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

echo "Running focused Playwright checks with ${e2e_workers} worker(s)..."
cd "$snapshot_dir"
if CI= \
   MOBIUS_LOCAL_E2E=1 \
   MOBIUS_AUTH_FILE="$auth_file" \
   MOBIUS_URL="http://localhost:${test_port}" \
   MOBIUS_RECOVER_URL="http://localhost:${recovery_test_port}" \
   MOBIUS_TEST_INTERNAL_API="http://127.0.0.1:${internal_test_port}" \
   MOBIUS_USER=admin \
   MOBIUS_PASS=admin \
     "$snapshot_dir/node_modules/.bin/playwright" test "$@" --workers="$e2e_workers"; then
  exit 0
else
  test_rc=$?
fi

# The snapshot is deleted during cleanup, so retain the browser context and
# stack logs before exiting. The default lives under an ignored repository
# path; callers can redirect it to a durable CI/debug directory.
artifact_dir="${MOBIUS_LOCAL_E2E_ARTIFACTS:-$ROOT/test-results/local-e2e-$run_id}"
mkdir -p "$artifact_dir"
for result_dir in test-results playwright-report; do
  if [[ -e "$snapshot_dir/$result_dir" ]]; then
    cp -a "$snapshot_dir/$result_dir" "$artifact_dir/"
  fi
done
compose logs --no-color app caddy recoveryd fake-tandoor \
  >"$artifact_dir/stack.log" 2>&1 || true
echo "Local E2E artifacts retained at: $artifact_dir" >&2
exit "$test_rc"
