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
#   scripts/deploy-prod.sh --allow-unpushed # emergency hotfix: deploy a commit not yet on origin/main
#                                           # (downgrades the default refusal + dirty-tree abort to a warning;
#                                           #  push it to main ASAP or the next deploy-from-main reverts it)
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
CHECK_ONLY=0
# Default-refuse to deploy a commit that isn't on origin/main: a deploy from an
# unpushed commit ships code the next deploy-from-main silently REVERTS (the
# "deployed-but-unpushed → reverted" class — see push-deploy-to-main lesson).
# The escape hatch (--allow-unpushed / ALLOW_UNPUSHED=1) downgrades the abort to
# a loud warning for a deliberate emergency hotfix: empower with an explicit
# override, safe-by-default — not a hard wall.
ALLOW_UNPUSHED="${ALLOW_UNPUSHED:-0}"
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
for _knob in PREFLIGHT_WAIT_SECONDS CUTOVER_WAIT_SECONDS; do
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

external_prod_caddy_running() {
  [ "$TARGET" = "prod" ] &&
    docker inspect -f '{{.State.Running}}' deploy-caddy-1 2>/dev/null | grep -q true
}

ensure_external_caddy_route() {
  if ! external_prod_caddy_running; then return 0; fi
  if ! docker network inspect deploy_default >/dev/null 2>&1; then
    warn "deploy-caddy-1 is running, but deploy_default network is missing; public proxy may not reach ${CONTAINER}."
    return 0
  fi
  # The public Caddy in this host's outer deploy project proxies to
  # http://mobius:8000 on deploy_default. Recreating the app from this repo's
  # compose project puts it on mobius_default, so reconnect it to the proxy
  # network after every cutover. `network connect` is idempotent for our
  # purposes: "already exists" means the route is already present.
  if docker network connect --alias mobius --alias app deploy_default "$CONTAINER" 2>/dev/null; then
    ok "connected ${CONTAINER} to deploy_default for external Caddy"
  else
    info "${CONTAINER} already connected to deploy_default, or Docker reported no-op"
  fi
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
# analogue of served_bundle (which only sees the frontend shell).
served_sha() {
  docker exec "$CONTAINER" sh -c "curl -fsS '${INTERNAL_BASE}/api/version' 2>/dev/null" \
    | sed -n 's/.*"sha":"\([^"]*\)".*/\1/p' \
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

# Tell already-open PWAs to reload after a successful shell rebuild.
#
# deploy-prod.sh shadows /data/shell/dist and the new SW skipWaiting/
# clientsClaim, but a PWA that was already open never learns a fresh
# bundle exists — it keeps running the old shell until the user closes
# and reopens it. The Shell's system-event stream already handles a
# `shell_rebuilt` event (frontend/src/components/Shell/Shell.jsx — it
# fades out and reloads), and POST /api/notify already broadcasts that
# event type to every open Shell's /api/events/system subscription. The
# only missing link is firing it; deploy never did. We fire it here,
# AFTER the post-restart readiness gate, so open PWAs auto-reload onto
# the new bundle.
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
# container at once race (recreate clobbers, half-built /data/shell). Take
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
attempt_rollback() {
  if [ -z "$PREV_IMAGE" ] || [ -z "$IMAGE_TAG" ]; then
    fail "no previous image captured — cannot auto-roll back; recover ${CONTAINER} manually."
    return 1
  fi
  warn "auto-rolling back ${CONTAINER} to the previous image (${IMAGE_TAG} = ${PREV_IMAGE:0:19}…)"
  intent "docker tag ${PREV_IMAGE} ${IMAGE_TAG} && docker compose ${COMPOSE_ARGS[*]} up -d --force-recreate"
  if ! docker tag "$PREV_IMAGE" "$IMAGE_TAG"; then
    fail "rollback: could not re-tag ${IMAGE_TAG} → ${PREV_IMAGE:0:19}… — recover ${CONTAINER} manually."
    return 1
  fi
  if ! docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate; then
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
    git -C "$REPO_ROOT" fetch origin -q 2>/dev/null || true

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

    # ── clean-tree guard ──────────────────────────────────────────────────
    # deploy-prod builds from the CHECKOUT (docker's build context = the working
    # tree, not HEAD), so uncommitted changes ship code that is on NO commit at
    # all — strictly worse than an unpushed commit (it can't even be pushed as
    # is). Refuse a dirty tree unless the same override is set.
    if ! { git -C "$REPO_ROOT" diff --quiet && git -C "$REPO_ROOT" diff --cached --quiet; } 2>/dev/null; then
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
  BUILT_THIS_RUN=1
  info "baking BUILD_SHA=${BUILD_SHA:0:18}… into the image"
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
if external_prod_caddy_running; then
  info "external deploy-caddy-1 owns ports 80/443; updating app service only"
  docker rm -f "${CONTAINER}-caddy-1" >/dev/null 2>&1 || true
  intent "docker compose ${COMPOSE_ARGS[*]} up -d app"
  docker compose "${COMPOSE_ARGS[@]}" up -d app
  ensure_external_caddy_route
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

# ── step 3: refresh /data/shell/{src,dist} so new bundle isn't masked ──
# The data volume survives the container recreation. /data/shell/dist/
# is whatever the agent last built, NOT the new image's /app/static/.
# main.py picks _live_dir over _baked_dir at module load, so without
# this step uvicorn keeps serving the stale dist.
step "[3/4] refresh /data/shell/ from new image's /app/shell-src"
# Clear /data/shell first so cp -a can land symlinks where the previous
# deploy wrote regular files (or vice versa). Without this, npm packages
# whose internal layout shifted between builds (e.g.
# micromark-extension-math's hoisted katex bin: file in v3 → symlink in
# v4) cause `cp: cannot create symbolic link ... File exists` because
# cp -a's `-d` flag preserves the source symlink but won't overwrite an
# existing destination of a different type. Trying to be defensive with
# rm -rf /data/shell/dist alone (the previous behavior) missed the
# node_modules sub-tree entirely. We're about to rebuild from scratch
# anyway — clear everything in /data/shell. /app/static/ remains as the
# baked fallback if anything goes wrong.
# Snapshot live shell edits before the wholesale clear erases them. The
# in-product agent edits /data/shell/src in place, but src is NOT tracked in
# /data's git (only shell/dist + shell/node_modules are gitignored — src is
# simply never `git add`ed), so without this every agent UI fix is silently
# lost on the next deploy. This is exactly how the text_boundary / scroll /
# app-CTA work drifted unrecoverably out of origin/main. Snapshot to a
# timestamped tarball OUTSIDE /data/shell (so the rm can't eat it) whenever
# src differs from the image's baked /app/shell-src. Best effort — never
# blocks the deploy; recover later by diffing the tarball into frontend/src.
if docker exec "$CONTAINER" sh -c 'test -d /data/shell/src' 2>/dev/null \
   && ! docker exec "$CONTAINER" sh -c 'diff -rq /app/shell-src/src /data/shell/src >/dev/null 2>&1'; then
  # Write under /data/backups/ (gitignored, so nightly pm-commit/dreaming
  # `git add -A` never commits a ~360KB blob into /data history) and run as
  # `mobius` (NOT bare docker exec → root, which would poison the mobius-owned
  # /data tree). Keep only the last 5 so they can't accumulate on the host.
  shell_snap="/data/backups/shell-src-predeploy-$(date +%Y%m%d-%H%M%S).tar.gz"
  warn "/data/shell/src differs from the baked shell (live agent edits and/or a"
  warn "prior frontend deploy) — snapshotting to ${shell_snap} before refresh."
  intent "docker exec -u mobius ${CONTAINER} sh -c \"mkdir -p /data/backups; tar czf '${shell_snap}' -C /data/shell src; ls -1t /data/backups/shell-src-predeploy-*.tar.gz | tail -n +6 | xargs -r rm -f\""
  docker exec -u mobius "$CONTAINER" sh -c "mkdir -p /data/backups && tar czf '${shell_snap}' -C /data/shell src 2>/dev/null && ls -1t /data/backups/shell-src-predeploy-*.tar.gz | tail -n +6 | xargs -r rm -f" \
    && info "shell snapshot saved (recover: docker cp ${CONTAINER}:${shell_snap} . then diff into frontend/src)" \
    || warn "shell snapshot failed — proceeding; agent shell edits may be unrecoverable after this."
fi
intent "docker exec ${CONTAINER} sh -c 'rm -rf /data/shell/* /data/shell/.[!.]* 2>/dev/null; true'"
docker exec "$CONTAINER" sh -c 'rm -rf /data/shell/* /data/shell/.[!.]* 2>/dev/null; true'
intent "docker exec ${CONTAINER} cp -a /app/shell-src/. /data/shell/"
docker exec "$CONTAINER" cp -a /app/shell-src/. /data/shell/
intent "docker exec ${CONTAINER} rm -rf /data/shell/dist"
docker exec "$CONTAINER" rm -rf /data/shell/dist
intent "docker exec ${CONTAINER} bash /app/scripts/rebuild_shell.sh"
docker exec "$CONTAINER" bash /app/scripts/rebuild_shell.sh

# Stamp /data/shell/.image-build-sha with the SHA we just deployed so the
# entrypoint's self-host image-update detector (entrypoint.sh) sees the
# marker already matching this image's BUILD_SHA on the next boot and stays
# a no-op — it must not overwrite the dist this deploy just rebuilt+verified
# from /data/shell/src. Without the stamp a later recreate would see
# BUILD_SHA != stale-marker and re-copy /app/static over the deploy's build.
intent "docker exec ${CONTAINER} sh -c 'printf %s \"\$BUILD_SHA\" > /data/shell/.image-build-sha'"
docker exec "$CONTAINER" sh -c 'printf %s "${BUILD_SHA:-unknown}" > /data/shell/.image-build-sha' || true

# ── step 3b: sync /data/platform from the new baked floor ──────────────
# The backend serves from /data/platform (the agent-editable, git-tracked
# platform layer), which persists across image deploys BY DESIGN. A deploy
# therefore does not reach the served backend until /data/platform is
# synced from the new image's baked copy — without this step, prod keeps
# running the previous deploy's backend while the sha-verify below reads
# the new image and reports success (exactly how the icon-transparency
# fix "deployed" but never served). Fast-forward automatically only when
# the platform repo shows no agent work: tree clean (mode-only and dotfile
# boot artifacts ignored) and every commit is a system commit
# (init/restore/sync). Agent divergence is left for the in-product agent
# to merge — discarding it here would violate the reversibility contract.
step "[3b/4] sync /data/platform from the new baked floor"
platform_state=$(docker exec -u mobius "$CONTAINER" bash -c '
  cd /data/platform 2>/dev/null || { echo missing; exit 0; }
  dirty=$(git -c core.fileMode=false status --porcelain | grep -vE "^\?\? \.|\.baked-sha$" || true)
  agent_commits=$(git log --format="%s" \
    | grep -cvE "^(init: platform layer|restore: platform|sync: platform)" || true)
  if [ -n "$dirty" ] || [ "$agent_commits" != "0" ]; then echo diverged
  else echo clean; fi
' 2>/dev/null || echo missing)
case "$platform_state" in
  clean)
    intent "docker exec -u root ${CONTAINER} bash /app/scripts/recovery_restore.sh platform-baked"
    docker exec -u root "$CONTAINER" bash /app/scripts/recovery_restore.sh platform-baked >/dev/null
    info "platform layer fast-forwarded to the new baked tree"
    ;;
  missing)
    info "no /data/platform yet — the entrypoint creates it on the restart below"
    ;;
  *)
    warn "/data/platform has agent changes — NOT auto-synced."
    warn "The served backend stays on the PREVIOUS version until merged:"
    warn "ask the in-product agent to merge /app/app-baked into /data/platform,"
    warn "or discard agent edits with: recovery_restore.sh platform-baked"
    ;;
esac

# main.py resolves _static_dir at module load time, so the freshly
# rebuilt /data/shell/dist isn't actually served until uvicorn
# restarts. See "Shell rebuild + static-dir resolution" in CLAUDE.md.
intent "docker restart ${CONTAINER}  # so main.py re-resolves _static_dir"
docker restart "$CONTAINER" >/dev/null
info "waiting up to ${CUTOVER_WAIT_SECONDS}s for ${INTERNAL_BASE}/api/health after restart"
wait_for_cutover \
  "docker exec \"\$CONTAINER\" sh -c \"curl -s -o /dev/null -w '%{http_code}' '\${INTERNAL_BASE}/api/health'\" 2>/dev/null || echo 000" \
  "healthy" \
  "post-restart health check never returned 200"
info "waiting up to ${CUTOVER_WAIT_SECONDS}s for ${INTERNAL_BASE}/api/ready after restart"
wait_for_cutover "ready_code" "writer ready" \
  "post-restart readiness check never returned 200 — the chat-persistence writer is not serving"

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
# source changed → rebuild_shell.sh legitimately produces an identical
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
  warn "rebuild_shell.sh didn't no-op. Container is recreated + healthy regardless."
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
  # --skip-build: we didn't build, so don't compare against a possibly stale,
  # shell-inherited BUILD_SHA — just report what's serving.
  info "backend sha: ${served:-<none>} (no build this run; not compared)"
fi

# ── served-sha == origin/main assertion (prod only) ────────────────────
# The pre-flight guard refused to BUILD an unpushed commit, but assert the same
# invariant on the OTHER end too: what's actually SERVING must equal origin/main's
# tip. They can drift even past the pre-flight — e.g. a sibling pushed a newer
# main mid-deploy, or this deploy ran with --allow-unpushed. If the served sha
# isn't origin/main's tip, the operator must push (or pull+redeploy) or the next
# deploy-from-main reverts what's live. Warn loudly; never FAIL an
# already-succeeded deploy (the code IS serving and healthy — this is a
# push-hygiene nudge, not a runtime fault). prod-only: the test container has no
# origin/main contract.
if [ "$TARGET" = "prod" ] && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  origin_main_sha=$(git -C "$REPO_ROOT" rev-parse origin/main 2>/dev/null || echo "")
  served_clean="${served%-dirty}"  # the build stamps -dirty on an unclean tree
  if [ -z "$origin_main_sha" ]; then
    warn "could not resolve origin/main — skipping the served-sha==origin/main check."
  elif [ -z "$served_clean" ] || [ "$served_clean" = "unknown" ]; then
    info "served sha unknown — cannot compare to origin/main (provenance stamp missing)."
  elif [ "$served_clean" = "$origin_main_sha" ]; then
    ok "served sha == origin/main tip (${origin_main_sha:0:18}…) — prod is on pushed main"
  else
    warn "served sha ${served_clean:0:18}… != origin/main tip ${origin_main_sha:0:18}…"
    warn "what's LIVE is not origin/main's tip. PUSH it (git push origin HEAD:main) or the"
    warn "next deploy-from-main will REVERT this build. Deploy itself is healthy."
  fi
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

printf '\n%sdeploy complete%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
