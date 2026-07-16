#!/usr/bin/env bash
# deploy-prod.sh — One-command prod deploy for the mobius container.
#
# Collapses the deploy recipe (build image -> recreate container -> rebuild the
# served platform frontend -> verify) into a single script so future deploys
# don't have to be reconstructed from memory.
#
# Usage:
#   scripts/deploy-prod.sh                  # full deploy (build, recreate, rebuild frontend, verify)
#   scripts/deploy-prod.sh --skip-build     # skip docker compose build (useful when image is already current)
#   scripts/deploy-prod.sh --yes            # don't prompt before `docker compose build`
#   scripts/deploy-prod.sh --target=test    # redirect to mobius-test (port 8001) instead of prod
#   scripts/deploy-prod.sh --check          # verify-only: bundle hash, internal health, public health
#   scripts/deploy-prod.sh --allow-unpushed # emergency hotfix: deploy a commit not yet on origin/main
#                                           # (downgrades the default refusal + dirty-tree abort to a warning;
#                                           #  push it to main ASAP or the next deploy-from-main reverts it)
#   scripts/deploy-prod.sh --allow-stale    # deliberate rollback: deploy a prod checkout BEHIND origin/main
#                                           # (bypasses the behind-origin/main hard block — only for an
#                                           #  intentional rollback to an older main; you are reverting newer work)
#   scripts/deploy-prod.sh --force-now      # skip the owner-presence gate (deploy even mid-conversation)
#
# Env knobs (seconds; both default 120):
#   PREFLIGHT_WAIT_SECONDS  how long the scratch-container preflight waits for boot
#   CUTOVER_WAIT_SECONDS    how long the LIVE cutover health/ready checks wait
#                           before rolling back. Raise it on a memory-thrashing
#                           host so a slow-but-healthy boot doesn't false-fail
#                           (e.g. CUTOVER_WAIT_SECONDS=240 scripts/deploy-prod.sh).
#
# Safety: only the `docker compose build` step prompts (it's slow and
# has OOM'd this 7.6GB host before). Everything else auto-proceeds.

set -euo pipefail

# ── target selection ────────────────────────────────────────────────────
TARGET="prod"
SKIP_BUILD=0
ASSUME_YES=0
# Owner-presence gate: before a prod recreate/restart (which 502s any live turn
# and can drop the owner's in-flight message), wait for an active turn to finish.
# --force-now / FORCE_NOW=1 skips it; PRESENCE_WAIT_SECONDS caps the wait.
FORCE_NOW="${FORCE_NOW:-0}"
PRESENCE_WAIT_SECONDS="${PRESENCE_WAIT_SECONDS:-90}"
CHECK_ONLY=0
# Default-refuse to deploy a commit that isn't on origin/main: a deploy from an
# unpushed commit ships code the next deploy-from-main silently REVERTS (the
# "deployed-but-unpushed → reverted" class — see push-deploy-to-main lesson).
# The escape hatch (--allow-unpushed / ALLOW_UNPUSHED=1) downgrades the abort to
# a loud warning for a deliberate emergency hotfix: empower with an explicit
# override, safe-by-default — not a hard wall.
ALLOW_UNPUSHED="${ALLOW_UNPUSHED:-0}"
# The mirror-image refusal: deploy-prod builds from the WORKING TREE, so a
# checkout that is BEHIND origin/main bakes a STALE image and silently REVERTS
# everyone's pushed work (the served frontend regresses to an old bundle). This
# is the recurring "sibling deployed from a stale checkout" prod incident. We
# HARD-BLOCK a strictly-behind checkout (exit 2). --allow-stale / ALLOW_STALE=1
# is the escape hatch for a DELIBERATE rollback to an older main — same
# empower-with-an-explicit-override shape as --allow-unpushed.
ALLOW_STALE="${ALLOW_STALE:-0}"
BUILT_THIS_RUN=0  # set to 1 once we actually build, so the verify step only
                  # compares the served SHA when THIS run produced the image
PREFLIGHT_WAIT_SECONDS="${PREFLIGHT_WAIT_SECONDS:-120}"
# How long the LIVE cutover health/ready checks wait before giving up and
# rolling back. Same knob shape as PREFLIGHT_WAIT_SECONDS. On the memory-tight
# 7.6GB host, a freshly-recreated container with a populated /data volume can
# take well over a minute to bind uvicorn when the build's memory hasn't fully
# released and sibling containers are thrashing — a slow-but-healthy boot, not
# a crash. The window must outlast that stall, or the deploy false-fails and
# rolls back an image that was about to come up fine. Override upward on a
# heavily-loaded host (e.g. CUTOVER_WAIT_SECONDS=240).
CUTOVER_WAIT_SECONDS="${CUTOVER_WAIT_SECONDS:-120}"
# How many restarts above the baseline mean a genuine crash-loop. This
# memory-thrashing 7.6GB host can OOM-kill a freshly-recreated container ONCE
# and self-heal on Docker's automatic restart — a single restart is not proof
# of a crash-loop, just of the host being tight. Require at least this many
# extra restarts before rolling back, so a one-off OOM bounce doesn't abort a
# deploy that was about to come up fine (card 116). 2 = the container must die
# and be restarted twice within the cutover window.
CRASH_RESTART_THRESHOLD="${CRASH_RESTART_THRESHOLD:-2}"
# Both wait knobs feed `seq 1 "$N"` loops; an empty, zero, negative, or
# unit-suffixed value (e.g. "240s", which arithmetic reads as 240… or, in a
# `[ -eq ]`, errors) yields an EMPTY sequence — the loop body never runs, the
# health probe is skipped entirely, and the deploy false-fails into an instant
# rollback that looks like a boot failure. Reject anything that isn't a bare
# positive integer up front, where the operator can see and fix it (card 116).
for _knob in PREFLIGHT_WAIT_SECONDS CUTOVER_WAIT_SECONDS PRESENCE_WAIT_SECONDS; do
  case "${!_knob}" in
    ''|*[!0-9]*|0)
      printf 'deploy-prod: %s=%q must be a positive integer (seconds)\n' \
        "$_knob" "${!_knob}" >&2
      exit 2
      ;;
  esac
done
unset _knob
for arg in "$@"; do
  case "$arg" in
    --target=prod) TARGET="prod" ;;
    --target=test) TARGET="test" ;;
    --skip-build)  SKIP_BUILD=1 ;;
    -y|--yes)      ASSUME_YES=1 ;;
    --check)       CHECK_ONLY=1 ;;
    --allow-unpushed) ALLOW_UNPUSHED=1 ;;
    --allow-stale) ALLOW_STALE=1 ;;
    --force-now)   FORCE_NOW=1 ;;
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
  # Pin the compose project to `mobius`. Without this, the project name
  # defaults to the cwd directory — so running this from a git worktree
  # gives project `<slug>` instead of `mobius`, which then collides on the
  # fixed `container_name: mobius` and creates junk `<slug>_*` volumes.
  # (A real incident: a worktree deploy spawned substrate-freshness_* vols
  # + a name conflict.) Pinning makes it correct from any cwd.
  COMPOSE_ARGS=(-p mobius)
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

# Is the owner mid-conversation? True iff /api/debug/status shows any SDK client,
# SDK session, starting chat, or running broadcast. Probed from INSIDE the
# container (:8000 isn't host-published) with the owner service token, bounded by
# `timeout` + curl --max-time so a hung backend can NEVER wedge the deploy. Any
# probe failure — unreachable, slow, OR auth (a stale service-token after a
# token-epoch revocation returns 401 with no fields) — returns "not in flight":
# the gate is politeness (durable replay is the real guarantee), so it fails open
# rather than block on an unverifiable probe.
owner_turn_in_flight() {
  timeout 6 docker exec "$CONTAINER" sh -c '
    tok=$(cat /data/service-token.txt 2>/dev/null) || exit 1
    curl -s --connect-timeout 1 --max-time 3 -H "Authorization: Bearer $tok" http://localhost:8000/api/debug/status 2>/dev/null
  ' 2>/dev/null | python3 -c '
import sys, json
try:
  d = json.load(sys.stdin)
except Exception:
  sys.exit(1)
n = (len(d.get("active_sdk_clients") or [])
     + len(d.get("active_sdk_sessions") or [])
     + len(d.get("starting") or [])
     + sum(1 for b in (d.get("broadcasts") or []) if isinstance(b, dict) and b.get("running")))
sys.exit(0 if n > 0 else 1)
' 2>/dev/null
}

# Defer a disruptive prod action until the owner's live turn finishes. Advisory:
# waits up to PRESENCE_WAIT_SECONDS, then aborts with a clear --force-now hint —
# never an irreversible block ("code empowers, does not police").
presence_gate() {
  [ "$TARGET" = "prod" ] || return 0
  if [ "$FORCE_NOW" = "1" ]; then warn "--force-now: skipping owner-presence gate before $1"; return 0; fi
  local waited=0
  while owner_turn_in_flight; do
    if [ "$waited" -ge "$PRESENCE_WAIT_SECONDS" ]; then
      warn "owner still has a live turn after ${PRESENCE_WAIT_SECONDS}s — $1 would 502 it (and may drop the in-flight message)."
      warn "Wait for the turn to finish, or re-run with --force-now to proceed anyway."
      fail "deferred: $1 (owner turn in flight)"; exit 1
    fi
    [ "$waited" = 0 ] && info "owner has a live turn — deferring $1 up to ${PRESENCE_WAIT_SECONDS}s for it to finish…"
    sleep 5; waited=$((waited + 5))
  done
  [ "$waited" -gt 0 ] && ok "owner turn finished — proceeding with $1"
  return 0
}

source_env_file() {
  local file="$1"
  if [ ! -f "$file" ]; then return 1; fi
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  return 0
}

ensure_prod_env() {
  if [ "$TARGET" != "prod" ]; then return 0; fi
  if [ -n "${DOMAIN:-}" ]; then return 0; fi

  if source_env_file "$REPO_ROOT/.env"; then
    info "loaded prod env from $REPO_ROOT/.env"
  fi
  if [ -n "${DOMAIN:-}" ]; then return 0; fi

  # Worktrees normally do not carry the canonical checkout's .env, but docker
  # compose still interpolates DOMAIN before it touches the live container.
  # Resolve the shared git dir back to the canonical checkout and source its
  # .env when available so a worktree deploy cannot recreate prod with
  # FRONTEND_ORIGIN=https://.
  local git_common canonical_root
  git_common=$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$git_common" ]; then
    canonical_root="$(dirname "$git_common")"
    if [ "$canonical_root" != "$REPO_ROOT" ] &&
       source_env_file "$canonical_root/.env"; then
      info "loaded prod env from canonical checkout $canonical_root/.env"
    fi
  fi
  if [ -z "${DOMAIN:-}" ]; then
    fail "DOMAIN is empty; refusing to deploy prod because FRONTEND_ORIGIN would be invalid."
    fail "Set DOMAIN in the environment, $REPO_ROOT/.env, or the canonical checkout .env."
    exit 1
  fi
}

ensure_prod_env

# ── proxy topology — sampled ONCE, then frozen ──────────────────────────
# The shared edge proxy (its own compose stack; see the edge repo's README)
# owns ports 80/443 on this host. When it is running, this repo's bundled
# caddy service must NOT start, app + recoveryd join the external edge-mobius
# network via the docker-compose.prod.yml overlay (declared on the service, so
# every recreate carries the membership — no post-recreate repair hooks), and
# this deploy installs the rendered edge fragment below.
#
# Frozen up front because every later decision — overlay files in
# COMPOSE_ARGS, cutover service selection, rollback service selection,
# fragment install — must see ONE consistent topology. Re-sampling live
# docker state at each call site would let an edge restart mid-deploy flip
# the answers and, e.g., start the bundled caddy against edge-owned ports.
EDGE_TOPOLOGY="self-hosted"
if [ "$TARGET" = "prod" ]; then
  EDGE_CONTAINER_STATE=$(
    docker inspect -f '{{.State.Status}}' edge-caddy 2>/dev/null || true
  )
  case "$EDGE_CONTAINER_STATE" in
    running)
      EDGE_TOPOLOGY="edge"
      COMPOSE_ARGS+=(-f docker-compose.yml -f docker-compose.prod.yml)
      ;;
    "")
      if docker inspect -f '{{.State.Running}}' deploy-caddy-1 2>/dev/null | grep -q true; then
        fail "the retired legacy proxy (deploy-caddy-1) still owns ports 80/443."
        fail "This script now deploys behind the shared edge proxy (edge-caddy);"
        fail "run the edge cutover first, or stop deploy-caddy-1 to self-host."
        exit 1
      fi
      ;;
    *)
      # An existing edge stack defines this host's topology even while it is
      # unhealthy. Starting the bundled Caddy in that window would silently
      # seize the edge-owned ports and replace every other routed service.
      fail "edge-caddy exists but is ${EDGE_CONTAINER_STATE}; refusing to fall back to bundled Caddy."
      fail "Restore the shared edge proxy, then re-run the deploy."
      exit 1
      ;;
  esac
fi

external_prod_caddy_running() {
  [ "$TARGET" = "prod" ] && [ "$EDGE_TOPOLOGY" = "edge" ]
}

# External networks referenced by the overlay must exist before compose up.
# Creation is idempotent and safe: the edge stack declares the same name as
# external, so whichever side runs first wins and both attach to one network.
ensure_edge_network() {
  docker network inspect edge-mobius >/dev/null 2>&1 && return 0
  intent "docker network create edge-mobius"
  docker network create edge-mobius >/dev/null
}

valid_gateway_origin() {
  local origin="$1" authority port
  # Keep the render substitution inert: one HTTPS DNS/IPv4 authority, with an
  # optional numeric port, and no credentials, path, query, fragment, control
  # characters, or sed delimiter. Caddy validates the hostname itself later.
  [[ "$origin" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] \
    || return 1
  authority=${origin#https://}
  if [[ "$authority" == *:* ]]; then
    port=${authority##*:}
    (( 10#$port >= 1 && 10#$port <= 65535 )) || return 1
  fi
}

# The bundled Caddyfile is the single routing source of truth for BOTH
# topologies: the self-host compose runs it directly with runtime env, and the
# shared-edge topology installs the same file with the {$VAR} placeholders
# rendered here (the edge proxy carries no Möbius env on purpose). Rendering
# rather than mounting keeps prod at permanent parity with the file CI
# validates — the pre-edge setup drifted for months because the external proxy
# had its own hand-written copy of these vhosts.
#
# EDGE_CSP_MODE=report-only downgrades the shell Content-Security-Policy to
# Report-Only for a staged rollout (violations appear in the browser console
# without breaking the page); default is enforce.
install_edge_fragment() {
  external_prod_caddy_running || return 0
  local edge_dir="${MOBIUS_EDGE_DIR:-$HOME/projects/edge}"
  if [ ! -x "$edge_dir/edgectl" ]; then
    # A missing installer must not silently pass the later public checks
    # against a STALE fragment and misreport new routing/CSP as shipped.
    fail "edge topology detected but $edge_dir/edgectl is missing — cannot install the rendered fragment."
    fail "Set MOBIUS_EDGE_DIR to the edge checkout, or restore it, then re-run."
    exit 1
  fi
  # ensure_prod_env skips .env when DOMAIN is pre-exported, so the gateway
  # origin can be unset here even though the canonical .env carries it. Fall
  # back to that file before concluding the gateway is unconfigured.
  local gw="${MOBIUS_SERVICE_GATEWAY_ORIGIN:-}"
  if [ -z "$gw" ] && [ -f "$REPO_ROOT/.env" ]; then
    gw=$(sed -n 's/^MOBIUS_SERVICE_GATEWAY_ORIGIN=//p' "$REPO_ROOT/.env" | tail -1)
  fi
  case "$DOMAIN" in
    *[!A-Za-z0-9.-]*|"") fail "DOMAIN '$DOMAIN' is not a bare hostname; refusing to render the edge fragment"; exit 1 ;;
  esac
  if [ -n "$gw" ] && ! valid_gateway_origin "$gw"; then
    fail "MOBIUS_SERVICE_GATEWAY_ORIGIN '$gw' is not a bare https origin; refusing to render the edge fragment"
    exit 1
  fi
  local rendered
  rendered=$(mktemp)
  if [ -n "$gw" ]; then
    sed -e "s|{\$DOMAIN}|${DOMAIN}|g" \
        -e "s|{\$MOBIUS_SERVICE_GATEWAY_ORIGIN}|${gw}|g" \
        -e "s|{\$FRONTEND_ORIGIN}|https://${DOMAIN}|g" \
        "$REPO_ROOT/Caddyfile" > "$rendered"
  else
    # No gateway configured: the bundled compose parks the vhost on an inert
    # .invalid label, but the edge installer rightly rejects hostnames the
    # manifest does not assign — so drop the gateway site block entirely
    # (brace-depth walk, since the block nests handlers).
    info "MOBIUS_SERVICE_GATEWAY_ORIGIN not configured — rendering fragment without the gateway vhost"
    sed -e "s|{\$DOMAIN}|${DOMAIN}|g" \
        -e "s|{\$FRONTEND_ORIGIN}|https://${DOMAIN}|g" \
        "$REPO_ROOT/Caddyfile" \
      | awk '
          /^\{\$MOBIUS_SERVICE_GATEWAY_ORIGIN\} \{/ { skip = 1; depth = 0 }
          skip {
            n = gsub(/\{/, "{"); m = gsub(/\}/, "}")
            depth += n - m
            if (depth <= 0) skip = 0
            next
          }
          { print }
        ' > "$rendered"
  fi
  if grep -qF '{$' "$rendered"; then
    fail "rendered edge fragment still contains {\$...} placeholders the render step does not know:"
    grep -nF '{$' "$rendered" | sed 's/^/    /' >&2
    rm -f "$rendered"
    exit 1
  fi
  case "${EDGE_CSP_MODE:-enforce}" in
    enforce) ;;
    report-only)
      sed -i 's|>Content-Security-Policy |>Content-Security-Policy-Report-Only |g' "$rendered"
      info "edge fragment CSP rendered as Report-Only (EDGE_CSP_MODE=report-only)"
      ;;
    *)
      rm -f "$rendered"
      fail "EDGE_CSP_MODE must be 'enforce' or 'report-only'."
      exit 1
      ;;
  esac
  # edgectl is transactional: a bad render never replaces the installed
  # fragment, a failed reload restores it, and the previously SERVED fragment
  # stays available as `edgectl rollback mobius`.
  if "$edge_dir/edgectl" install mobius "$rendered"; then
    ok "edge fragment installed (mobius.Caddyfile rendered for ${DOMAIN})"
  else
    rm -f "$rendered"
    fail "edgectl rejected the rendered fragment — public routing unchanged; fix and re-run"
    exit 1
  fi
  rm -f "$rendered"
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

# Pull the served frontend bundle filename out of the index.html the container
# is currently serving. Empty if the container is down or has no bundle.
#
# Probes /shell/ explicitly because the SPA lives under /shell/ since the
# manifest-scope migration (commit e451f01) — `/` 308-redirects there and
# `curl` without `-L` returns a redirect response with no <script> tag.
# We use `-L` so the same code works pre- and post-/shell/ scope.
served_bundle() {
  docker exec "$CONTAINER" sh -c "curl -fsSL '${INTERNAL_BASE}/shell/' 2>/dev/null" \
    | grep -oE 'index-[A-Za-z0-9_-]+\.js' \
    | head -n1 || true
}

# The git commit the SERVED backend reports at /api/version (baked at build
# time via the BUILD_SHA build-arg). Empty if the route is missing; "unknown"
# if the image predates the stamp or the arg wasn't passed. The backend
# analogue of served_bundle (which only sees the frontend).
served_sha() {
  docker exec "$CONTAINER" sh -c "curl -fsS '${INTERNAL_BASE}/api/version' 2>/dev/null" \
    | sed -n 's/.*"sha":"\([^"]*\)".*/\1/p' \
    | head -n1 || true
}

# A single field from /api/version (string OR bool). Used to verify the SERVED
# /data/platform identity (serving_source / platform_dirty) — distinct from the
# IMAGE build sha above (`sha`), which they can disagree with. Empty if missing.
served_version_field() {  # $1 = json key
  docker exec "$CONTAINER" sh -c "curl -fsS '${INTERNAL_BASE}/api/version' 2>/dev/null" \
    | sed -n "s/.*\"$1\":\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" \
    | head -n1 || true
}

# The HTTP status of the writer-aware readiness probe. /api/health is
# liveness only — it returns 200 even when the single-writer chat-persistence
# actor failed to start, went fatal, or is stopping, so a deploy could green
# while every chat write fails. /api/ready returns 200 only when the writer
# can actually persist; "000" if the container is down / curl couldn't reach
# it. The deploy gate fails on anything but 200, so a process that can't
# persist a chat does not pass as deployed.
ready_code() {
  docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/ready'" 2>/dev/null || echo "000"
}

# Tell already-open PWAs to reload after a successful frontend rebuild.
#
# A PWA that was already open never learns a fresh bundle exists — it keeps
# running the old shell until the user closes and reopens it. The Shell's
# system-event stream already handles a
# `shell_rebuilt` event (frontend/src/components/Shell/Shell.jsx — it
# fades out and reloads), and POST /api/notify already broadcasts that
# event type to every open Shell's /api/events/system subscription. The
# only missing link is firing it; deploy never did. We fire it here,
# AFTER the deploy verification gate, so open PWAs auto-reload onto the new
# bundle.
#
# Auth: /api/notify requires an owner JWT. The entrypoint mints a full
# owner-scoped service token at /data/service-token.txt (90-day, carries
# the owner's token_epoch) for exactly this kind of in-container call —
# `get_current_owner` accepts it. A docker-exec curl sends no
# Sec-Fetch-Site / Origin, so reject_cross_site passes it through.
#
# Best-effort: a failure here must not fail an otherwise-healthy deploy —
# the worst case is the pre-fix behaviour (open PWAs reload on next
# manual open). We log a warning and continue.
broadcast_shell_rebuilt() {
  docker exec "$CONTAINER" sh -c '
    tok=$(cat /data/service-token.txt 2>/dev/null) || exit 1
    [ -n "$tok" ] || exit 1
    curl -fsS -o /dev/null \
      -X POST "'"${INTERNAL_BASE}"'/api/notify" \
      -H "Authorization: Bearer $tok" \
      -H "Content-Type: application/json" \
      -d "{\"type\":\"shell_rebuilt\"}"
  ' 2>/dev/null
}

run_deploy_canary() {
  [ "${DEPLOY_CANARY:-0}" = "1" ] || return 0
  info "DEPLOY_CANARY=1: sending throwaway chat turn and waiting for a reply"
  if docker exec "$CONTAINER" sh -c '
    set -eu
    base="'"${INTERNAL_BASE}"'"
    tok=$(cat /data/service-token.txt 2>/dev/null) || exit 2
    [ -n "$tok" ] || exit 2
    chat_id=""
    cleanup() {
      [ -n "${chat_id:-}" ] || return 0
      curl -fsS -o /dev/null \
        -X DELETE "$base/api/chats/$chat_id" \
        -H "Authorization: Bearer $tok" || true
    }
    trap cleanup EXIT
    chat_json=$(curl -fsS \
      -X POST "$base/api/chats" \
      -H "Authorization: Bearer $tok" \
      -H "Content-Type: application/json" \
      -d "{\"title\":\"Deploy canary\",\"messages\":[]}")
    chat_id=$(printf "%s" "$chat_json" | python3 -c "
import json, sys
print(json.load(sys.stdin)[\"id\"])
")
    curl -fsS -o /dev/null \
      -X POST "$base/api/chats/$chat_id/messages" \
      -H "Authorization: Bearer $tok" \
      -H "Content-Type: application/json" \
      -d "{\"content\":\"reply ok\"}"
    for _i in $(seq 1 90); do
      status_json=$(curl -fsS \
        -H "Authorization: Bearer $tok" \
        "$base/api/chats/$chat_id?limit=10")
      if printf "%s" "$status_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get(\"running\"):
  sys.exit(1)
for m in d.get(\"messages\") or []:
  if m.get(\"role\") != \"assistant\":
    continue
  if (m.get(\"content\") or \"\").strip() or m.get(\"blocks\"):
    sys.exit(0)
sys.exit(1)
"; then
        exit 0
      fi
      sleep 1
    done
    exit 1
  '; then
    ok "deploy canary passed — throwaway SDK turn completed"
  else
    docker exec "$CONTAINER" sh -c '
      set -eu
      base="'"${INTERNAL_BASE}"'"
      tok=$(cat /data/service-token.txt 2>/dev/null) || exit 0
      [ -n "$tok" ] || exit 0
      curl -fsS -o /dev/null \
        -X POST "$base/api/notifications/send" \
        -H "Authorization: Bearer $tok" \
        -H "Content-Type: application/json" \
        -d "{\"title\":\"Deploy canary failed\",\"body\":\"Check provider auth, rate/usage limits, /api/debug/status, and container logs.\"}" \
        || true
    ' 2>/dev/null || true
    warn "deploy canary FAILED — deploy remains live, but chat turns may be unhealthy."
    warn "Check provider auth, rate/usage limits, /api/debug/status, and container logs."
  fi
}

# Docker's restart counter for the live container, as an integer. This is the
# real "is the new image actually crash-looping?" signal: the preflight already
# proved the image BOOTS in a scratch box, so during cutover a climbing
# RestartCount means the container died and Docker is restarting it (a genuine
# boot failure → roll back now), whereas a steady count that simply hasn't
# served /api/health yet is a slow-but-healthy boot under memory pressure (keep
# waiting). Returns -1 if inspect fails so a transient inspect error can't be
# mistaken for "no restarts."
container_restart_count() {
  docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo "-1"
}

# ── --check shortcut: verification-only, no deploy ─────────────────────
if [ "$CHECK_ONLY" = "1" ]; then
  step "[check] verifying ${CONTAINER}"
  hash=$(served_bundle)
  info "bundle: ${hash:-<none>}"
  sha=$(served_sha)
  info "backend sha: ${sha:-<none>}"
  code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
  info "internal /api/health: ${code}"
  rcode=$(ready_code)
  info "internal /api/ready:  ${rcode}"
  if [ -n "$PUBLIC_URL" ]; then
    pcode=$(curl -sk -o /dev/null -w '%{http_code}\n' "$PUBLIC_URL" || echo "000")
    info "public  /api/health: ${pcode}  (${PUBLIC_URL})"
  fi
  exit 0
fi

# ── deploy lock: serialize concurrent deploys ───────────────────────────
# Multiple Claude sessions run against this repo. Two deploys to the same
# container at once race (recreate clobbers, half-built frontend dist). Take
# a non-blocking per-target flock; the fd stays open for the script's life
# and releases on exit. (--check above skips this — it doesn't deploy.)
# Lock in a user-private 0700 dir, not world-writable /tmp — a local
# symlink in /tmp could otherwise redirect the open (fd 9 is opened for
# write). `install -d` guarantees the dir is ours, so dropping the old
# `|| true` is safe: a failed open is now fatal (via set -e), not a
# silently-unbound fd 9 that would make the lock a no-op.
DEPLOY_LOCK_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/mobius"
install -d -m 0700 "$DEPLOY_LOCK_DIR"
DEPLOY_LOCK="$DEPLOY_LOCK_DIR/deploy-${TARGET}.lock"
exec 9>"$DEPLOY_LOCK"
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    fail "another ${TARGET} deploy is already running (lock: ${DEPLOY_LOCK})."
    fail "wait for it to finish (check 'docker ps' / the other session) and retry."
    exit 1
  fi
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

# Capture the CURRENTLY-RUNNING image so we can roll back to it if the new
# build fails its post-cutover health check. A build can succeed yet
# crash-loop at runtime (a SyntaxError fails Python at import, not
# `docker build` — exactly the 2026-05-30 conflict-marker outage), and the
# recreate has already replaced the live container by the time we notice.
# PREV_IMAGE is the image ID (stable); IMAGE_TAG is the tag compose
# resolves (e.g. `mobius-app`) that we re-point at it to restore.
PREV_IMAGE=$(docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || echo "")
IMAGE_TAG=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || echo "")
info "rollback image: ${IMAGE_TAG:-<unknown>} (${PREV_IMAGE:0:19}…)"

# Pin the previous image under a stable tag BEFORE the build. A
# `docker compose build` reuses IMAGE_TAG for the new image, which untags the
# old one; that now-dangling image can then be pruned (by this run's cleanup,
# a sibling, or earlyoom housekeeping) before a rollback needs it — the
# 2026-06-06 "No such image: sha256:…" rollback failure. A tagged image is
# never dangling, so this keeps PREV_IMAGE alive and resolvable for rollback.
if [ -n "$PREV_IMAGE" ]; then
  docker tag "$PREV_IMAGE" "${IMAGE_TAG%%:*}:rollback-prev" 2>/dev/null || true
fi

# Best-effort restore of the previous image after a failed cutover. Called
# as `attempt_rollback || true`, which disables errexit inside the function,
# so a failing docker step here can't itself abort the script. It is
# BEST-EFFORT, not guaranteed: each docker step is explicitly guarded so a
# failure is reported (not silently swallowed by the surrounding `|| true`),
# and we `--force-recreate` so compose can't no-op when the resolved image
# digest looks unchanged. A tag-succeeds-then-recreate-fails path can leave
# the tag pointing at the old image while the broken container still runs —
# hence the loud failure message for manual follow-up.
#
# Deliberately NOT presence-gated: rollback only fires when the cutover already
# FAILED its health probe, i.e. prod is broken and serving nothing. Waiting for
# an owner turn to drain there would prolong an outage; restoring the last-good
# image immediately is the correct emergency action. presence_gate() guards the
# routine cutover/restart paths, never this recovery path.
attempt_rollback() {
  if [ -z "$PREV_IMAGE" ] || [ -z "$IMAGE_TAG" ]; then
    fail "no previous image captured — cannot auto-roll back; recover ${CONTAINER} manually."
    return 1
  fi
  warn "auto-rolling back ${CONTAINER} to the previous image (${IMAGE_TAG} = ${PREV_IMAGE:0:19}…)"
  # With an external edge proxy on 80/443, an unselected recreate would also
  # start the bundled caddy service, whose port bindings collide with the
  # edge and fail the rollback exactly when it must not. Select the services
  # this script manages in that topology; a self-hosted (bundled-caddy)
  # rollback keeps the full-project recreate.
  local rollback_services=()
  if external_prod_caddy_running; then rollback_services=(app recoveryd); fi
  intent "docker tag ${PREV_IMAGE} ${IMAGE_TAG} && docker compose ${COMPOSE_ARGS[*]} up -d --force-recreate ${rollback_services[*]}"
  if ! docker tag "$PREV_IMAGE" "$IMAGE_TAG"; then
    fail "rollback: could not re-tag ${IMAGE_TAG} → ${PREV_IMAGE:0:19}… — recover ${CONTAINER} manually."
    return 1
  fi
  if ! docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate "${rollback_services[@]}"; then
    fail "rollback: 'compose up -d --force-recreate' failed — recover ${CONTAINER} manually."
    return 1
  fi
  for i in $(seq 1 "$CUTOVER_WAIT_SECONDS"); do
    code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
      ok "rolled back — ${CONTAINER} healthy on the previous image again"
      return 0
    fi
    sleep 1
  done
  fail "rollback did not restore health within ${CUTOVER_WAIT_SECONDS}s — manual intervention required."
  return 1
}

# Wait for a live-container probe to return 200, then roll back + exit if it
# never does. Consolidates the four formerly-near-identical cutover waits so
# the window is honest and configurable in ONE place. Args:
#   $1 probe command (a string eval'd each poll; must echo an HTTP status)
#   $2 success label   (e.g. "healthy", "writer ready")
#   $3 failure summary (printed before rollback, names what didn't come up)
#
# Two behaviors replace the old `for i in $(seq 1 120); … if [ "$i" = "30" ]`
# loops, whose 120 bound was dead code: the i==30 trap rolled back at 30s, so
# despite the "waiting up to 120s" message a slow-but-healthy boot false-failed
# at 30s (card 116). Now:
#   1. Configurable window — poll up to CUTOVER_WAIT_SECONDS (default 120), so a
#      slow real-data boot under memory pressure has room to bind before we
#      give up; override upward on a thrashing host.
#   2. Adaptive early rollback — if Docker's RestartCount climbs above where it
#      sat when the wait began, the container is genuinely crash-looping (not
#      just slow), so roll back immediately instead of burning the full window.
#      The preflight already proved the image boots, so a restart here is a real
#      runtime failure (bad migration on the populated volume, OOM-kill), worth
#      catching fast — while a steady count keeps waiting through a slow boot.
wait_for_cutover() {
  local probe="$1" ok_label="$2" fail_summary="$3"
  local code="000" baseline_restarts now_restarts i
  baseline_restarts=$(container_restart_count)
  for i in $(seq 1 "$CUTOVER_WAIT_SECONDS"); do
    code=$(eval "$probe")
    if [ "$code" = "200" ]; then
      ok "${ok_label} after ${i}s"
      return 0
    fi
    # Crash-loop detection: a RestartCount that climbs CRASH_RESTART_THRESHOLD
    # above the baseline means the container died and Docker restarted it
    # repeatedly — a genuine boot failure, not a slow bind. A SINGLE extra
    # restart is tolerated: this host OOM-bounces a recreated container once and
    # self-heals, so a delta of 1 isn't proof of a loop. Roll back only once the
    # delta reaches the threshold, rather than wait out the whole window. Guard
    # against the -1 inspect-error sentinel so a transient inspect hiccup
    # doesn't trip a false crash-loop verdict.
    now_restarts=$(container_restart_count)
    if [ "$now_restarts" -ge 0 ] 2>/dev/null &&
       [ "$baseline_restarts" -ge 0 ] 2>/dev/null &&
       [ $((now_restarts - baseline_restarts)) -ge "$CRASH_RESTART_THRESHOLD" ]; then
      fail "${fail_summary} (last: ${code}); ${CONTAINER} restarted $((now_restarts - baseline_restarts))× — it is crash-looping, not just slow."
      attempt_rollback || true
      exit 1
    fi
    sleep 1
  done
  fail "${fail_summary} after ${CUTOVER_WAIT_SECONDS}s (last: ${code}) — the new image is not serving."
  attempt_rollback || true
  exit 1
}

# ── prod source-safety guard ────────────────────────────────────────────
# The project pin above stops worktree junk, but a worktree (or a stale
# checkout) builds ITS branch — deploying non-main code to prod. Warn +
# confirm so prod always ships a deliberate, current main.
if [ "$TARGET" = "prod" ]; then
  if [ -f "$REPO_ROOT/.git" ]; then
    warn "running from a git worktree (${REPO_ROOT})."
    warn "prod normally deploys main from the canonical checkout; a worktree builds its own branch."
    confirm_yes "deploy prod from this worktree anyway?" || { fail "aborted — use the main checkout"; exit 1; }
  fi
  if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    # Pull origin/main forward so the ancestor + tip comparisons below see the
    # real remote state, not a stale local ref. `fetch origin` (not `origin
    # main`) so origin/main updates even on a worktree whose upstream tracks a
    # different branch; best-effort — an offline fetch leaves the prior ref.
    # Record whether the fetch actually succeeded: the behind-origin/main guard
    # below trusts origin/main as fresh only when it did; on a failed fetch the
    # local ref may be stale, so the guard warns rather than silently trusting it.
    fetch_ok=0
    if git -C "$REPO_ROOT" fetch origin -q 2>/dev/null; then fetch_ok=1; fi

    # ── unpushed-commit guard (the headline structural fix) ───────────────
    # You can only deploy code that is ALREADY on origin/main. A deploy from a
    # commit that isn't on origin/main ships code the NEXT deploy-from-main
    # silently REVERTS — the "deployed-but-unpushed → reverted" class. Assert
    # HEAD is contained in (an ancestor of) origin/main and refuse otherwise.
    # `--allow-unpushed` / ALLOW_UNPUSHED=1 is the documented escape hatch for a
    # deliberate emergency hotfix: it downgrades the abort to a loud warning
    # (safe-by-default, with an explicit override — not a hard wall).
    head_sha=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "")
    main_sha=$(git -C "$REPO_ROOT" rev-parse origin/main 2>/dev/null || echo "")
    if [ -z "$main_sha" ]; then
      warn "couldn't resolve origin/main (no network/remote?) — skipping the"
      warn "unpushed-commit guard; confirm you're on current, pushed main."
    elif ! git -C "$REPO_ROOT" merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
      if [ "$ALLOW_UNPUSHED" = "1" ]; then
        warn "HEAD $(git -C "$REPO_ROOT" rev-parse --short HEAD) is NOT on origin/main — deploying anyway (--allow-unpushed)."
        warn "PUSH IT to main ASAP: the next deploy-from-main will REVERT this prod build until you do."
      else
        fail "HEAD $(git -C "$REPO_ROOT" rev-parse --short HEAD) is not on origin/main (not an ancestor of $(git -C "$REPO_ROOT" rev-parse --short origin/main))."
        fail "Deploying an unpushed commit means the NEXT deploy-from-main silently REVERTS it"
        fail "(the deployed-but-unpushed → reverted class). Push it first:"
        fail "    git push origin HEAD:main"
        fail "then re-run. For a deliberate emergency hotfix, pass --allow-unpushed (or ALLOW_UNPUSHED=1)"
        fail "and push to main immediately after."
        exit 1
      fi
    fi

    # ── behind-origin/main guard (the mirror-image structural fix) ────────
    # The unpushed guard above refuses a checkout that is AHEAD of / DIVERGED
    # from origin/main. This refuses the OPPOSITE failure: a checkout that is
    # strictly BEHIND origin/main. deploy-prod builds from the WORKING TREE, so
    # a behind checkout bakes a STALE image — it lacks commits that ARE on main,
    # so the deploy silently REVERTS everyone's pushed work (served frontend
    # regresses to an old bundle). This is the recurring "sibling deployed from
    # a stale checkout" prod incident, and the single most common cause of it.
    #
    # Condition is STRICTLY BEHIND, not merely "not an ancestor": HEAD IS an
    # ancestor of origin/main AND origin/main is NOT an ancestor of HEAD. We
    # require BOTH so the guard never fires on a DIVERGED checkout — diverged is
    # owned by the unpushed guard above (exit 1, with its own --allow-unpushed
    # messaging). A bare `! is-ancestor origin/main HEAD` would also catch
    # diverged, which collides with the unpushed guard when the operator passed
    # --allow-unpushed (it warns-and-continues on diverged, then we'd wrongly
    # relabel it "behind"). The two-sided test keeps the guards disjoint:
    #   HEAD==main          → both ancestor checks true  → PASS (no-op)
    #   HEAD ahead/unpushed → origin/main IS ancestor    → PASS (unpushed concern)
    #   strictly behind     → only HEAD-is-ancestor true → BLOCK
    #   diverged            → HEAD-is-ancestor false     → PASS here (unpushed owns)
    # We hard-block (exit 2, distinct from the unpushed guard's exit 1 so callers
    # can tell the two apart). --allow-stale / ALLOW_STALE=1 is the escape hatch
    # for a DELIBERATE rollback to an older main. prod-only: scoped to
    # TARGET==prod by the enclosing `if`; the test target deploys throwaway
    # checkouts. main_sha empty (origin/main unresolvable) means the block above
    # already warned + skipped, so this only runs when origin/main is known.
    # NOTE: this verifies against the LOCAL origin/main ref, which is only
    # trustworthy when the pre-guard fetch (line ~539) actually SUCCEEDED. We
    # therefore HARD-BLOCK only when fetch_ok=1 — a failed fetch (offline /
    # remote down) leaves a possibly-stale cached ref, and the offline-is-a-
    # warning contract says we must NOT hard-block an offline deploy. When the
    # fetch failed we only WARN that staleness is unverified (the elif below),
    # accepting that a checkout matching a stale ref then deploys — the
    # alternative, blocking every offline deploy, is what we were told not to do.
    # main_sha empty (origin/main unresolvable at all) means the unpushed-guard
    # block above already warned + skipped.
    if [ -n "$main_sha" ] && [ "$fetch_ok" = "1" ] &&
       git -C "$REPO_ROOT" merge-base --is-ancestor HEAD origin/main 2>/dev/null &&
       ! git -C "$REPO_ROOT" merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
      behind_count=$(git -C "$REPO_ROOT" rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
      if [ "$ALLOW_STALE" = "1" ]; then
        warn "checkout is BEHIND origin/main by ${behind_count} commit(s) — deploying anyway (--allow-stale)."
        warn "this is a ROLLBACK: you are reverting newer pushed work. Confirm that's intended."
      else
        fail "checkout is BEHIND origin/main: HEAD $(git -C "$REPO_ROOT" rev-parse --short HEAD) is missing ${behind_count} commit(s) that are on origin/main $(git -C "$REPO_ROOT" rev-parse --short origin/main)."
        fail "deploy-prod builds from the working tree, so deploying this STALE checkout"
        fail "would bake an old image and silently REVERT everyone's pushed work."
        fail "Bring the checkout current first, then re-run:"
        fail "    git fetch && git rebase origin/main      # (or: git pull --ff-only)"
        fail "For a DELIBERATE rollback to an older main, pass --allow-stale (or ALLOW_STALE=1)."
        exit 2
      fi
    elif [ -n "$main_sha" ] && [ "$fetch_ok" != "1" ]; then
      # Origin/main resolved from a CACHED ref but the pre-guard fetch failed, so
      # the ref may be stale: a checkout that matches it could still be behind the
      # TRUE remote. Don't hard-block an offline deploy (offline-is-a-warning
      # contract) — make the unverified staleness explicit instead.
      warn "could not fetch origin (offline?) — the behind-origin/main staleness check"
      warn "is UNVERIFIED (ran against a possibly-stale cached origin/main ref)."
      warn "Confirm you're current with the true remote before trusting this deploy."
    fi

    # ── clean-tree guard ──────────────────────────────────────────────────
    # deploy-prod builds from the CHECKOUT (docker's build context = the working
    # tree, not HEAD), so uncommitted changes ship code that is on NO commit at
    # all — strictly worse than an unpushed commit (it can't even be pushed as
    # is). Refuse a dirty tree unless the same override is set.
    # `git status --porcelain` (not `diff --quiet`) so UNTRACKED files also count:
    # docker's build context includes them, so a stray/sibling untracked file
    # under a COPYed path (e.g. backend/app) ships to prod exactly like a
    # modified one. Gitignored files (.env, .pm/, dist) don't show, so this
    # only trips on genuine would-ship content.
    if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
      if [ "$ALLOW_UNPUSHED" = "1" ]; then
        warn "working tree is DIRTY — deploying uncommitted changes anyway (--allow-unpushed)."
        warn "These changes are on no commit; commit + push to main ASAP."
      else
        fail "working tree is dirty — deploy-prod builds from the checkout, so this would"
        fail "ship uncommitted code that is on no commit (the next deploy reverts it)."
        fail "Commit + push to main first, or pass --allow-unpushed for an emergency hotfix."
        exit 1
      fi
    fi

    # Existing on-main-but-different / unresolvable-origin advisory (kept).
    if [ -n "$head_sha" ] && [ -n "$main_sha" ] && [ "$head_sha" != "$main_sha" ]; then
      warn "HEAD $(git -C "$REPO_ROOT" rev-parse --short HEAD) != origin/main $(git -C "$REPO_ROOT" rev-parse --short origin/main 2>/dev/null) — you may deploy non-main code."
      confirm_yes "deploy this non-main checkout to prod?" || { fail "aborted"; exit 1; }
    fi
  fi
fi

# ── step 1: build (with cache-prune guard) ─────────────────────────────
if [ "$SKIP_BUILD" = "1" ]; then
  step "[1/4] SKIPPED docker compose build (--skip-build)"
else
  step "[1/4] docker compose build"
  # Refuse to build source that still carries unresolved merge-conflict
  # markers. A sibling once committed `<<<<<<< HEAD` into apps.py and
  # deployed it — the image crash-looped on a SyntaxError and prod went
  # 502. A sub-second grep is the cheapest possible backstop against
  # shipping a half-resolved merge to prod.
  intent "scanning build source for merge-conflict markers"
  if markers=$(grep -rlIE '^(<<<<<<<|>>>>>>>) ' backend/app backend/scripts frontend/src skill 2>/dev/null) && [ -n "$markers" ]; then
    fail "unresolved merge-conflict markers in:"
    printf '%s\n' "$markers" | sed 's/^/    /' >&2
    fail "resolve them before building (this exact class 502'd prod once)."
    exit 1
  fi
  ok "build source is conflict-free"
  cache_gb=$(build_cache_gb)
  info "current build cache: ~${cache_gb}GB"
  if [ "$cache_gb" -ge 6 ]; then
    warn "build cache ≥ 6GB; prior runs have OOM'd this 7.6GB host."
    intent "docker builder prune -af --filter \"until=24h\""
    docker builder prune -af --filter "until=24h" >/dev/null
    ok "build cache pruned"
  fi
  # Bake the commit being deployed into the image (→ GET /api/version), so
  # the verify step + future --check can confirm the served backend matches.
  # compose reads BUILD_SHA from the env via `args: BUILD_SHA: ${BUILD_SHA:-…}`.
  # docker build includes the WORKING TREE, not just HEAD — so if the tree is
  # dirty, mark the SHA `-dirty` rather than claim an exact commit it isn't.
  _sha="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  # `git status --porcelain` (not `diff --quiet HEAD`) so UNTRACKED files count
  # too — docker's build context includes them, so an untracked source file
  # would otherwise let the stamp claim a clean commit the image isn't.
  if [ "$_sha" != "unknown" ] && [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
    _sha="${_sha}-dirty"
  fi
  export BUILD_SHA="$_sha"
  # The commit's date (YYYY-MM-DD) → a human "version · date" line in Settings.
  # For a DIRTY (--allow-unpushed) build the working tree is NOT HEAD, so HEAD's
  # commit date would mislabel uncommitted code with an older date — stamp the
  # build date (today, UTC) instead so the date matches what actually shipped.
  case "$_sha" in
    *-dirty) export BUILD_DATE="$(date -u +%Y-%m-%d)" ;;
    *) export BUILD_DATE="$(git -C "$REPO_ROOT" show -s --format=%cs HEAD 2>/dev/null || echo unknown)" ;;
  esac
  BUILT_THIS_RUN=1
  info "baking BUILD_SHA=${BUILD_SHA:0:18}… (${BUILD_DATE}) into the image"
  intent "docker compose ${COMPOSE_ARGS[*]} build"
  if ! confirm_yes "${C_YELLOW}slow step (5-15 min, has OOM'd before).${C_RESET} proceed?"; then
    fail "aborted by user at build step"
    exit 1
  fi
  docker compose "${COMPOSE_ARGS[@]}" build
  ok "image rebuilt"
fi

# ── preflight: boot the new image in a scratch container before cutover ──
# A green `docker build` does NOT mean the image RUNS: a SyntaxError fails at
# Python import, a missing dep at uvicorn start, a bad migration at lifespan —
# none of which `build` catches (this is exactly the 2026-05-30 conflict-marker
# outage: the build succeeded, the container crash-looped). Step [2/4] below
# replaces the LIVE container before we learn that, leaving the rollback as the
# only net and prod blinking 502 for its duration. So first boot the freshly-
# built image in an ISOLATED scratch container: if it can't serve /api/health
# AND /api/ready, abort with the live container UNTOUCHED — no blink, no
# rollback needed. Only runs when we built an image this run; --skip-build
# reuses the already-live (already-proven) image, which needs no pre-check.
if [ "$BUILT_THIS_RUN" = "1" ] && [ -n "$IMAGE_TAG" ]; then
  step "[preflight] boot the new image in a scratch container"
  PREFLIGHT_CONTAINER="${CONTAINER}-preflight-$$"
  # Remove the scratch box on ANY exit from here on (idempotent: rm -f on a
  # missing container no-ops). Left set for the rest of the run — it only ever
  # targets this run's uniquely-$$-named box, so it can't touch prod.
  _cleanup_preflight() { docker rm -f "$PREFLIGHT_CONTAINER" >/dev/null 2>&1 || true; }
  trap _cleanup_preflight EXIT
  # Sweep any scratch box a previously-killed run left behind (best-effort).
  docker ps -aq --filter "name=^${CONTAINER}-preflight-" \
    | xargs -r docker rm -f >/dev/null 2>&1 || true
  # Any 32+ char key satisfies the settings validator; the scratch box has a
  # fresh tmpfs DB and serves no authenticated request, so a throwaway key is
  # correct here — we are testing "does the image boot", not prod auth.
  _pf_sk=$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null \
    || echo "preflight-throwaway-key-at-least-32-chars-not-for-real-use")
  # tmpfs /data → never reads or writes the prod volume; no published port →
  # no host collision; MOEBIUS_SKIP_BOOTSTRAP skips the first-boot GitHub fetch
  # (irrelevant to "does it boot"). IMAGE_TAG is the tag compose just rebuilt
  # in place, so this runs the about-to-be-deployed image.
  intent "docker run -d --name ${PREFLIGHT_CONTAINER} (tmpfs /data, no port) ${IMAGE_TAG}"
  docker run -d \
    --name "$PREFLIGHT_CONTAINER" \
    --init \
    --restart no \
    --tmpfs /data:mode=0755 \
    -e "SECRET_KEY=${_pf_sk}" \
    -e "DATABASE_URL=sqlite:////data/db/preflight.db" \
    -e "DATA_DIR=/data" \
    -e "DOMAIN=localhost" \
    -e "FRONTEND_ORIGIN=http://localhost" \
    -e "MOEBIUS_SKIP_BOOTSTRAP=1" \
    "$IMAGE_TAG" >/dev/null
  # Poll liveness then writer-readiness via `docker exec` (same probe as the
  # live waits below). The scratch box cold-starts (tmpfs, no warm page cache)
  # and can spend close to a minute on first-run setup on the memory-tight
  # host, so keep this longer than the live cutover waits.
  _pf_live=0
  info "waiting up to ${PREFLIGHT_WAIT_SECONDS}s for preflight ${INTERNAL_BASE}/api/health"
  for i in $(seq 1 "$PREFLIGHT_WAIT_SECONDS"); do
    code=$(docker exec "$PREFLIGHT_CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then ok "preflight /api/health: 200 after ${i}s"; _pf_live=1; break; fi
    sleep 1
  done
  if [ "$_pf_live" != "1" ]; then
    fail "preflight: the new image never served /api/health 200 in ${PREFLIGHT_WAIT_SECONDS}s (last: ${code})."
    fail "it crash-loops at boot — the LIVE ${CONTAINER} was NOT touched. Last 40 log lines:"
    docker logs "$PREFLIGHT_CONTAINER" --tail 40 2>&1 | sed 's/^/    /' >&2 || true
    exit 1
  fi
  _pf_ready=0
  info "waiting up to ${PREFLIGHT_WAIT_SECONDS}s for preflight ${INTERNAL_BASE}/api/ready"
  for i in $(seq 1 "$PREFLIGHT_WAIT_SECONDS"); do
    rcode=$(docker exec "$PREFLIGHT_CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/ready'" 2>/dev/null || echo "000")
    if [ "$rcode" = "200" ]; then ok "preflight /api/ready: 200 after ${i}s"; _pf_ready=1; break; fi
    sleep 1
  done
  if [ "$_pf_ready" != "1" ]; then
    fail "preflight: the new image's /api/ready never returned 200 in ${PREFLIGHT_WAIT_SECONDS}s (last: ${rcode})."
    fail "the chat-persistence writer fails to start — the LIVE ${CONTAINER} was NOT touched. Last 40 log lines:"
    docker logs "$PREFLIGHT_CONTAINER" --tail 40 2>&1 | sed 's/^/    /' >&2 || true
    exit 1
  fi
  _cleanup_preflight
  ok "preflight passed — the new image boots and the writer is ready; cutting over"
elif [ "$SKIP_BUILD" = "1" ]; then
  info "preflight skipped (--skip-build): reusing the already-live image; nothing new to pre-check"
fi

# ── step 2: recreate container with the new image ──────────────────────
step "[2/4] docker compose up -d (recreates ${CONTAINER})"
presence_gate "cutover (recreate ${CONTAINER})"
RECOVERYD_CUTOVER_FAILED=0
if external_prod_caddy_running; then
  info "external edge-caddy owns ports 80/443; updating app + recoveryd services"
  ensure_edge_network
  docker rm -f "${CONTAINER}-caddy-1" >/dev/null 2>&1 || true
  intent "docker compose ${COMPOSE_ARGS[*]} up -d app"
  docker compose "${COMPOSE_ARGS[@]}" up -d app
  # Recover the recovery floor onto the new image too. The recovery agent runs
  # as full root, so its container carries the read_only + cap_drop guardrail
  # (base compose) — recreating it here from the freshly-built image is how the
  # guardrailed recoveryd stays reproducible instead of a hand-run container.
  # A recoveryd failure must NOT roll back a healthy app deploy, but it must
  # not pass silently either: the deploy exits nonzero at the end (the flag
  # below) because a prod without its recovery floor is one platform bug away
  # from being unrecoverable.
  intent "docker compose ${COMPOSE_ARGS[*]} up -d recoveryd"
  if docker compose "${COMPOSE_ARGS[@]}" up -d recoveryd; then
    # recoveryd's healthcheck allows a 10s start_period; probing once right
    # after `up -d` would false-fail every deploy. Poll within a bounded
    # window instead.
    _recoveryd_ok=0
    for _i in $(seq 1 30); do
      if docker exec mobius-recoveryd sh -c \
        "curl -fsS -o /dev/null http://localhost:8001/recover/health" 2>/dev/null; then
        _recoveryd_ok=1; break
      fi
      sleep 1
    done
    if [ "$_recoveryd_ok" = "1" ]; then
      ok "recovery floor (mobius-recoveryd) healthy on the new image"
    else
      fail "recoveryd recreated but /recover/health did not answer within 30s — the recovery floor is DOWN"
      RECOVERYD_CUTOVER_FAILED=1
    fi
  else
    fail "recoveryd cutover failed — app deploy is unaffected, but the recovery floor is NOT on the new image"
    RECOVERYD_CUTOVER_FAILED=1
  fi
else
  intent "docker compose ${COMPOSE_ARGS[*]} up -d"
  docker compose "${COMPOSE_ARGS[@]}" up -d
fi
info "waiting up to ${CUTOVER_WAIT_SECONDS}s for ${INTERNAL_BASE}/api/health"
wait_for_cutover \
  "docker exec \"\$CONTAINER\" sh -c \"curl -s -o /dev/null -w '%{http_code}' '\${INTERNAL_BASE}/api/health'\" 2>/dev/null || echo 000" \
  "healthy" \
  "health check never returned 200"

# Liveness alone is not enough: the chat-persistence writer must be ready
# (started, alive, not fatal, not stopping) or every chat write fails on a
# process that still answers /api/health 200. Give it the same budget —
# start_writer runs in the lifespan before serving, so this is normally
# already 200 by the time /api/health was.
info "waiting up to ${CUTOVER_WAIT_SECONDS}s for ${INTERNAL_BASE}/api/ready"
wait_for_cutover "ready_code" "writer ready" \
  "readiness check never returned 200 — the chat-persistence writer is not serving"

run_deploy_canary

# ── step 2b: install the rendered edge fragment ────────────────────────
# After the app is verified healthy so a failed cutover never ships new
# routing, but before step 4's public checks so they exercise the fragment
# this deploy just rendered.
if external_prod_caddy_running; then
  step "[2b/4] install edge fragment"
  install_edge_fragment
fi

# ── step 3: rebuild the served platform frontend ───────────────────────
# The authoritative frontend source and dist now live in /data/platform. After
# boot reconcile advances the clone, rebuild that tree in place; do not copy
# from /app/shell-src, and do not touch any leftover legacy shell directory.
# StaticFiles keeps serving the same dist path, so no extra restart is needed
# after the swap.
step "[3/4] rebuild /data/platform/frontend"
intent "docker exec -u mobius ${CONTAINER} bash /app/scripts/rebuild_shell.sh"
docker exec -u mobius "$CONTAINER" bash /app/scripts/rebuild_shell.sh

# ── step 4: verify bundle rotated + endpoints respond ──────────────────
step "[4/4] verify"
after_hash=$(served_bundle)
info "before: ${before_hash:-<none>}"
info "after:  ${after_hash:-<none>}"
if [ -z "$after_hash" ]; then
  fail "could not parse bundle hash from ${INTERNAL_BASE}/ — check manually"
  exit 1
fi
# An unchanged hash is EXPECTED for a backend-only deploy (no frontend
# source changed -> rebuild_shell.sh legitimately produces an identical
# bundle), so it's a warning, not a failure — the real success signals are
# the health checks below + rebuild_shell's own exit status (a hard rebuild
# failure already aborts step 3 under `set -e`). On a deploy you EXPECTED to
# change the frontend, an unchanged hash means rebuild_shell no-op'd or
# produced an identical bundle — the warning + the before/after hashes are
# the cue to investigate. (Previously this was a hard exit 1, which
# false-failed every backend-only deploy.)
if [ -n "$before_hash" ] && [ "$before_hash" = "$after_hash" ]; then
  warn "bundle hash unchanged (${after_hash})."
  warn "expected for a backend-only deploy; if you changed the frontend, check"
  warn "the build output. Container is recreated + healthy regardless."
else
  ok "bundle rotated: ${before_hash:-<none>} → ${after_hash}"
fi

# Backend version stamp: confirm the SERVED backend is the commit we built —
# the backend analogue of the bundle-hash check above (which only sees the
# frontend).
served=$(served_sha)
if [ "$BUILT_THIS_RUN" = "1" ]; then
  if [ -n "$served" ] && [ "$served" = "$BUILD_SHA" ]; then
    ok "backend sha: ${served:0:18}… (matches the commit just built)"
  elif [ -z "$served" ] || [ "$served" = "unknown" ]; then
    # The new image is healthy (checked above) but reports no/unknown SHA — the
    # BUILD_SHA build-arg didn't populate the stamp. The CODE is deployed; only
    # the provenance stamp is missing, so warn rather than fail.
    warn "backend sha is '${served:-<none>}' after a build — the BUILD_SHA arg"
    warn "didn't reach the image. Deploy is healthy; fix the arg pipeline so"
    warn "/api/version reports provenance next time."
  else
    # A DIFFERENT real commit is serving than the one we built: the recreate is
    # serving a stale/wrong image, so the deploy did NOT actually take. Fail so
    # it isn't misreported as complete.
    fail "backend sha MISMATCH: serving ${served:0:18}… but built ${BUILD_SHA:0:18}…"
    fail "the recreate is serving a different image than the one just built —"
    fail "the deploy did not take (stale tag / concurrent deploy). Investigate."
    exit 1
  fi
else
  # --skip-build: we didn't build, so don't compare against BUILD_SHA — just
  # report what's serving.
  info "backend sha: ${served:-<none>} (no build this run; not compared)"
fi

# ── served PLATFORM ancestry assertion (prod only) ─────────────────────
# The backend-sha block above reads the IMAGE build sha. Under the clone model,
# the deployed backend is the served /data/platform HEAD after boot reconcile.
# Assert freshness by ancestry: origin/main must be contained in that served HEAD
# (exact equality is fine; local agent commits replayed on top are also fine).
# Reconcile conflict/rollback/offline states are explicit exceptions because
# they intentionally keep the previous working tree live.
if [ "$TARGET" = "prod" ]; then
  serving_source=$(served_version_field serving_source)
  platform_sha=$(served_version_field platform_sha)
  case "$platform_sha" in null|unknown) platform_sha="" ;; esac

  platform_freshness=$(docker exec -u mobius "$CONTAINER" bash -c '
    cd /data/platform 2>/dev/null || { echo missing; exit 0; }
    [ -f /data/.platform-conflict ] && { echo conflict; exit 0; }
    [ -f /data/.platform-rolled-back ] && { echo rolled_back; exit 0; }
    [ -f /data/.platform-offline ] && { echo offline_flag; exit 0; }
    head=$(git rev-parse --verify HEAD 2>/dev/null) || { echo invalid; exit 0; }
    if ! git fetch --quiet origin main:refs/remotes/origin/main >/dev/null 2>&1; then
      echo offline; exit 0
    fi
    target=$(git rev-parse --verify origin/main 2>/dev/null) || { echo offline; exit 0; }
    if [ "$head" = "$target" ]; then
      echo "exact:$head"; exit 0
    fi
    if git merge-base --is-ancestor "$target" "$head" 2>/dev/null; then
      echo "ancestor:$target:$head"; exit 0
    fi
    echo "stale:$target:$head"
  ' 2>/dev/null || echo probe_failed)

  case "$platform_freshness" in
    conflict)
      warn "served platform freshness skipped: /data/.platform-conflict is set."
      warn "The previous working platform remains live until the conflict is resolved."
      ;;
    rolled_back)
      warn "served platform freshness skipped: /data/.platform-rolled-back is set."
      warn "Boot reconcile rejected the update and kept the previous working platform live."
      ;;
    offline|offline_flag)
      warn "served platform freshness skipped: origin/main could not be refreshed in the container."
      warn "Boot reconcile will retry when origin is reachable."
      ;;
    exact:*)
      head_sha=${platform_freshness#exact:}
      if [ "$serving_source" != "platform" ]; then
        fail "served platform source is '${serving_source:-<unknown>}' even though /data/platform is fresh."
        fail "The backend is not serving the one served tree; investigate entrypoint fallback."
        exit 1
      fi
      ok "served platform: HEAD == origin/main (${head_sha:0:18}…)"
      ;;
    ancestor:*)
      pair=${platform_freshness#ancestor:}
      origin_sha=${pair%%:*}
      head_sha=${pair#*:}
      if [ "$serving_source" != "platform" ]; then
        fail "served platform source is '${serving_source:-<unknown>}' even though /data/platform is fresh."
        fail "The backend is not serving the one served tree; investigate entrypoint fallback."
        exit 1
      fi
      if [ -n "$platform_sha" ] && [ "$platform_sha" != "$head_sha" ]; then
        fail "/api/version platform_sha ${platform_sha:0:18}… != /data/platform HEAD ${head_sha:0:18}…"
        fail "The served-platform stamp is stale after reconcile."
        exit 1
      fi
      ok "served platform: origin/main ${origin_sha:0:18}… is ancestor of served HEAD ${head_sha:0:18}…"
      ;;
    stale:*)
      pair=${platform_freshness#stale:}
      origin_sha=${pair%%:*}
      head_sha=${pair#*:}
      fail "served platform is stale: origin/main ${origin_sha:0:18}… is not an ancestor of /data/platform HEAD ${head_sha:0:18}…"
      fail "No reconcile conflict/offline flag explains the drift; deploy did not advance the served tree."
      exit 1
      ;;
    missing)
      fail "/data/platform is missing after recreate; entrypoint did not seed the served tree."
      exit 1
      ;;
    invalid)
      fail "/data/platform exists but has no valid git HEAD after recreate."
      exit 1
      ;;
    *)
      fail "could not verify served platform freshness (${platform_freshness})."
      exit 1
      ;;
  esac
fi

# Internal /api/health (we already checked this twice during waits, but
# repeat it in the verification block so the final summary stands alone).
code=$(docker exec "$CONTAINER" sh -c "curl -s -o /dev/null -w '%{http_code}' '${INTERNAL_BASE}/api/health'" 2>/dev/null || echo "000")
if [ "$code" = "200" ]; then
  ok "internal /api/health: ${code}"
else
  fail "internal /api/health: ${code}"
  exit 1
fi

# Writer-aware readiness: liveness 200 isn't enough — fail the deploy if the
# chat-persistence writer can't actually serve (so we never report a deploy
# as complete on a process where every chat write fails).
rcode=$(ready_code)
if [ "$rcode" = "200" ]; then
  ok "internal /api/ready:  ${rcode}"
else
  fail "internal /api/ready:  ${rcode}"
  fail "the process is live but the chat-persistence writer is not ready — chat writes would fail."
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

  # The recovery floor must be publicly reachable through the proxy — it is
  # the way back in when the platform itself is broken, so a deploy that
  # silently severed /recover* routing is a failed deploy even with a healthy
  # app.
  rcode_pub=$(curl -sk -o /dev/null -w '%{http_code}' "https://${DOMAIN}/recover/health" || echo "000")
  if [ "$rcode_pub" = "200" ]; then
    ok "public  /recover/health: ${rcode_pub}"
  else
    fail "public  /recover/health: ${rcode_pub} — the recovery floor is not reachable through the proxy."
    if external_prod_caddy_running; then
      fail "If this deploy's fragment caused it: <edge>/edgectl rollback mobius restores the previously served routing."
    fi
    exit 1
  fi

  # Service gateway origin (when configured): a non-/services path must fail
  # closed with 404 — anything else means the gateway host is either not
  # routed (000/5xx) or serving shell content (200), both wrong.
  gw_origin="${MOBIUS_SERVICE_GATEWAY_ORIGIN:-}"
  if [ -n "$gw_origin" ] && [ "$gw_origin" != "http://services.invalid" ]; then
    gcode=$(curl -sk -o /dev/null -w '%{http_code}' "${gw_origin}/" || echo "000")
    if [ "$gcode" = "404" ]; then
      ok "gateway  ${gw_origin}/ fails closed: ${gcode}"
    else
      fail "gateway  ${gw_origin}/ returned ${gcode} (expected 404) — check the edge fragment + gateway routing."
      fail "If this deploy's fragment caused it: <edge>/edgectl rollback mobius restores the previously served routing."
      exit 1
    fi
  fi
fi

# Tell open PWAs to reload onto the freshly-rebuilt shell. Only now —
# the deploy is verified healthy (bundle rotated + /api/health + /api/ready
# + public reachability all green), so a reload lands on a known-good build.
# Best-effort: never fails the deploy.
step "[4b/4] notify open shells to reload"
if broadcast_shell_rebuilt; then
  ok "broadcast shell_rebuilt — open PWAs will reload onto the new bundle"
else
  warn "could not broadcast shell_rebuilt (no service token, or /api/notify"
  warn "unreachable). Deploy is healthy; open PWAs reload on next manual open."
fi

# Reclaim the image this deploy superseded. A cutover leaves the prior `latest`
# untagged (rollback-prev just moved to the new previous), so without this every
# deploy permanently accumulates a ~4.7GB dangling image on /mnt/data — the
# recurring disk-full cause that crash-looped prod on 2026-06-08 (a full volume
# fails SQLite WAL with "disk I/O error", which the auto-rollback can't escape
# because both images share the full disk). Prune ONLY dangling (untagged)
# images: never a tagged image (the current, the rollback-prev, or a sibling
# mobius-test:ci), and shared base layers stay alive via the running container's
# refcount. Best-effort — a prune failure must not fail a successful deploy.
info "reclaiming the superseded image (dangling only)…"
docker image prune -f >/dev/null 2>&1 || true

if [ "${RECOVERYD_CUTOVER_FAILED:-0}" = "1" ]; then
  fail "deploy verified healthy, BUT the recovery floor cutover failed (see step 2 above)."
  fail "Fix mobius-recoveryd before walking away — a prod without recovery is one bug from unrecoverable."
  exit 1
fi

printf '\n%sdeploy complete%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
