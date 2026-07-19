#!/bin/sh
# Entrypoint: runs as root to fix volume permissions, then drops to
# the 'mobius' user for the actual server.  The non-root user allows
# --dangerously-skip-permissions in the Claude CLI.

# Stop cron on container shutdown so it doesn't orphan processes.
cleanup() { kill "$(cat /var/run/crond.pid 2>/dev/null)" 2>/dev/null; }
trap cleanup TERM INT

# Ensure /data and key subdirectories exist and are writable by mobius.
# Railway (and similar platforms) mount a fresh volume at /data owned by
# root — the dirs from the Dockerfile are replaced by the empty mount.
mkdir -p /data/db /data/apps /data/app-secrets /data/compiled /data/shared /data/logs /data/cron-logs /data/cli-auth /data/agent-browser-profiles /data/platform /data/run
# Per-boot fail-closed proof. FastAPI lifespan recreates this only after every
# discovered managed schedule has been converged through the common runner.
rm -f /data/run/app-cron-supervision-ready

# /data/agent-browser-profiles holds PER-CHAT Chrome user-data dirs
# (chat-<chat_id>/...) for agent-browser. The path is set per-chat by
# `app.chat._build_subprocess_env` so the agent's repeated screenshots
# within one chat reuse cached SW + assets + warm bundle (faster + a
# closer match to the partner's persistent PWA state). Per-chat
# isolation avoids the lock conflict that would happen if two parallel
# agent chats both tried to launch Chrome against a shared dir.

# -----------------------------------------------------------------------
# PHASE 1: Platform layer — served directly from /data/platform
#
# /data/platform/ is the agent-editable, git-tracked whole mobius repo.
# uvicorn serves its backend with `cd /data/platform/backend && uvicorn
# app.main:app`. There is no normal /app/app symlink swap and no
# each-boot copy-over of an existing platform tree.
#
# Fallback invariant: if /data/platform/backend/app exists but cannot import,
# or the repo is missing/corrupt, preserve it untouched and serve the baked
# backend floor from /app/platform-baked/backend/app via a degraded /app/app
# symlink. recoveryd remains the outer recovery floor.
# -----------------------------------------------------------------------

# PHASE 3: Boot-attempt counter. Written BEFORE starting uvicorn so a
# crash during startup (or a SIGKILL before the health probe writes the
# success sentinel) increments the count on the next boot. Counter is a
# plain integer in /data/.boot-attempt. On >=3 failures without an
# intervening /data/.last-successful-boot reset, we trigger a
# platform-baked restore and reset the counter, then log a flag that
# /api/debug/status surfaces.
#
# The counter file stores "N TIMESTAMP" — two fields so we can correlate
# crash times in the log. We read just the first field.
_boot_counter=0
if [ -f /data/.boot-attempt ]; then
  _boot_counter=$(cut -d' ' -f1 /data/.boot-attempt 2>/dev/null || echo 0)
  # Validate: must be a non-negative integer.
  case "$_boot_counter" in
    ''|*[!0-9]*) _boot_counter=0 ;;
  esac
fi
_boot_counter=$((_boot_counter + 1))
echo "$_boot_counter $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.boot-attempt
# chown deferred until after the broad /data chown below; done explicitly
# here only if that chown is not going to happen (Railway fallback path).

# If the counter reached the threshold, trigger automatic platform-baked
# restore so a broken platform doesn't brick the container in a crash
# loop. Threshold = 3 because a transient OOM or SIGKILL can cause 1-2
# false failures; three consecutive failures without a health success
# strongly implies the platform code itself is broken.
if [ "$_boot_counter" -ge 3 ] && [ -f /data/.last-successful-boot ]; then
  echo "PLATFORM-RESTORE: boot-attempt counter = $_boot_counter, re-cloning platform..." >&2
  # Crash-loop escape hatch: the platform imported OK (else the probe would
  # have already fallen back to baked) but keeps crashing at runtime. Move the
  # broken tree ASIDE so the next boot re-clones a fresh canonical
  # /data/platform. Non-destructive: the broken tree is preserved at a
  # TIMESTAMPED /data/platform.crashloop-prev.<ts> for inspection/recovery, not
  # deleted. A one-slot .crashloop-prev would let a SECOND crash-loop delete the
  # first preserved tree before the owner could inspect it, so we timestamp each
  # quarantine and keep only the newest few. (slice B's deploy=rebase
  # reconciliation will refine this.)
  _cl_ts=$(date -u +%Y%m%dT%H%M%SZ)
  if [ -e /data/platform ] && [ -n "$(ls -A /data/platform 2>/dev/null)" ] &&
     mv /data/platform "/data/platform.crashloop-prev.$_cl_ts" 2>/dev/null; then
    echo "PLATFORM-RESTORE: broken tree moved to /data/platform.crashloop-prev.$_cl_ts; next boot re-clones." >&2
    echo "crashloop-reclone /data/platform.crashloop-prev.$_cl_ts $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.platform-restore-active
    chown mobius:mobius /data/.platform-restore-active 2>/dev/null || true
    # Retention cap: keep the newest 3 crashloop quarantines, prune older ones.
    # Pruning runs AFTER the move so the tree just preserved this boot is always
    # among the kept copies — we never delete the only/newest copy.
    ls -1dt /data/platform.crashloop-prev.* 2>/dev/null | tail -n +4 | while IFS= read -r _old; do
      rm -rf "$_old" 2>/dev/null || true
    done
  else
    echo "PLATFORM-RESTORE: no /data/platform to move aside (or mv failed); serving baked floor." >&2
  fi
  # Reset counter after the restore attempt regardless of success, so
  # the next boot gets a clean slate. If the restore fixed things, the
  # health probe will write last-successful-boot and suppress further
  # auto-restores. If it didn't fix things, we'll restore again after 3
  # more attempts (an explicit loop so the operator can see what's
  # happening via the counter file).
  echo "0 $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.boot-attempt
  _boot_counter=0
elif [ "$_boot_counter" -ge 3 ] && [ ! -f /data/.last-successful-boot ]; then
  # Fresh volume or first-ever boot — last-successful-boot not yet written.
  # Don't trigger restore on what is literally the first few boots.
  # Reset counter so it doesn't grow forever on a slow-starting instance.
  echo "0 $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.boot-attempt
  _boot_counter=0
fi

if ! chown -R mobius:mobius /data 2>/dev/null; then
  echo "WARNING: chown -R mobius:mobius /data failed (likely a managed-volume platform like Railway)." >&2
  echo "WARNING: Falling back to chmod 1777 /data + 777 on subdirs so the mobius user can traverse" >&2
  echo "WARNING: AND create files at the /data top level. .secret-key and service-token.txt get an" >&2
  echo "WARNING: explicit 600 later in this script; cli-auth/ credential files (Claude + GitHub) are" >&2
  echo "WARNING: written 0600 at write time by the backend (auth.py / github_auth.py os.open), so the" >&2
  echo "WARNING: wide perms don't expose secrets. chmod 700 here would lock the mobius user out of" >&2
  echo "WARNING: /data entirely (root-owned dir, mode 700, no read/exec for non-owner) and break" >&2
  echo "WARNING: boot. chmod 755 was the previous fallback but broke runtime writes to top-level" >&2
  echo "WARNING: /data/service-token.txt — POST /api/auth/setup writes it as the mobius user, and" >&2
  echo "WARNING: it needs to be able to create files in /data, not just traverse." >&2
  chmod 1777 /data 2>/dev/null || true
  chmod -R 777 /data/db /data/apps /data/compiled /data/shared /data/logs /data/cron-logs /data/cli-auth /data/run 2>/dev/null || true
fi

# App credentials live outside ordinary app storage and the outer /data git
# repo. Prefer a mobius-owned 0700 root. On managed volumes that reject chown,
# use a root-owned write+traverse-only directory: mobius can create its own
# 0700 app directories, but cannot list the secret root. Payloads remain 0600.
if chown mobius:mobius /data/app-secrets 2>/dev/null; then
  chmod 700 /data/app-secrets 2>/dev/null || true
else
  chmod 733 /data/app-secrets 2>/dev/null || true
fi
find /data/app-secrets -mindepth 1 -type d -exec chmod 700 {} + 2>/dev/null || true
find /data/app-secrets -type f -exec chmod 600 {} + 2>/dev/null || true

# The /app/platform-baked/ clone stays root-owned + chmod a-w as the baked
# floor. /data/platform is handed to mobius so the agent can edit it and Python
# can write bytecode there.
#
# Why a+rX too: any baked-in Python source/script with a group-restrictive
# mode (host umask 027 leaves files at 640) would be unreadable by mobius.
# World-readable is the safe default for code that doesn't hold secrets.
# `/app/skill` (the constitution mobius reads for the system prompt) gets
# it too.
chmod -R a+rX /app/skill 2>/dev/null || true
# /app/shell-src remains the baked frontend source solely so
# /data/platform/frontend/node_modules can symlink to its installed
# node_modules. It carries a ~30k-file node_modules, so a blanket `chmod -R`
# there blocks boot past the health window. chmod only the git-sourced parts
# (src/public/config), pruning node_modules.
find /app/shell-src -path '*/node_modules' -prune -o -exec chmod a+rX {} + 2>/dev/null || true

# Auto-generate SECRET_KEY if not set (one-click deploy support).
# Persisted to /data so it survives container restarts. This must happen
# before the platform import probe because app.main loads settings at import.
if [ -z "$SECRET_KEY" ]; then
  if [ -f /data/.secret-key ]; then
    export SECRET_KEY=$(cat /data/.secret-key)
  else
    export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "$SECRET_KEY" > /data/.secret-key
    chmod 600 /data/.secret-key
    echo "Generated SECRET_KEY (persisted to /data/.secret-key)"
  fi
fi

if [ -z "$SECRET_KEY" ]; then
  echo "FATAL: SECRET_KEY is empty after generation attempt" >&2
  exit 1
fi

_public_port=${PORT:-8000}
_app_port=${MOBIUS_APP_PORT:-18000}
_recovery_port=${MOBIUS_RECOVERY_PORT:-18001}
_railway_gateway=0
if [ "${MOBIUS_RAILWAY_GATEWAY:-}" = "1" ] ||
   [ -n "${RAILWAY_ENVIRONMENT:-}" ] ||
   [ -n "${RAILWAY_ENVIRONMENT_ID:-}" ] ||
   [ -n "${RAILWAY_PROJECT_ID:-}" ] ||
   [ -n "${RAILWAY_SERVICE_ID:-}" ]; then
  _railway_gateway=1
fi

_app_pid=""
_gateway_pid=""
_recovery_pid=""
_restart_poller_started=0

_shutdown_railway_gateway() {
  _status="${1:-0}"
  trap - TERM INT
  cleanup
  [ -n "$_app_pid" ] && kill "$_app_pid" 2>/dev/null || true
  [ -n "$_recovery_pid" ] && kill "$_recovery_pid" 2>/dev/null || true
  [ -n "$_gateway_pid" ] && kill "$_gateway_pid" 2>/dev/null || true
  [ -n "$_app_pid" ] && wait "$_app_pid" 2>/dev/null || true
  [ -n "$_recovery_pid" ] && wait "$_recovery_pid" 2>/dev/null || true
  [ -n "$_gateway_pid" ] && wait "$_gateway_pid" 2>/dev/null || true
  exit "$_status"
}

_railway_child_running() {
  _child_pid="$1"
  _child_state=$(awk '/^State:/ { print $2; exit }' "/proc/${_child_pid}/status" 2>/dev/null) || return 1
  [ -n "$_child_state" ] && [ "$_child_state" != "Z" ]
}

_wait_for_railway_child_exit() {
  # Railway sees the gateway as pid1's public service, but the gateway can stay
  # alive after uvicorn crashes and return 502 forever. Watch BOTH essential
  # children. Any unexpected exit brings the whole container down so Railway's
  # ON_FAILURE policy can restart a coherent gateway/app/recovery process set.
  # kill -0 still succeeds for an exited child that has become a zombie. Read
  # procfs state so either critical process is reaped and reported promptly.
  while _railway_child_running "$_gateway_pid" && _railway_child_running "$_app_pid"; do
    sleep 1
  done

  if ! _railway_child_running "$_app_pid"; then
    wait "$_app_pid"
    _child_status=$?
    echo "FATAL: Railway app process exited with status $_child_status." >&2
  else
    wait "$_gateway_pid"
    _child_status=$?
    echo "FATAL: Railway gateway process exited with status $_child_status." >&2
  fi

  # A clean child exit is still a service failure: with ON_FAILURE, returning
  # zero would leave the stopped deployment down instead of restarting it.
  [ "$_child_status" -ne 0 ] || _child_status=1
  return "$_child_status"
}

_start_platform_restart_poller() {
  [ "$_restart_poller_started" -eq 1 ] && return 0
  (
    while true; do
      if [ -f /data/.platform-restart-requested ]; then
        rm -f /data/.platform-restart-requested 2>/dev/null || true
        echo "O1: platform-restart sentinel seen — sending SIGTERM to pid 1 (container restart)." >&2
        kill -TERM 1 2>/dev/null || true
        # pid1 is now draining + exiting; give it a moment, then stop polling.
        sleep 5
        exit 0
      fi
      sleep 2
    done
  ) &
  _restart_poller_started=1
}

_reenforce_protected_files() {
  [ -f /app/protected-files.txt ] || return 0
  while IFS= read -r line; do
    case "$line" in \#*|"") continue ;; esac
    case "$line" in
      /*) target="$line" ;;
      *)  continue ;;
    esac
    if [ -f "$target" ]; then
      chown root:root "$target" 2>/dev/null || true
      case "$target" in
        *.sh) chmod 555 "$target" 2>/dev/null || true ;;
        *)    chmod 444 "$target" 2>/dev/null || true ;;
      esac
    fi
  done < /app/protected-files.txt
}

_process_recover_pending() {
  [ -f /data/.recover-pending ] || return 0
  mode=$(cat /data/.recover-pending 2>/dev/null | tr -d '[:space:]')
  rm -f /data/.recover-pending
  restore_status=""
  case "$mode" in
    platform|platform-baked)
      echo "Recovery flag detected: $mode — running recovery_restore.sh as root..."
      if /app/scripts/recovery_restore.sh "$mode"; then
        restore_status="ok"
      else
        restore_status="failed"
        echo "WARNING: recovery_restore.sh $mode failed" >&2
      fi
      ;;
    "") : ;;
    *)
      restore_status="unknown-mode"
      echo "WARNING: unknown recovery flag mode: $mode" >&2
      ;;
  esac
  if [ -n "$restore_status" ]; then
    python3 -c "
import json, sys, time
entry = {
  'role': 'system',
  'content': f\"Recovery action '{sys.argv[1]}' completed: {sys.argv[2]}. Server restarted.\",
  'ts': int(time.time()),
}
print(json.dumps(entry, separators=(',', ':')))
" "$mode" "$restore_status" >> /data/recovery_chat.jsonl
    chown mobius:mobius /data/recovery_chat.jsonl 2>/dev/null || true
  fi
  _reenforce_protected_files
}

_process_recover_pending

if [ "$_railway_gateway" -eq 1 ]; then
  # Railway templates expose one public service with the /data volume attached.
  # Compose keeps recoveryd in its own container and Caddy routes /recover* to it;
  # on Railway the closest equivalent is a separate recoveryd process sharing the
  # same mounted /data, with this tiny gateway playing Caddy's routing role.
  # recoveryd remains the only recovery implementation.
  _recovery_allowed_hosts="${RECOVERY_ALLOWED_HOSTS:-}"
  if [ -z "$_recovery_allowed_hosts" ]; then
    _recovery_allowed_hosts="${RAILWAY_PUBLIC_DOMAIN:-${DOMAIN:-}}"
  fi
  echo "Railway gateway mode: public :$_public_port, app :$_app_port, recovery :$_recovery_port." >&2
  (
    while true; do
      DATA_DIR="${DATA_DIR:-/data}" \
      RECOVERY_PORT="$_recovery_port" \
      RECOVERY_PLATFORM_HEALTH_URL="http://127.0.0.1:${_app_port}/api/health" \
      RECOVERY_ALLOWED_HOSTS="$_recovery_allowed_hosts" \
        python3 -P /app/recovery/recoveryd.py
      _code=$?
      echo "WARNING: recoveryd exited with status $_code; restarting in 1s." >&2
      sleep 1
    done
  ) &
  _recovery_pid=$!

  python3 /app/scripts/railway_gateway.py \
    --port "$_public_port" \
    --app "http://127.0.0.1:${_app_port}" \
    --recovery "http://127.0.0.1:${_recovery_port}" &
  _gateway_pid=$!
  trap '_shutdown_railway_gateway 143' TERM INT
  _start_platform_restart_poller
fi

# -----------------------------------------------------------------------
# Platform layer selection (Phase 1).
# -----------------------------------------------------------------------
# The baked floor is the LAST-RESORT serve source (probe/git failure falls
# back to it). It must stay readable by mobix even though it is chmod a-w
# root-owned: a host umask of 027/077 can bake the copied sources at mode 640
# (a-w -> 440, root-only), and then the fallback uvicorn-as-mobius can't
# `import app.main` from it -> the safety net itself bricks. Re-open read+exec
# every boot (root can override a-w). See CLAUDE.md "Entrypoint permission
# defenses"; this replaces the old `chmod a+rX /app/app` the symlink model had.
chmod -R a+rX /app/platform-baked/backend/app /app/platform-baked/backend/scripts 2>/dev/null || true

_platform_root=/data/platform
_platform_backend=/data/platform/backend
_platform_app=${_platform_backend}/app
_baked_app=/app/platform-baked/backend/app
_baked_scripts=/app/platform-baked/backend/scripts
_use_platform=0
_serve_workdir=/app
_serve_source=baked
_served_sha="${BUILD_SHA:-unknown}"

# Env scrub shared by the import probe and the uvicorn exec so probe and serve
# stay identical. Drops ONLY inherited GIT_*/PYTHONPATH: a GIT_DIR/GIT_WORK_TREE
# leaked from the entrypoint would silently redirect the app's own git ops
# (app_git, platform_update, the /data repo) at the wrong repository, and a
# stray PYTHONPATH could shadow app.main. SECRET_KEY/DATABASE_URL/DATA_DIR are
# preserved (env -u removes only the named vars) so `import app.main` still
# resolves settings exactly as the served process does.
_env_scrub="env -u PYTHONPATH -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY -u GIT_COMMON_DIR -u GIT_NAMESPACE"

_platform_git_valid() {
  [ -d /data/platform/.git ] || return 1
  su -s /bin/sh mobius -c \
    'git -C /data/platform rev-parse --is-inside-work-tree >/dev/null &&
     git -C /data/platform rev-parse --verify HEAD >/dev/null'
}

_platform_import_probe_dir() {
  _probe_backend="$1"
  # Must mirror the real uvicorn exec EXACTLY (same user, cwd, env) so a probe
  # pass means the serve will import too. `import app.main` -> app.database ->
  # get_settings() REQUIRES SECRET_KEY (>=32 chars), which is exported above as
  # a plain env var. `su -s /bin/sh` (NON-login) inherits it; switching this or
  # the uvicorn exec to `su -`/`su -l`/`env -i` would drop SECRET_KEY and make
  # EVERY boot false-negative to baked (or brick uvicorn) — keep them identical.
  #
  # `timeout 60` bounds the probe: a module-level infinite loop or blocking
  # network call in agent-edited code would otherwise wedge boot forever (before
  # uvicorn, before crash-loop recovery can advance). A timeout-kill exits 124,
  # which counts as probe-fail -> serve baked. `$_env_scrub` drops the inherited
  # GIT_*/PYTHONPATH (see its definition); the uvicorn exec below applies the
  # IDENTICAL scrub so probe and serve stay byte-for-byte the same environment.
  su -s /bin/sh mobius -c \
    "cd '$_probe_backend' && $_env_scrub timeout 60 python3 -c 'import app.main'"
}

_platform_import_probe() {
  _platform_import_probe_dir /data/platform/backend
}

_platform_clear_empty_target() {
  if [ -e /data/platform ]; then
    if [ -n "$(ls -A /data/platform 2>/dev/null)" ]; then
      echo "PLATFORM LAYER WARNING: /data/platform became non-empty before install; refusing to overwrite it." >&2
      return 1
    fi
    rm -rf /data/platform 2>/dev/null || true
  fi
}

_restore_baked_dir_if_symlink() {
  _dst="$1"
  _src="$2"
  if [ -L "$_dst" ]; then
    rm -f "$_dst"
    mkdir -p "$_dst"
    cp -a "$_src/." "$_dst/"
    chmod -R a+rX "$_dst" 2>/dev/null || true
  fi
}

_platform_bootstrap() {
  # /data/platform is a REAL git clone of the canonical repo. A baked real clone
  # (not a copied tree + `git init`) can seed first boot offline while preserving
  # common ancestry with origin/main; if that seed is missing/invalid, the
  # network clone path below remains the update fallback. A fresh init has NO
  # common ancestor with origin/main, so a pushed branch would read as unrelated
  # histories. A clone shares ancestry, so `git diff origin/main` is exactly the
  # agent's edits and `git push` a branch is a clean PR. The whole repo lands
  # here and the agent edits it in place. One-time, first boot.
  _origin="${MOBIUS_PLATFORM_ORIGIN:-https://github.com/mobius-os/mobius.git}"
  echo "Platform layer: bootstrapping /data/platform (first boot)."
  # F1 non-destructive migration. We are here because /data/platform/backend/app
  # is absent, but a REAL prod volume may still carry the OLD overlay shape
  # (/data/platform/app + the agent's committed edits + .git, NOT backend/app),
  # which also reads as "no clone yet". `git clone` needs an empty/absent target,
  # so NEVER rm -rf a non-empty tree: MOVE an existing non-empty /data/platform
  # to a TIMESTAMPED quarantine the owner (or slice B's migration) can recover
  # the edits from, and only rm a genuinely EMPTY dir (a fresh volume, or one a
  # prior boot already quarantined then failed to clone into — so repeated failed
  # boots don't pile up empty quarantines). If the move fails, return non-zero
  # (serve baked, retry next boot) rather than deleting the only copy.
  if [ -e /data/platform ]; then
    if [ -n "$(ls -A /data/platform 2>/dev/null)" ]; then
      _quar="/data/platform.pre-clone.$(date -u +%Y%m%dT%H%M%SZ)"
      if mv /data/platform "$_quar" 2>/dev/null; then
        echo "PLATFORM MIGRATION: existing /data/platform preserved at $_quar (NOT deleted)." >&2
        echo "  Its agent edits + git history are intact; migrate them into the fresh clone as needed." >&2
        echo "pre-clone $_quar $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.platform-pre-clone-active
        chown mobius:mobius /data/.platform-pre-clone-active 2>/dev/null || true
      else
        echo "PLATFORM MIGRATION: FAILED to move existing /data/platform aside — refusing to delete it." >&2
        echo "  Serving baked floor; will retry the migration on the next boot." >&2
        return 1
      fi
    else
      rm -rf /data/platform 2>/dev/null || true
    fi
  fi

  # Primary first-boot path: seed the editable /data/platform clone from the
  # baked real clone in the image. Copy into a temp dir owned by mobius, validate
  # git + Python import there, then atomically rename into place. Any seed
  # failure falls through to the network clone path; it never overwrites a
  # non-empty /data/platform.
  rm -rf /data/platform.seeding.* 2>/dev/null || true
  _seeding="/data/platform.seeding.$(date -u +%Y%m%dT%H%M%SZ).$$"
  if [ -d /app/platform-baked/.git ] &&
     git -C /app/platform-baked rev-parse --is-inside-work-tree >/dev/null 2>&1 &&
     git -C /app/platform-baked rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "Platform layer: seeding /data/platform from /app/platform-baked."
    mkdir -p "$_seeding"
    chown mobius:mobius "$_seeding" 2>/dev/null || true
    if _SEEDING="$_seeding" su -s /bin/sh mobius -c \
      'cp -a /app/platform-baked/. "$_SEEDING"'; then
      chown -R mobius:mobius "$_seeding" 2>/dev/null || true
      if [ ! -f "$_seeding/.baked-sha" ]; then
        echo "${BUILD_SHA:-unknown}" > "$_seeding/.baked-sha"
        chown mobius:mobius "$_seeding/.baked-sha" 2>/dev/null || true
      fi
      if _SEEDING="$_seeding" su -s /bin/sh mobius -c \
        'git -C "$_SEEDING" rev-parse HEAD >/dev/null' &&
        _platform_import_probe_dir "$_seeding/backend"; then
        # /data/platform is absent here (the move-aside / empty-rm above
        # handled it); this helper only removes an absent/empty target and
        # refuses to touch a non-empty tree.
        if ! _platform_clear_empty_target; then
          rm -rf "$_seeding" 2>/dev/null || true
          return 1
        fi
        if mv -T "$_seeding" /data/platform 2>/dev/null; then
          echo "Platform layer: seed complete; serving /data/platform/backend."
          return 0
        fi
        echo "PLATFORM LAYER WARNING: could not move baked seed into place; trying network clone." >&2
      else
        echo "PLATFORM LAYER WARNING: baked platform seed failed validation; trying network clone." >&2
      fi
    else
      echo "PLATFORM LAYER WARNING: baked platform seed copy failed; trying network clone." >&2
    fi
    rm -rf "$_seeding" 2>/dev/null || true
  fi

  echo "Platform layer: cloning $_origin -> /data/platform (first boot fallback)."
  # --depth 1: shallow is fine — a later PR fetches origin + unshallows only if
  # it needs the merge base (same pattern as the app clone-update path).
  #
  # Clone into a TEMP dir and only move it into place on FULL success. A clone
  # that dies mid-checkout (disk-full, smudge-filter, interrupt) must never leave
  # a half-written /data/platform: a partial tree that still has backend/app would
  # be served broken, and a partial tree WITHOUT backend/app would be re-quarantined
  # as pre-clone data on every retry (accumulating). Build-then-atomic-move keeps
  # /data/platform either absent or fully ready. MOBIUS_PLATFORM_ORIGIN goes
  # through the ENV (not interpolated into the single-quoted su script) so a value
  # with a quote / shell metacharacter can't break the quoting or inject shell;
  # the temp path is ours (timestamp + pid) so it is safe to interpolate.
  rm -rf /data/platform.cloning.* 2>/dev/null || true
  _cloning="/data/platform.cloning.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mkdir -p "$_cloning"
  chown mobius:mobius "$_cloning" 2>/dev/null || true
  if ! MOBIUS_PLATFORM_ORIGIN="$_origin" _CLONING="$_cloning" su -s /bin/sh mobius -c '
    git clone --depth 1 "$MOBIUS_PLATFORM_ORIGIN" "$_CLONING" &&
    git -C "$_CLONING" config user.name "Mobius Agent" &&
    git -C "$_CLONING" config user.email "agent@mobius" &&
    git -C "$_CLONING" branch -f upstream HEAD
  '; then
    rm -rf "$_cloning" 2>/dev/null || true
    return 1
  fi
  su -s /bin/sh mobius -c "
    cd '$_cloning/frontend' 2>/dev/null || exit 0
    [ -e node_modules ] || [ -L node_modules ] ||
      ln -s /app/shell-src/node_modules node_modules || true
    mkdir -p dist 2>/dev/null || true
    cp -a /app/static/. dist/ 2>/dev/null || true
  " || true
  _build_sha=${BUILD_SHA:-unknown}
  if [ "$_build_sha" != "unknown" ]; then
    su -s /bin/sh mobius -c \
      "git -C '$_cloning' tag baked-${_build_sha} HEAD 2>/dev/null || true"
  fi
  echo "$_build_sha" > "$_cloning/.baked-sha"
  chown mobius:mobius "$_cloning/.baked-sha" 2>/dev/null || true
  # /data/platform is absent here (the move-aside / empty-rm above handled it);
  # this helper only removes an absent/empty target so the rename can't nest
  # temp inside a stray dir. Same-filesystem rename = atomic swap-in of a
  # fully-ready tree.
  if ! _platform_clear_empty_target; then
    rm -rf "$_cloning" 2>/dev/null || true
    return 1
  fi
  if ! mv -T "$_cloning" /data/platform 2>/dev/null; then
    echo "PLATFORM LAYER WARNING: could not move the fresh clone into place; retrying next boot." >&2
    rm -rf "$_cloning" 2>/dev/null || true
    return 1
  fi
  echo "Platform layer: clone complete; serving /data/platform/backend."
}

_platform_seed_test_checkout() {
  # docker-compose.test.yml mounts the checkout read-only at /workspace. The
  # image may have been built without a BUILD_SHA (notably build-push-action),
  # so its baked clone can legitimately point at origin/main instead of the PR
  # merge under test. Seed the disposable test volume from the mounted checkout
  # before normal platform selection, making backend imports and the warm
  # frontend watcher use the same revision as the tests.
  [ "${MOBIUS_TEST_RUNTIME:-0}" = "1" ] || return 0
  _test_source=${MOBIUS_TEST_PLATFORM_SOURCE:-}
  case "$_test_source" in
    /*) ;;
    *)
      echo "TEST RUNTIME FATAL: MOBIUS_TEST_PLATFORM_SOURCE must be an absolute path." >&2
      return 1
      ;;
  esac
  if [ ! -d "$_test_source/.git" ] || [ ! -d "$_test_source/backend/app" ]; then
    echo "TEST RUNTIME FATAL: $_test_source is not a complete git checkout." >&2
    return 1
  fi
  _test_head=$(git -c safe.directory="$_test_source" -C "$_test_source" \
    rev-parse --verify HEAD 2>/dev/null) || {
    echo "TEST RUNTIME FATAL: cannot resolve mounted checkout HEAD." >&2
    return 1
  }
  _expected_sha=${BUILD_SHA:-unknown}
  if printf '%s' "$_expected_sha" | grep -Eq '^[0-9a-fA-F]{40}$' &&
     [ "$_test_head" != "$_expected_sha" ]; then
    echo "TEST RUNTIME FATAL: mounted HEAD $_test_head != BUILD_SHA $_expected_sha." >&2
    return 1
  fi

  rm -rf /data/platform.test-seeding.* 2>/dev/null || true
  _test_seeding="/data/platform.test-seeding.$(date -u +%Y%m%dT%H%M%SZ).$$"
  _test_archive="${_test_seeding}.tar"
  mkdir -p "$_test_seeding"
  # Copy only the committed tree (not host node_modules/build output), then the
  # real git metadata so /api/version and runtime git operations see exact HEAD.
  if ! git -c safe.directory="$_test_source" -C "$_test_source" \
       archive --format=tar -o "$_test_archive" HEAD ||
     ! tar -xf "$_test_archive" -C "$_test_seeding"; then
    echo "TEST RUNTIME FATAL: could not copy mounted checkout tree." >&2
    rm -rf "$_test_seeding" "$_test_archive" 2>/dev/null || true
    return 1
  fi
  rm -f "$_test_archive"
  if ! cp -a "$_test_source/.git" "$_test_seeding/.git"; then
    echo "TEST RUNTIME FATAL: could not copy mounted checkout metadata." >&2
    rm -rf "$_test_seeding" 2>/dev/null || true
    return 1
  fi
  chown -R mobius:mobius "$_test_seeding" 2>/dev/null || true
  su -s /bin/sh mobius -c "
    cd '$_test_seeding/frontend' || exit 1
    [ -e node_modules ] || [ -L node_modules ] ||
      ln -s /app/shell-src/node_modules node_modules
    mkdir -p dist
    cp -a /app/static/. dist/
    printf '%s\n' '$_test_head' > '$_test_seeding/.baked-sha'
    git -C '$_test_seeding' rev-parse --verify HEAD >/dev/null
  " || {
    echo "TEST RUNTIME FATAL: copied checkout setup failed." >&2
    rm -rf "$_test_seeding" 2>/dev/null || true
    return 1
  }
  if ! _platform_import_probe_dir "$_test_seeding/backend"; then
    echo "TEST RUNTIME FATAL: copied checkout failed the import probe." >&2
    rm -rf "$_test_seeding" 2>/dev/null || true
    return 1
  fi

  # This path is reachable only in the explicitly disposable test runtime.
  # Replace rather than reuse the named volume so a prior run cannot leak an
  # older platform checkout into this test run.
  rm -rf /data/platform
  if ! mv -T "$_test_seeding" /data/platform; then
    echo "TEST RUNTIME FATAL: could not install copied checkout." >&2
    rm -rf "$_test_seeding" 2>/dev/null || true
    return 1
  fi
  echo "Test runtime: seeded /data/platform at $_test_head."
}

_platform_use_direct() {
  _use_platform=1
  _serve_source=platform
  _serve_workdir=$_platform_backend
  _restore_baked_dir_if_symlink /app/app "$_baked_app"
  _restore_baked_dir_if_symlink /app/scripts "$_baked_scripts"
  _served_sha=$(su -s /bin/sh mobius -c \
    'git -C /data/platform rev-parse HEAD' 2>/dev/null || echo unknown)
}

_platform_use_baked() {
  _use_platform=0
  _serve_source=baked
  _serve_workdir=/app
  _served_sha="${BUILD_SHA:-unknown}"
  export PYTHONDONTWRITEBYTECODE=1
  echo "PLATFORM LAYER WARNING: serving baked floor from $_baked_app." >&2
  echo "  /data/platform is preserved untouched and is NOT served." >&2
  echo "  Fix /data/platform or run recovery_restore.sh platform-baked." >&2
  if [ -e /app/app ] && [ ! -L /app/app ]; then
    find /app/app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    rm -rf /app/app
  fi
  ln -sfn "$_baked_app" /app/app
  _restore_baked_dir_if_symlink /app/scripts "$_baked_scripts"
}

if [ "${MOBIUS_TEST_RUNTIME:-0}" = "1" ]; then
  _platform_seed_test_checkout || exit 1
elif [ -n "${MOBIUS_TEST_PLATFORM_SOURCE:-}" ]; then
  echo "TEST RUNTIME FATAL: source override requires MOBIUS_TEST_RUNTIME=1." >&2
  exit 1
fi

chown -R mobius:mobius /data/platform 2>/dev/null || true

if [ ! -d "$_platform_app" ]; then
  if _platform_bootstrap && _platform_git_valid && _platform_import_probe; then
    _platform_use_direct
  else
    echo "PLATFORM LAYER WARNING: bootstrap did not produce an importable repo." >&2
    _platform_use_baked
  fi
else
  if _platform_git_valid; then
    if _platform_import_probe; then
      echo "Platform layer: import probe OK; serving /data/platform/backend."
      _platform_use_direct
    else
      echo "PLATFORM LAYER WARNING: import probe failed for /data/platform." >&2
      _platform_use_baked
    fi
  else
    echo "PLATFORM LAYER WARNING: /data/platform/.git missing or invalid." >&2
    _platform_use_baked
  fi
fi

printf '%s\n' "$_serve_source" > /tmp/serving-source
printf '%s\n' "$_served_sha" > /tmp/serving-sha
chmod 644 /tmp/serving-source /tmp/serving-sha 2>/dev/null || true

if [ "$_use_platform" -eq 1 ] && [ "${MOBIUS_TEST_RUNTIME:-0}" != "1" ]; then
  # Slice B deploy=rebase reconcile. A deploy ships a new image AND advances
  # canonical origin/main; fetch origin and replay the local edits onto the new
  # version NOW, before uvicorn imports the code, so the update goes live this
  # boot with no restart. Runs as mobius (writes /data; root would poison /data
  # ownership + hit git "dubious ownership"), cwd the served backend so `app`
  # imports resolve from the clone, under the IDENTICAL GIT_*/PYTHONPATH scrub
  # the import probe + uvicorn exec use. The function catches its own errors and
  # `|| true` guards the shell, so a reconcile failure never bricks boot; a
  # conflict/rollback leaves the pre-reconcile code on disk (aborted/reset) and
  # sets a flag Settings surfaces. The outer `timeout` is a last-resort bound set
  # ABOVE the reconcile's bounded operations: fetch 120 + unshallow 120 + rebase
  # 120 + probe 60 = 420, plus commit_local's own bounded git calls. Keep this
  # comfortably higher so internal timeouts fire FIRST; the post-timeout guard
  # below still cleans the tree if the outer kill ever wins. recoveryd remains
  # the outer floor.
  echo "Platform layer: reconciling /data/platform with origin (slice B deploy=rebase)..." >&2
  su -s /bin/sh mobius -c \
    "cd /data/platform/backend && $_env_scrub timeout 900 python3 -c \
     'from app import platform_update; print(platform_update.reconcile_clone_sync())'" \
    2>&1 || true
  # Reconcile itself is best-effort, but the post-reconcile guard is the final
  # safety boundary: if it cannot prove/reset the tree to a clean committed
  # state, do not import that tree. Exiting lets recoveryd/container policy use
  # the baked recovery floor instead of serving possibly half-applied code.
  if ! su -s /bin/sh mobius -c \
    "cd /data/platform/backend && $_env_scrub python3 -c \
     'from app import platform_update; print(platform_update.boot_guard_sync())'" \
    2>&1; then
    echo "Platform layer: boot guard failed; refusing to serve the platform tree." >&2
    exit 1
  fi
  # A fast-forward / rebase advanced main, so the served sha the /api/version and
  # /api/debug/serving routes report (written to /tmp/serving-sha above) must
  # reflect the reconciled HEAD, not the pre-reconcile clone tip.
  _served_sha=$(su -s /bin/sh mobius -c \
    'git -C /data/platform rev-parse HEAD' 2>/dev/null || echo "$_served_sha")
  printf '%s\n' "$_served_sha" > /tmp/serving-sha
  chmod 644 /tmp/serving-sha 2>/dev/null || true
fi

# SECRET_KEY drift detection.
#
# Compute sha256 of the ACTIVE key and compare against the persisted
# fingerprint from the previous boot. A mismatch means the key changed
# between boots — all outstanding JWTs (owner tokens, app tokens, service
# token, media tokens) are now invalid. This is intentional when the
# operator rotates the key; it is UNINTENTIONAL (and data-losing) when the
# auto-generate fallback fires on a previously-pinned instance (e.g.
# SECRET_KEY dropped from .env after a deploy-prod.sh run that stored it
# only in the container's .env, then the container was recreated from a
# fresh image without the .env copy — see the "SECRET_KEY drift fail-loud"
# memory note).
#
# On mismatch: emit a loud multi-line warning to the container log AND write
# /data/.secret-key-changed so the backend can surface it in /api/debug/status.
# On match (or first boot where no fingerprint exists): write/refresh the
# fingerprint file. Both paths run as root (we haven't dropped to mobius yet)
# so the fingerprint file stays root-owned with mode 640 — readable by mobius
# (the uvicorn process) but not world-readable.
_key_hash=$(echo -n "$SECRET_KEY" | sha256sum | cut -d' ' -f1)
_fp_file=/data/.secret-key-fingerprint
_changed_file=/data/.secret-key-changed

if [ -f "$_fp_file" ]; then
  _prev_hash=$(cat "$_fp_file" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$_prev_hash" ] && [ "$_key_hash" != "$_prev_hash" ]; then
    echo ""
    echo "========================================================="
    echo "WARNING: SECRET_KEY changed since last boot"
    echo "  All sessions and tokens have been invalidated."
    echo "  If this is UNINTENTIONAL:"
    echo "    1. Restore the previous SECRET_KEY value, OR"
    echo "    2. Pin SECRET_KEY in your .env file so it survives"
    echo "       image rebuilds (see the 'SECRET_KEY drift' note)."
    echo "  If this is INTENTIONAL (key rotation), users will need"
    echo "    to log in again and re-connect their AI providers."
    echo "========================================================="
    echo ""
    # Write the flag file so /api/debug/status can surface it.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$_changed_file"
  else
    # Key unchanged: clear any stale changed-flag from a previous anomalous boot.
    rm -f "$_changed_file"
  fi
fi

# Always write (or refresh) the fingerprint for the current key.
echo "$_key_hash" > "$_fp_file"
chmod 640 "$_fp_file"
chown root:mobius "$_fp_file" 2>/dev/null || true

# Cron is started by the health-probe process only AFTER FastAPI lifespan has
# completed. Lifespan rewrites replayed legacy app entries through the common
# leased/sandboxed runner; keeping the daemon stopped until then closes the
# boot window in which an old direct entry could fire unsupervised.

# Create cron log directory. Recursive chown so a file created by a
# root-run docker exec (the classic /data poisoning trap) can't
# permanently block mobius appends to an existing log.
mkdir -p /data/cron-logs
chown -R mobius:mobius /data/cron-logs

# Trim runaway cron logs at boot. App job scripts append here forever
# and rotation can't be imposed on agent-authored scripts, so the
# substrate reclaims its own volume at the one moment nothing writes.
# Growth between boots is accepted (MBs/month against a multi-GB
# volume); the supervisor's own app-jobs.log self-rotates at runtime.
# mktemp (O_EXCL) + the symlink guard keep this root-run loop from
# following a planted link in the mobius-writable directory.
for _log in /data/cron-logs/*.log; do
  [ -f "$_log" ] && [ ! -L "$_log" ] || continue
  if [ "$(stat -c%s "$_log" 2>/dev/null || echo 0)" -gt 8388608 ]; then
    _trim=$(mktemp "${_log}.XXXXXX") || continue
    tail -c 2097152 "$_log" > "$_trim" && mv -f "$_trim" "$_log"
    rm -f "$_trim"
    chown mobius:mobius "$_log" 2>/dev/null || true
  fi
done

# Generate (or refresh) a service token for cron scripts and sub-agents.
# Stored outside /data/shared/ so it's not accessible via the storage API.
# Token lifetime is 90 days; auto-regenerated when within 30 days of expiry.
# Only runs after first-time setup (requires an owner to exist).
_token=$(python3 -c "
from app.auth import create_access_token, decode_access_token
from app.database import SessionLocal
from app import models
from datetime import datetime, UTC, timedelta
import os

try:
    db = SessionLocal()
    owner = db.query(models.Owner).first()
    db.close()
except Exception:
    exit(0)
if not owner:
    exit(0)

# Check if existing token is still valid with more than 30 days remaining
# AND not revoked. A "sign out everywhere" bumps owner.token_epoch, which
# strands the on-disk service token even though it hasn't expired — so we
# re-mint when the stored token's epoch is behind the owner's current one.
# This restart is the documented recovery path for the service token after
# revocation (see routes/admin.py:sign_out_everywhere).
token_file = '/data/service-token.txt'
if os.path.exists(token_file):
    try:
        existing = open(token_file).read().strip()
        payload = decode_access_token(existing)
        exp = payload.get('exp', 0)
        remaining = exp - datetime.now(UTC).timestamp()
        fresh = remaining > 30 * 86400  # more than 30 days left
        current_epoch = payload.get('epoch', 0) == owner.token_epoch
        if fresh and current_epoch:
            exit(0)
    except Exception:
        pass  # expired or invalid — fall through to regenerate

token = create_access_token(
    {'sub': owner.username},
    expires_delta=timedelta(days=90),
    token_epoch=owner.token_epoch,
)
print(token, end='')
")
if [ -n "$_token" ]; then
  # Atomic write: tmp + rename. A crash mid-write would otherwise leave
  # an empty service-token.txt that cron jobs read as an empty token.
  echo "$_token" > /data/service-token.txt.tmp
  chown mobius:mobius /data/service-token.txt.tmp 2>/dev/null || true
  chmod 600 /data/service-token.txt.tmp
  mv /data/service-token.txt.tmp /data/service-token.txt
  echo "Service token written/refreshed at /data/service-token.txt"
fi

# The python block above imports app.database, which opens the SQLite
# engine as root and creates an empty /data/db/ultimate.db file owned
# by root. That then breaks uvicorn running as mobius. Re-chown /data/db
# here so the mobius user can always write.
#
# /data/logs is included as defense in depth: any root-run step that
# accidentally touches /data/logs/chat.log (initial logging
# configuration, a future entrypoint script, a startup probe) would
# leave it root-owned and uvicorn (mobius) could not subsequently
# write. Source of any specific occurrence is hard to pin down after
# the fact — this chown costs nothing and closes the class.
chown -R mobius:mobius /data/db /data/logs 2>/dev/null || true

# --- enforce protected file permissions ---
# Two categories of protected files (see protected-files.txt header):
#   1. Credential surfaces — chmod 444 root prevents agent tampering.
#   2. Boot / restore scripts — chmod 555 root keeps infra executable
#      but not agent-writable.
# Entries are absolute paths. Runs on every boot, not just first boot,
# to re-enforce after the chown sweep above.
if [ -f /app/protected-files.txt ]; then
  while IFS= read -r line; do
    # skip comments and empty lines
    case "$line" in \#*|"") continue ;; esac
    # Absolute paths only. Relative legacy shell entries are ignored.
    case "$line" in
      /*) target="$line" ;;
      *)  continue ;;
    esac
    if [ -f "$target" ]; then
      chown root:root "$target"
      # Shell scripts in the frozen list need to stay EXECUTABLE
      # (recovery_restore.sh, entrypoint.sh) so root can run them.
      # 555 = read + execute for everyone, no write. Non-executable
      # files (Python sources, CSS, HTML) stay 444.
      case "$target" in
        *.sh) chmod 555 "$target" ;;
        *)    chmod 444 "$target" ;;
      esac
    fi
  done < /app/protected-files.txt
fi

# Install the agent self-reminders cron dispatcher (feature 088). This
# is platform-level, not a mini-app, so it lives under a reserved
# _self-reminders/ slug and runs a tiny job.sh that execs the baked
# /app/scripts/self-reminders-dispatch.sh. The platform invokes its own trusted
# scaffold on every boot; app-owned init scripts are never executed. The job is
# create-if-absent so we never clobber an operator edit. DEFAULT OFF: the dispatcher
# itself fires nothing until /data/shared/self-reminders.enabled exists,
# so this installs the plumbing without firing any check-in.
SR_DIR=/data/apps/_self-reminders
if [ ! -f "$SR_DIR/init-cron.sh" ]; then
  su -s /bin/sh mobius -c "mkdir -p $SR_DIR" 2>/dev/null || true
  cat > "$SR_DIR/job.sh" <<'SRJOB'
#!/bin/bash
# Thin wrapper: cron runs this; the real logic is the baked dispatcher.
exec /app/scripts/self-reminders-dispatch.sh
SRJOB
  chmod +x "$SR_DIR/job.sh" 2>/dev/null || true
  chown -R mobius:mobius "$SR_DIR" 2>/dev/null || true
fi
su -s /bin/sh mobius -c \
  "bash /app/scripts/init-cron-scaffold.sh _self-reminders '*/5 * * * *'" \
  2>/dev/null || true

# Install the agent-browser profile reaper. Same reserved-slug pattern as
# _self-reminders above: a platform job, not a mini-app, so it lives under a
# `_`-prefixed slug and its job.sh only execs the baked script.
#
# Why this exists: agent-browser mints a per-chat Chromium profile under
# /data/agent-browser-profiles/chat-<id>/ and nothing reaped them. The cleanup
# script has shipped since its introduction but was never scheduled, so the
# tree only ever grew (measured 2026-07-19: 2.0 GiB across 134 profiles in
# ~4 days, inside the prod /data volume).
#
# The flags are the load-bearing part:
#   --delete                  the script is read-only by default
#   --include-existing-chats  WITHOUT this, only orphaned and soft-deleted-chat
#                             profiles are ever selected. 121 of the 134
#                             measured profiles belonged to EXISTING chats, so
#                             omitting it would leave the actual growth
#                             unbounded. With it, a profile is reaped only when
#                             the profile is stale AND the chat has been
#                             inactive that long AND no run is active.
# --include-non-chat is deliberately NOT passed: those are deliberately-named
# profiles (e.g. a long-lived `atlas-touch-*`), not per-chat scratch.
#
# Profiles are a cache and auth/session mirror for the agent's own browser --
# never partner transcript data -- so a reaped profile costs at most a re-login
# inside a chat nobody has touched in two weeks. The 14-day default lives in
# the script, not here, so operators tune one place.
PC_DIR=/data/apps/_profile-cleanup
if [ ! -f "$PC_DIR/init-cron.sh" ]; then
  su -s /bin/sh mobius -c "mkdir -p $PC_DIR" 2>/dev/null || true
  cat > "$PC_DIR/job.sh" <<'PCJOB'
#!/bin/bash
# Thin wrapper: cron runs this; the real logic is the baked cleanup script.
# Log every run, including the script's non-zero exit on a partial rmtree or a
# refused (fail-closed) delete. Cron has no mailer here, so this log is the only
# signal that a nightly --delete job ran and how it went.
log=/data/cron-logs/_profile-cleanup.log
mkdir -p /data/cron-logs
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) profile-cleanup start ==="
  python3 /app/scripts/agent-browser-profile-cleanup.py \
    --delete --include-existing-chats
  rc=$?
  echo "=== exit $rc ==="
  exit $rc
} >>"$log" 2>&1
PCJOB
  chmod +x "$PC_DIR/job.sh" 2>/dev/null || true
  chown -R mobius:mobius "$PC_DIR" 2>/dev/null || true
fi
# 04:17 daily -- off the :00/:30 marks the app jobs cluster on, and the work is
# a stat-walk over a few hundred directories, so it never needs to be frequent.
su -s /bin/sh mobius -c \
  "bash /app/scripts/init-cron-scaffold.sh _profile-cleanup '17 4 * * *'" \
  2>/dev/null || true

# Never execute app-owned init-cron.sh at boot. Older files are declarations,
# not trusted code: FastAPI lifespan parses their effective ENTRY (or the
# manifest schedule) and rewrites both the durable file and live crontab through
# app-job-runner.py. Cron remains stopped until that reconciliation proves every
# discovered live app schedule safe. This closes the pre-supervision path where
# arbitrary persisted shell could run merely because the container restarted.

# Ensure mobius's crontab has the full PATH at the top. Must run AFTER
# the trusted self-reminder scaffold; app schedule reconciliation preserves
# existing environment rows. Without PATH, cron's minimal /usr/bin:/bin can't resolve
# `#!/usr/bin/env node` shebangs (claude pre-2.1.119 was such a script);
# defensive against any future shebang script the agent creates.
# Uses `crontab -u` rather than direct file writes so cron's locking
# and file ownership are handled correctly.
if crontab -u mobius -l 2>/dev/null | grep -q '^PATH='; then
  : # already set
elif crontab -u mobius -l 2>/dev/null | grep -q .; then
  { echo 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    crontab -u mobius -l 2>/dev/null
  } | crontab -u mobius -
  echo "Added PATH= to mobius crontab"
fi

# Publish the per-deploy upstream-diff file (/data/shared/upstream-diff.txt).
python3 /app/scripts/init_agent_context.py

# One-time idempotent app-rename migration (mind->memory, dreaming->reflection)
# for EXISTING instances. MUST run before init_skills, which renames the
# agent-edited skill file in place, so the migration does not reseed a fresh
# file. Preserves each app's numeric id, so reports/storage are untouched. No-op
# on a fresh instance or one already migrated. Runs as mobius (writes /data + the
# mobius crontab; as root it would poison /data ownership + target root's crontab).
su -s /bin/sh mobius -c "bash /app/scripts/migrate-app-rename.sh" 2>&1 || true

# Bootstrap only the always-on per-chat summary directory. Optional graph
# memory, its seeds, and its `.ready` lifecycle belong to the installed Memory
# system app; base boot must not activate them.
python3 /app/scripts/init_chat_summaries.py

# Bootstrap the agent-editable skills layer (/data/shared/skills/). CREATE-IF-
# ABSENT like the graph — the agent (and the nightly Reflection agent) improve
# these skills, so a reseed must not clobber their edits. The system prompt
# (skill/core.md) points at these.
python3 /app/scripts/init_skills.py

# Theme: no starter file written here. /api/theme reads
# /data/shared/theme.css when present, otherwise falls through to
# theme.py:DEFAULT_THEME — the single source of truth for the
# platform default. Deleting /data/shared/theme.css cleanly reverts
# to that default; writing the file creates an override.

# Always write the gitignore so prior boots (which lacked .secret-key in
# the ignore list) get the updated rules before the next add/commit cycle.
cat > /data/.gitignore <<'EOF'
cli-auth/
app-secrets/
push/*.pem
push/*.json
push/*.txt
service-token.txt
.secret-key
.recovery-secret
.recovery-owner.json
.pm-commit
compiled/
db/
db.sqlite3
mobius.db
chats/
backups/
*.bak-*
apps/*/data/
apps/*/.git/
recovery_chat.jsonl
.recover-pending
agent-browser-profiles/
generated/
logs/
cron-logs/
# Memory is an optional system app, but while installed it owns a durable Git
# repository here. Keep the outer /data safety-net repo from treating that
# repository as an untracked submodule; Memory owns its history directly.
shared/memory/repository/
# platform/ has its OWN git repo; exclude it from the outer /data repo so
# `git add -A` from /data doesn't treat it as an untracked submodule. Its source
# is tracked by its own git, not /data's.
platform/
# Transient bootstrap scratch + quarantines (siblings of platform/, not
# matched by platform/). Never user data; never track them in the safety-net.
platform.seeding.*
platform.reseeding.*
platform.reseed-prev.*
platform.pre-clone.*
platform.crashloop-prev.*
# Phase 3 boot-state files — runtime counters, not content the agent manages.
.boot-attempt
.last-successful-boot
.platform-restore-active
.platform-upgrade-available
.platform-pre-clone-active
# Transient platform-update markers — runtime signals, never user data. If the
# outer /data repo is initialized while one of these exists, `git add -A` must
# not track it.
.platform-conflict
.platform-offline
.platform-restart-needed
.platform-apply-in-progress
.platform-restart-requested
.platform-rolled-back
.platform-reconcile-pre
.platform-reconcile.lock
EOF
chown mobius:mobius /data/.gitignore 2>/dev/null || true

# Drop accidental nested git repos under /data, but preserve the intentional
# per-app repos at /data/apps/<slug>/.git, Memory's optional graph repo at
# /data/shared/memory/repository/.git, and the platform git at
# /data/platform/.git. The outer /data repo ignores those repos so `git add -A`
# does not try to treat them as submodules, while their owning lifecycle can
# still keep history across container restarts.
# Contribution checkouts under /data/contrib and legacy /data/contributions are
# intentional durable repos too: prepared review cards point at their exact
# commits and the approved Send path re-verifies that history before pushing.
#
# The .pre-clone.<ts> / .crashloop-prev.<ts> quarantines are also preserved
# WHOLE (including their .git): they hold the agent's migrated-aside platform
# tree, and the whole point of the move-aside was to keep those edits AND their
# git history recoverable — this pruner must not silently eat that history.
find /data -regextype posix-extended -mindepth 2 -maxdepth 4 \
  -type d -name '.git' \
  ! -regex '/data/apps/[^/]+/\.git' \
  ! -regex '/data/shared/memory/repository/\.git' \
  ! -regex '/data/platform/\.git' \
  ! -path '/data/contrib/*' \
  ! -path '/data/contributions/*' \
  ! -regex '/data/platform\.pre-clone\..*' \
  ! -regex '/data/platform\.crashloop-prev\..*' \
  -prune -exec rm -rf {} + 2>/dev/null || true

# Idempotent re-chown of /data/.git BEFORE the if/else below — git
# refuses cross-owner operations with "dubious ownership", so any git
# command we run as mobius (the else branch's untrack loop) needs the
# repo mobius-owned first. A `docker pull` plus a recreated volume can
# leave a previously-mobius-owned /data/.git root-owned again (e.g.
# some recovery installs bake /data/.git into the image layer); without
# this the agent's commits also fail. No-op via `|| true` if /data/.git
# doesn't exist yet — the fresh-init branch below creates it and
# re-chowns in that case.
chown -R mobius:mobius /data/.git 2>/dev/null || true

if [ ! -d /data/.git ]; then
  su -s /bin/sh mobius -c '
    git init /data
    git -C /data config user.name "Mobius Agent"
    git -C /data config user.email "agent@mobius"
    git -C /data add -A
    git -C /data commit -m "init" --allow-empty
  '
  chown -R mobius:mobius /data/.git 2>/dev/null || true
else
  # Defensive: prior boots may have committed paths that the current
  # gitignore now covers (.secret-key landed in the gitignore after
  # it had already been committed; same story for the loose root-level
  # .db files). `git rm --cached` is idempotent — silently no-ops if
  # the path isn't in the index — so this is safe to re-run every boot.
  # Runs as mobius (the .git owner); the pre-chown above guarantees
  # ownership is correct.
  su -s /bin/sh mobius -c '
    for path in .secret-key db.sqlite3 mobius.db; do
      git -C /data rm --cached "$path" 2>/dev/null || true
    done
  '
fi

# Ensure the mobius user has a GLOBAL git identity, a gh config symlink,
# and a git credential helper — all as mobius, all re-asserted every boot
# because mobius's home (/home/mobius) is image-layer and ephemeral across
# container recreation. The local-repo config above only covers /data/.git;
# a global identity means `git commit` inside /data/apps/<slug>/ (own .git,
# or none) never fails with "Please tell me who you are."
#
# When GitHub is connected the identity attributes commits to the connected
# user, read from mobius-github.json (the backend writes it on connect; no
# jq in the image, so parse with python3). Deriving it from on-disk state
# each boot — not once at connect time — is what makes attribution survive
# recreation. When not connected, fall back to the Mobius Agent defaults.
# The ~/.config/gh symlink lets `gh` resolve the volume-backed token, and
# the credential helper lets a plain `git push` to github.com authenticate
# through it; with no token gh serves nothing and the push fails loudly.
su -s /bin/sh mobius -c '
  mkdir -p ~/.config
  ln -sfn /data/cli-auth/gh ~/.config/gh
  _gh_state=/data/cli-auth/gh/mobius-github.json
  gh_login=""
  gh_uid=""
  if [ -f "$_gh_state" ]; then
    gh_login=$(python3 -c "import json;print(json.load(open(\"$_gh_state\")).get(\"login\") or \"\")" 2>/dev/null)
    gh_uid=$(python3 -c "import json;v=json.load(open(\"$_gh_state\")).get(\"user_id\");print(\"\" if v is None else v)" 2>/dev/null)
  fi
  if [ -n "$gh_login" ]; then
    gh_email="${gh_login}@users.noreply.github.com"
    if [ -n "$gh_uid" ]; then
      gh_email="${gh_uid}+${gh_login}@users.noreply.github.com"
    fi
    git_name="$gh_login"
    git_email="$gh_email"
  else
    git_name="Mobius Agent"
    git_email="agent@mobius"
  fi
  git config --global user.name "$git_name"
  git config --global user.email "$git_email"
  for repo in /data /data/platform; do
    if [ -d "$repo/.git" ]; then
      git -C "$repo" config user.name "$git_name" || true
      git -C "$repo" config user.email "$git_email" || true
    fi
  done
  git config --global credential.helper "!gh auth git-credential"
' 2>/dev/null || true

# Only copy the pm-commit helper if missing or if the image version
# differs from the on-disk copy. Blindly overwriting on every boot wipes
# any instance-local edits the agent or operator may have made.
if [ ! -f /data/.pm-commit ] || ! cmp -s /app/scripts/pm-commit /data/.pm-commit; then
  cp /app/scripts/pm-commit /data/.pm-commit
  chmod +x /data/.pm-commit
  chown mobius:mobius /data/.pm-commit 2>/dev/null || true
fi
# POSIX shells don't search `.` in $PATH, so `pm-commit "msg"` from /data
# would otherwise fail with "command not found". The symlink makes the
# natural invocation work from any cwd.
ln -sf /data/.pm-commit /usr/local/bin/pm-commit


# Recovery may have scheduled a restore during this boot before the app server
# started. The early call near the top handles normal previous-boot restores;
# this second call is a no-op unless a fresh flag appeared while setup ran.
_process_recover_pending

# Install the codex-plugin-cc into the agent's CLAUDE_CONFIG_DIR if
# not yet present. Source is baked into the image at /opt/codex-plugin-cc
# (pinned in the Dockerfile via `git clone --branch v1.0.4`). The install
# writes settings.json + plugins/ under /data/cli-auth/claude/, which
# is volume-backed — so we re-install automatically if the volume is
# wiped. Runs as mobius so all resulting files are mobius-owned and
# the agent's CLI can update them if it ever runs `plugin update`.
# A failure here is non-fatal: the agent still works without the
# plugin, the user just doesn't get the /codex:* slash commands or
# codex:codex-rescue subagent.
if [ ! -f /data/cli-auth/claude/plugins/installed_plugins.json ]; then
  mkdir -p /data/cli-auth/claude
  chown mobius:mobius /data/cli-auth /data/cli-auth/claude 2>/dev/null || true
  su -s /bin/sh mobius -c "CLAUDE_CONFIG_DIR=/data/cli-auth/claude claude plugin marketplace add /opt/codex-plugin-cc" \
    && su -s /bin/sh mobius -c "CLAUDE_CONFIG_DIR=/data/cli-auth/claude claude plugin install codex@openai-codex" \
    || echo "WARNING: codex-plugin-cc install failed (non-fatal)" >&2
fi

# Drop to non-root user and start the server.
# umask 022: newly created files default to 644 (rw-r--r--) so the
# mobius server can read script/source files copied into the image at
# build time (whose default chmod inherits from the host umask of the
# user who created them — sometimes 027/077). Without this, runner
# scripts created via Write that ship with mode 640 are unreadable by
# the mobius user at runtime, causing subprocess "permission denied"
# failures that look like generic CLI crashes.
umask 022

# O1: platform-restart sentinel poller (the recoveryd handshake).
#
# The frozen recovery process cannot depend on app imports or app signal paths.
# So a Tier-1 restore in recoveryd writes /data/.recover-pending=<mode> and
# then the restart sentinel /data/.platform-restart-requested; this poller is
# the in-container half that acts on it. On sight it removes the sentinel and
# `kill -TERM 1` — SIGTERM to pid1. In compose, docker-init forwards SIGTERM to
# uvicorn; on Railway, the entrypoint shell traps it, stops the gateway/app/
# recoveryd children, and exits non-zero. Either way the service restarts; the
# fresh entrypoint processes /data/.recover-pending AS ROOT and reverts
# /data/platform, so the platform comes back on fixed code. NO Docker socket is
# involved.
#
# This subshell is forked before the server starts. In compose, the shell later
# execs uvicorn and the poller is reparented to docker-init; on Railway, the
# shell stays as a tiny supervisor for gateway/app/recoveryd. It is the SOLE
# writer that consumes /data/.platform-restart-requested; recoveryd is the sole
# writer that creates it. The 2s cadence bounds restore latency without
# busy-looping.
_start_platform_restart_poller

# PHASE 3: Background health probe — writes /data/.last-successful-boot
# and resets the boot-attempt counter once the server is confirmed
# healthy. This is the "success" signal that prevents false-positive
# crash-loop detection.
#
# The probe polls the app's /api/health (127.0.0.1, never routed outside the
# container) with a 90-second timeout (generous for slow first-boots with DB
# migrations). In Railway gateway mode it probes the private app port, not
# /recover/health, so recovery can be live while the boot-attempt counter still
# correctly treats a broken app as a failed platform boot. On success it writes
# the sentinel and zeroes the counter. It does NOT restart uvicorn or take any
# other action — it is purely the signal that "this boot succeeded."
#
# pgrep self-match trap: we do NOT use `until ! pgrep -f uvicorn` or
# similar — the probe waits on the outcome (/api/health 200), not on a
# process name. See feedback_pgrep_self_match_in_monitor_loops.md.
_health_url="http://127.0.0.1:${_public_port}/api/health"
if [ "$_railway_gateway" -eq 1 ]; then
  _health_url="http://127.0.0.1:${_app_port}/api/health"
fi
(
  # Wait up to 90 seconds for /api/health to return 200.
  for i in $(seq 1 90); do
    if curl -sf "$_health_url" > /dev/null 2>&1; then
      # Lifespan has completed, including app-cron supervision. Start cron now
      # (as root; entries themselves execute as mobius).
      if [ -f /data/run/app-cron-supervision-ready ]; then
        cron
        if command -v pgrep > /dev/null 2>&1; then
          pgrep -x cron > /dev/null || echo "WARNING: cron daemon failed to start" >&2
        fi
      else
        echo "WARNING: app cron supervision did not complete; cron remains disabled (fail closed)" >&2
      fi
      # Health probe passed — record the success sentinel and reset counter.
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.last-successful-boot
      echo "0 $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.boot-attempt
      # Remove the restore-active flag if set — the server is healthy now.
      rm -f /data/.platform-restore-active 2>/dev/null || true
      echo "Platform health probe: /api/health OK — boot success recorded."
      exit 0
    fi
    sleep 1
  done
  # 90 seconds elapsed without a 200 — uvicorn failed to start.
  echo "Platform health probe: /api/health did not return 200 within 90s — boot failure." >&2
  # Leave .boot-attempt in place (already incremented before uvicorn).
  exit 1
) &

# --timeout-graceful-shutdown bounds uvicorn's SIGTERM drain. Without it,
# uvicorn waits FOREVER for open connections to close on SIGTERM — and the chat
# SSE stream never closes on its own — so an in-app restart (the Settings
# Restart button SIGTERMs this worker) hangs the process in shutdown limbo: it
# stops serving but never exits, tini never exits, and the container never
# restarts ("pressed Restart, server never came back"). Bounding the drain makes
# every SIGTERM-based restart reliably cycle the container.
if [ "$_railway_gateway" -eq 1 ]; then
  _uvicorn_flags="--host 127.0.0.1 --port $_app_port --timeout-graceful-shutdown 10"
else
  _uvicorn_flags="--host 0.0.0.0 --port $_public_port --timeout-graceful-shutdown 10"
fi
# `$_env_scrub` is applied to the uvicorn exec so the served process runs with
# the SAME scrubbed GIT_*/PYTHONPATH the import probe validated — a leaked
# GIT_DIR must not redirect the app's git ops, and no stray PYTHONPATH may
# shadow app.main. It wraps uvicorn on both the platform and baked serve paths.
if [ "$_use_platform" -eq 1 ]; then
  _start_cmd="umask 022 && cd $_serve_workdir"
  _start_cmd="$_start_cmd && exec $_env_scrub uvicorn app.main:app $_uvicorn_flags"
else
  _start_cmd="umask 022 && export PYTHONDONTWRITEBYTECODE=1"
  _start_cmd="$_start_cmd && cd $_serve_workdir"
  _start_cmd="$_start_cmd && exec $_env_scrub uvicorn app.main:app $_uvicorn_flags"
fi

if [ "$_railway_gateway" -eq 1 ]; then
  su -s /bin/sh mobius -c "$_start_cmd" &
  _app_pid=$!
  if ! kill -0 "$_gateway_pid" 2>/dev/null; then
    echo "FATAL: Railway gateway exited before app startup." >&2
    _shutdown_railway_gateway 1
  fi
  _wait_for_railway_child_exit
  _railway_status=$?
  _shutdown_railway_gateway "$_railway_status"
fi

exec su -s /bin/sh mobius -c \
  "$_start_cmd"
