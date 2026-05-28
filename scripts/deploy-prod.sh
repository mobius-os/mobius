#!/usr/bin/env bash
# deploy-prod.sh — One-command prod deploy for the mobius container.
#
# Collapses the 4-step refresh recipe (build image → recreate container →
# refresh /data/shell/{src,dist} → verify) into a single script so future
# deploys don't have to be reconstructed from memory.
#
# The `/data/shell/dist/` masking gotcha is the headline reason this
# script exists: the data volume persists across `docker compose build
# && up -d`, so the new image's /app/static/ is shadowed by the old
# /data/shell/dist/ until we copy fresh sources in and rerun
# rebuild_shell.sh. See "Agent refresh" + "Frontend serving priority"
# in mobius/CLAUDE.md for the gory details.
#
# Usage:
#   scripts/deploy-prod.sh                  # full deploy (build, recreate, refresh shell, verify)
#   scripts/deploy-prod.sh --skip-build     # skip docker compose build (useful when image is already current)
#   scripts/deploy-prod.sh --yes            # don't prompt before `docker compose build`
#   scripts/deploy-prod.sh --target=test    # redirect to mobius-test (port 8001) instead of prod
#   scripts/deploy-prod.sh --check          # verify-only: bundle hash, internal health, public health
#
# Safety: only the `docker compose build` step prompts (it's slow and
# has OOM'd this 7.6GB host before). Everything else auto-proceeds.

set -euo pipefail

# ── target selection ────────────────────────────────────────────────────
TARGET="prod"
SKIP_BUILD=0
ASSUME_YES=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --target=prod) TARGET="prod" ;;
    --target=test) TARGET="test" ;;
    --skip-build)  SKIP_BUILD=1 ;;
    -y|--yes)      ASSUME_YES=1 ;;
    --check)       CHECK_ONLY=1 ;;
    -h|--help)
      sed -n '1,/^set -euo pipefail/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

if [ "$TARGET" = "prod" ]; then
  CONTAINER="mobius"
  COMPOSE_ARGS=()                       # default compose project + file
  INTERNAL_BASE="http://localhost:8000"  # checked via `docker exec curl`
  PUBLIC_URL="https://mobius.hamzamerzic.info/api/health"
else
  CONTAINER="mobius-test"
  COMPOSE_ARGS=(-p mobius-test -f docker-compose.test.yml)
  INTERNAL_BASE="http://localhost:8000"
  PUBLIC_URL=""                          # no public URL for the test container
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── ANSI colors (kept simple, matches sync-test-shell.sh's restraint) ──
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
  C_RESET=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_RESET=""
fi

CURRENT_STEP=""
on_err() {
  local rc=$?
  if [ -n "$CURRENT_STEP" ]; then
    echo "${C_RED}FAILED at step: ${CURRENT_STEP} (exit ${rc})${C_RESET}" >&2
  else
    echo "${C_RED}FAILED (exit ${rc})${C_RESET}" >&2
  fi
  exit "$rc"
}
trap on_err ERR

step()  { CURRENT_STEP="$1"; printf '\n%s[%s] %s%s\n' "$C_BOLD$C_BLUE" "$(date +%H:%M:%S)" "$1" "$C_RESET"; }
info()  { printf '  %s\n' "$1"; }
warn()  { printf '  %s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
ok()    { printf '  %s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
fail()  { printf '  %s%s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }

# Print the destructive command we're about to run so the operator
# always knows what's happening before it happens.
intent() { printf '  %s→ %s%s\n' "$C_DIM" "$1" "$C_RESET"; }

# Default-yes confirm (Enter = yes). Used only for `docker compose build`.
confirm_yes() {
  local prompt="$1"
  if [ "$ASSUME_YES" = "1" ]; then
    info "auto-confirmed (--yes): $prompt"
    return 0
  fi
  if [ ! -t 0 ]; then
    info "non-interactive stdin; auto-confirming: $prompt"
    return 0
  fi
  local reply
  printf '  %s [Y/n] ' "$prompt"
  read -r reply || reply=""
  case "${reply,,}" in
    ''|y|yes) return 0 ;;
    *)        return 1 ;;
  esac
}

# Parse the build-cache size out of `docker system df` and return GB as
# an integer (rounded down). Returns 0 on parse failure so we don't
# accidentally prune on a malformed line.
build_cache_gb() {
  local line size unit
  line=$(docker system df 2>/dev/null | awk '/^Build Cache/ {print; exit}') || true
  if [ -z "$line" ]; then echo 0; return; fi
  # Columns: "Build Cache  <total>  <active>  <size>  <reclaimable>"
  # Size column is e.g. "9.669GB" or "512MB". Grab the 4th whitespace-token.
  size=$(echo "$line" | awk '{print $4}')
  unit=$(echo "$size" | grep -oE '[A-Za-z]+$' || echo "")
  local num
  num=$(echo "$size" | grep -oE '^[0-9.]+' || echo "0")
  case "$unit" in
    GB|GiB) printf '%.0f\n' "$num" ;;
    TB|TiB) printf '%.0f\n' "$(echo "$num * 1024" | bc -l)" ;;
    MB|MiB|KB|KiB|B) echo 0 ;;
    *)      echo 0 ;;
  esac
}

# Pull the served bundle filename out of the index.html the container is
# currently serving. Empty if the container is down or has no bundle.
served_bundle() {
  docker exec "$CONTAINER" sh -c "curl -fsS '${INTERNAL_BASE}/' 2>/dev/null" \
    | grep -oE 'index-[A-Za-z0-9_-]+\.js' \
    | head -n1 || true
}

# ── --check shortcut: verification-only, no deploy ─────────────────────
if [ "$CHECK_ONLY" = "1" ]; then
  step "[check] verifying ${CONTAINER}"
  hash=$(served_bundle)
  info "bundle: ${hash:-<none>}"
  code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
  info "internal /api/health: ${code}"
  if [ -n "$PUBLIC_URL" ]; then
    pcode=$(curl -sk -o /dev/null -w '%{http_code}\n' "$PUBLIC_URL" || echo "000")
    info "public  /api/health: ${pcode}  (${PUBLIC_URL})"
  fi
  exit 0
fi

# ── prod guardrail ──────────────────────────────────────────────────────
# Refuse to run if the configured CONTAINER name isn't actually running.
# (`mobius` is the default; an operator targeting a renamed container
# would otherwise silently no-op or pick up the wrong thing.)
step "[0/4] checking ${CONTAINER} is reachable"
intent "docker inspect ${CONTAINER}"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  fail "container '${CONTAINER}' is not running"
  fail "this script only refreshes an existing container; bring it up first with:"
  if [ "$TARGET" = "prod" ]; then
    fail "  docker compose up -d"
  else
    fail "  docker compose -p mobius-test -f docker-compose.test.yml up -d"
  fi
  exit 1
fi
before_hash=$(served_bundle)
info "current bundle: ${before_hash:-<none>}"
info "target: ${C_BOLD}${TARGET}${C_RESET} (${CONTAINER})"

# ── step 1: build (with cache-prune guard) ─────────────────────────────
if [ "$SKIP_BUILD" = "1" ]; then
  step "[1/4] SKIPPED docker compose build (--skip-build)"
else
  step "[1/4] docker compose build"
  cache_gb=$(build_cache_gb)
  info "current build cache: ~${cache_gb}GB"
  if [ "$cache_gb" -ge 6 ]; then
    warn "build cache ≥ 6GB; prior runs have OOM'd this 7.6GB host."
    intent "docker builder prune -af --filter \"until=24h\""
    docker builder prune -af --filter "until=24h" >/dev/null
    ok "build cache pruned"
  fi
  intent "docker compose ${COMPOSE_ARGS[*]} build"
  if ! confirm_yes "${C_YELLOW}slow step (5-15 min, has OOM'd before).${C_RESET} proceed?"; then
    fail "aborted by user at build step"
    exit 1
  fi
  docker compose "${COMPOSE_ARGS[@]}" build
  ok "image rebuilt"
fi

# ── step 2: recreate container with the new image ──────────────────────
step "[2/4] docker compose up -d (recreates ${CONTAINER})"
intent "docker compose ${COMPOSE_ARGS[*]} up -d"
docker compose "${COMPOSE_ARGS[@]}" up -d
info "waiting up to 30s for ${INTERNAL_BASE}/api/health"
for i in $(seq 1 30); do
  code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
  if [ "$code" = "200" ]; then
    ok "healthy after ${i}s"
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    fail "health check never returned 200 (last: ${code})"
    exit 1
  fi
done

# ── step 3: refresh /data/shell/{src,dist} so new bundle isn't masked ──
# The data volume survives the container recreation. /data/shell/dist/
# is whatever the agent last built, NOT the new image's /app/static/.
# main.py picks _live_dir over _baked_dir at module load, so without
# this step uvicorn keeps serving the stale dist.
step "[3/4] refresh /data/shell/ from new image's /app/shell-src"
intent "docker exec ${CONTAINER} cp -a /app/shell-src/. /data/shell/"
docker exec "$CONTAINER" cp -a /app/shell-src/. /data/shell/
intent "docker exec ${CONTAINER} rm -rf /data/shell/dist"
docker exec "$CONTAINER" rm -rf /data/shell/dist
intent "docker exec ${CONTAINER} bash /app/scripts/rebuild_shell.sh"
docker exec "$CONTAINER" bash /app/scripts/rebuild_shell.sh

# main.py resolves _static_dir at module load time, so the freshly
# rebuilt /data/shell/dist isn't actually served until uvicorn
# restarts. See "Shell rebuild + static-dir resolution" in CLAUDE.md.
intent "docker restart ${CONTAINER}  # so main.py re-resolves _static_dir"
docker restart "$CONTAINER" >/dev/null
info "waiting up to 30s for ${INTERNAL_BASE}/api/health after restart"
for i in $(seq 1 30); do
  code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
  if [ "$code" = "200" ]; then
    ok "healthy after ${i}s"
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    fail "post-restart health check never returned 200 (last: ${code})"
    exit 1
  fi
done

# ── step 4: verify bundle rotated + endpoints respond ──────────────────
step "[4/4] verify"
after_hash=$(served_bundle)
info "before: ${before_hash:-<none>}"
info "after:  ${after_hash:-<none>}"
if [ -z "$after_hash" ]; then
  fail "could not parse bundle hash from ${INTERNAL_BASE}/ — check manually"
  exit 1
fi
# Hash match is OK only if the bundle genuinely didn't change. After a
# rebuild we expect rotation; same hash means either rebuild_shell.sh
# silently failed or the source on disk was identical to what was
# already in dist. Fail loud — the operator can re-run with --skip-build
# if they truly meant "no-op deploy".
if [ -n "$before_hash" ] && [ "$before_hash" = "$after_hash" ]; then
  fail "bundle hash unchanged (${after_hash}). expected rotation after rebuild."
  fail "either rebuild_shell.sh produced an identical bundle, or the rebuild was a no-op."
  fail "if you meant to redeploy without a frontend change, rerun with --skip-build."
  exit 1
fi
ok "bundle rotated: ${before_hash:-<none>} → ${after_hash}"

# Internal /api/health (we already checked this twice during waits, but
# repeat it in the verification block so the final summary stands alone).
code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
if [ "$code" = "200" ]; then
  ok "internal /api/health: ${code}"
else
  fail "internal /api/health: ${code}"
  exit 1
fi

# Public endpoint check (prod only). -k so a momentarily-expired cert
# doesn't mask a healthy backend; we're checking app reachability, not
# TLS chain validity. If the operator cares about cert health, that's a
# separate concern.
if [ -n "$PUBLIC_URL" ]; then
  pcode=$(curl -sk -o /dev/null -w '%{http_code}\n' "$PUBLIC_URL" || echo "000")
  if [ "$pcode" = "200" ]; then
    ok "public  /api/health: ${pcode}  (${PUBLIC_URL})"
  else
    fail "public  /api/health: ${pcode}  (${PUBLIC_URL})"
    fail "internal is healthy but public reverse-proxy returned non-200; check Caddy."
    exit 1
  fi
fi

printf '\n%sdeploy complete%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
