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
mkdir -p /data/db /data/apps /data/compiled /data/shared /data/shell /data/logs /data/cron-logs /data/cli-auth /data/agent-browser-profiles /data/platform

# /data/agent-browser-profiles holds PER-CHAT Chrome user-data dirs
# (chat-<chat_id>/...) for agent-browser. The path is set per-chat by
# `app.chat._build_subprocess_env` so the agent's repeated screenshots
# within one chat reuse cached SW + assets + warm bundle (faster + a
# closer match to the partner's persistent PWA state). Per-chat
# isolation avoids the lock conflict that would happen if two parallel
# agent chats both tried to launch Chrome against a shared dir.

# -----------------------------------------------------------------------
# PHASE 1: Platform layer — dual-layer boot
#
# /data/platform/ is the agent-editable, git-tracked copy of the backend
# Python source and scripts. On first boot (or after a wiped volume) we
# copy from the baked read-only floor at /app/app-baked/ and
# /app/scripts-baked/. After that the agent's edits live here and
# survive across container restarts and image upgrades.
#
# Invariant: /app/app and /app/scripts are ALWAYS symlinks pointing at
# the live platform copies. uvicorn runs as `cd /app && uvicorn
# app.main:app` — Python adds /app to sys.path, finds the `app` package
# through the symlink. Symlink indirection is transparent to the Python
# importer: `from app.config import ...` resolves the symlink and loads
# the real file from /data/platform/app/. __pycache__ directories land
# inside /data/platform/app/ (mobius-owned) so the agent's edits and
# Python's compiled bytecache are co-located.
#
# Fallback invariant: if /data/platform/app/main.py is absent or doesn't
# parse, we boot from baked directly (log loudly) so the recovery surface
# stays reachable even on a completely corrupt platform tree.
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
  echo "PLATFORM-RESTORE: boot-attempt counter = $_boot_counter, restoring from baked floor..." >&2
  # Clear pycache in the live platform tree before the restore so stale
  # bytecache doesn't survive the copy-over.
  find /data/platform -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  find /data/platform -name '*.pyc' -delete 2>/dev/null || true
  # Restore platform from baked. This runs as root so it can overwrite
  # protected (root-owned 444) files. After the restore, re-enforce
  # protected-file perms.
  mkdir -p /data/platform/app /data/platform/scripts
  if cp -a /app/app-baked/. /data/platform/app/ && cp -a /app/scripts-baked/. /data/platform/scripts/; then
    # Re-open write access (baked copies are chmod a-w; cp -a preserves that).
    chmod -R u+w /data/platform/app /data/platform/scripts 2>/dev/null || true
    echo "PLATFORM-RESTORE: baked restore succeeded." >&2
    # Record the restore event in a flag file that debug/status surfaces.
    echo "baked-restore $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /data/.platform-restore-active
    chown mobius:mobius /data/.platform-restore-active 2>/dev/null || true
    # git commit the restore so history records what happened.
    if [ -d /data/platform/.git ]; then
      su -s /bin/sh mobius -c '
        git -C /data/platform add -A
        git -C /data/platform commit -m "auto: crash-loop restore from baked floor" 2>/dev/null || true
      '
    fi
  else
    echo "PLATFORM-RESTORE: baked restore FAILED — continuing with potentially broken code." >&2
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
  echo "WARNING: AND create files at the /data top level. Per-file chmods later in this script set" >&2
  echo "WARNING: explicit 600 on sensitive paths (.secret-key, service-token.txt, cli-auth/) so the" >&2
  echo "WARNING: wide perms don't expose secrets. chmod 700 here would lock the mobius user out of" >&2
  echo "WARNING: /data entirely (root-owned dir, mode 700, no read/exec for non-owner) and break" >&2
  echo "WARNING: boot. chmod 755 was the previous fallback but broke runtime writes to top-level" >&2
  echo "WARNING: /data/service-token.txt — POST /api/auth/setup writes it as the mobius user, and" >&2
  echo "WARNING: it needs to be able to create files in /data, not just traverse." >&2
  chmod 1777 /data 2>/dev/null || true
  chmod -R 777 /data/db /data/apps /data/compiled /data/shared /data/shell /data/logs /data/cron-logs /data/cli-auth 2>/dev/null || true
fi

# Hand the live backend + scripts to mobius so the agent can edit
# them at runtime. The /app/app-baked/ and /app/scripts-baked/ copies
# stay root-owned + chmod a-w as the recovery floor (recovery_restore.sh
# copies from there if the agent breaks the live copy).
#
# Why chown here every boot: docker layer caching gives us back the
# baked image perms (root-owned) on container restart. The chown is
# idempotent. Frozen files are re-chmod-444'd a few lines down.
#
# Why a+rX too: any baked-in Python source/script with a group-
# restrictive mode (host umask 027 leaves files at 640) would be
# unreadable by mobius. World-readable is the safe default for code
# that doesn't hold secrets. `/app/skill` (the constitution mobius reads
# for the system prompt) gets it too.
chmod -R a+rX /app/skill 2>/dev/null || true
# /app/shell-src (the stock source a shell refresh copies into /data/shell
# as mobius) also needs a+rX, but it carries a ~30k-file node_modules — a
# blanket `chmod -R` there blocks boot past the health window. chmod only the
# git-sourced parts (src/public/config), pruning node_modules.
find /app/shell-src -path '*/node_modules' -prune -o -exec chmod a+rX {} + 2>/dev/null || true

# -----------------------------------------------------------------------
# Platform layer init (Phase 1).
#
# Step 1: Determine whether to boot from /data/platform or fall back to
# the baked /app/app-baked/ floor. The sanity check is whether
# /data/platform/app/main.py exists AND compiles cleanly. This is the
# single most crash-relevant file (a SyntaxError here means uvicorn
# can't import app.main and dies immediately with no health response).
# -----------------------------------------------------------------------
_platform_app=/data/platform/app
_platform_scripts=/data/platform/scripts
_baked_app=/app/app-baked
_baked_scripts=/app/scripts-baked
_use_platform=0

# Sanity check: does a usable platform tree exist?
# We require main.py to exist AND to parse. python3 -c "compile(...)" is
# cheaper than importing the whole app; it catches syntax errors without
# running any code.
_platform_sane() {
  [ -f "$_platform_app/main.py" ] && \
    python3 -c "
import ast, sys
try:
  ast.parse(open('$_platform_app/main.py').read())
  sys.exit(0)
except SyntaxError as e:
  print('PLATFORM SANITY FAIL: main.py SyntaxError:', e, file=sys.stderr)
  sys.exit(1)
" 2>&1
}

if [ -d "$_platform_app" ] && _platform_sane > /dev/null 2>&1; then
  _use_platform=1
  echo "Platform layer: /data/platform is present and healthy — serving from there."
else
  if [ ! -d "$_platform_app" ]; then
    echo "Platform layer: /data/platform/app absent — first boot, copying from baked floor..."
  else
    echo "PLATFORM LAYER WARNING: /data/platform/app/main.py failed sanity check — falling back to baked floor." >&2
    echo "  The agent's platform edits are preserved in /data/platform but NOT served." >&2
    echo "  To restore: run recovery_restore.sh platform-baked, or fix main.py and restart." >&2
  fi
fi

# Step 2: If we should use the platform layer but it's missing (first boot
# or wiped volume), copy from the baked floor.
if [ "$_use_platform" -eq 0 ] && [ ! -d "$_platform_app" ]; then
  # First boot: copy baked app and scripts into /data/platform.
  mkdir -p "$_platform_app" "$_platform_scripts"
  cp -a "$_baked_app/." "$_platform_app/"
  cp -a "$_baked_scripts/." "$_platform_scripts/"
  # The baked copies have chmod a-w (Dockerfile: chmod -R a-w /app/app-baked
  # /app/scripts-baked) — cp -a preserves those read-only modes. Re-open
  # write access for the mobius user so the agent can edit and Python can
  # write __pycache__ entries. The protected-file enforcement loop below
  # re-locks the specific frozen files after this broad chmod.
  chmod -R u+w "$_platform_app" "$_platform_scripts" 2>/dev/null || true
  # Clear any stale __pycache__ from the baked copy — bytecache is
  # path-dependent and the path has changed from /app/app to /data/platform/app.
  find "$_platform_app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  find "$_platform_app" -name '*.pyc' -delete 2>/dev/null || true
  find "$_platform_scripts" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  chown -R mobius:mobius /data/platform 2>/dev/null || true
  echo "Platform layer: initial copy complete."
  _use_platform=1
fi

# Step 3: Symlink swap — make /app/app and /app/scripts point at the
# platform tree so uvicorn's `cd /app && uvicorn app.main:app` picks up
# the right code. Python resolves the symlink transparently; __pycache__
# will land inside /data/platform/app/ (mobius-owned).
#
# We replace the existing /app/app and /app/scripts directories with
# symlinks. If they're already symlinks pointing at the right target, this
# is a no-op (ln -sfn handles re-pointing safely).
#
# IMPORTANT: if the sanity check failed (_use_platform=0), we do NOT
# create the symlink — /app/app and /app/scripts stay as real directories
# (the baked originals) so uvicorn still boots even if /data/platform is
# corrupt. The loud log above alerts the operator.
if [ "$_use_platform" -eq 1 ]; then
  # Replace real dir with symlink only if not already a symlink to the
  # right target. Guard: if /app/app is a non-symlink dir, rename it to
  # _baked (for the first ever swap); ln -sfn then creates the symlink.
  # On subsequent boots it's already a symlink — ln -sfn re-points it.
  if [ -d /app/app ] && [ ! -L /app/app ]; then
    # First-ever swap: /app/app is a real dir. We need to remove it before
    # we can create the symlink. The baked copies already exist in
    # /app/app-baked/ so we can safely remove the real /app/app dir.
    # Clear pycache first (it's baked-path-addressed, would confuse Python
    # if somehow loaded through the new symlink path).
    find /app/app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    rm -rf /app/app
    echo "Platform layer: replaced /app/app directory with symlink."
  fi
  if [ -d /app/scripts ] && [ ! -L /app/scripts ]; then
    find /app/scripts -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    rm -rf /app/scripts
    echo "Platform layer: replaced /app/scripts directory with symlink."
  fi
  # Create (or re-point) the symlinks.
  ln -sfn "$_platform_app" /app/app
  ln -sfn "$_platform_scripts" /app/scripts
  echo "Platform layer: /app/app -> $_platform_app, /app/scripts -> $_platform_scripts"
  # Ensure platform tree is mobius-owned and world-readable (same as the
  # old /app/app /app/scripts permissions above).
  chmod -R a+rX "$_platform_app" "$_platform_scripts" 2>/dev/null || true
  chown -R mobius:mobius "$_platform_app" "$_platform_scripts" 2>/dev/null || true
else
  # Fallback: boot from baked directly. Ensure baked dirs are accessible.
  # /app/app and /app/scripts remain as real directories (the baked originals).
  chmod -R a+rX /app/app /app/scripts 2>/dev/null || true
  chown -R mobius:mobius /app/app /app/scripts 2>/dev/null || true
fi

# -----------------------------------------------------------------------
# PHASE 2: Git tracking for /data/platform
#
# /data/platform is its own git repo (distinct from /data's repo) so
# the agent's platform edits are versioned and reversible. First boot
# initialises the repo and records the baked image's SHA as a tag so
# future diffs can compare against the shipped baseline.
#
# We initialise here, after the platform tree is populated and
# mobius-owned but before uvicorn starts, so git commands run
# synchronously and the repo is ready before the agent can edit files.
# -----------------------------------------------------------------------

# Ensure /data/platform is mobius-owned before any git operations.
# (The chown may have been done above but let's be explicit.)
chown -R mobius:mobius /data/platform 2>/dev/null || true

if [ -d /data/platform ] && [ ! -d /data/platform/.git ]; then
  # First-time init: create the repo, write a sensible .gitignore,
  # and make an initial commit so `git log` always has at least one
  # entry to diff against.
  echo "Platform git: initialising /data/platform..."
  su -s /bin/sh mobius -c '
    git init /data/platform
    git -C /data/platform config user.name "Mobius Agent"
    git -C /data/platform config user.email "agent@mobius"
  '
  # .gitignore: exclude pycache and pyc files. The platform tree is
  # source-only; compiled bytecache does not belong in git.
  cat > /data/platform/.gitignore <<'PGITIGNORE'
__pycache__/
*.pyc
*.pyo
*.pyd
*.so
*.egg-info/
.eggs/
dist/
build/
PGITIGNORE
  chown mobius:mobius /data/platform/.gitignore 2>/dev/null || true
  su -s /bin/sh mobius -c '
    git -C /data/platform add -A
    git -C /data/platform commit -m "init: platform layer from baked image floor"
  '
  # Tag the initial commit with the baked image SHA so the agent (or an
  # operator) can diff against it later: `git -C /data/platform diff
  # baked-<sha>..HEAD`. BUILD_SHA is baked at docker-build time via the
  # BUILD_SHA ARG (Dockerfile line "ENV BUILD_SHA=${BUILD_SHA}").
  _build_sha=${BUILD_SHA:-unknown}
  if [ "$_build_sha" != "unknown" ]; then
    su -s /bin/sh mobius -c "git -C /data/platform tag baked-${_build_sha} HEAD 2>/dev/null || true"
  fi
  # Record the baked SHA in a plain ref file as a belt-and-suspenders
  # fallback (the tag above can be deleted by the agent; this file is
  # just informational and lives outside git).
  echo "$_build_sha" > /data/platform/.baked-sha
  chown mobius:mobius /data/platform/.baked-sha 2>/dev/null || true
  echo "Platform git: initialised with commit and baked-sha tag."
elif [ -d /data/platform/.git ]; then
  # Subsequent boot: ensure the repo is mobius-owned (docker pull + volume
  # recreate can leave root-owned .git).
  chown -R mobius:mobius /data/platform/.git 2>/dev/null || true
  # Check if the baked SHA changed since the recorded one — this means
  # an image upgrade happened. Do NOT auto-merge (Phase 4, deferred).
  # Log a prominent warning and set a debug flag so the operator knows.
  _build_sha=${BUILD_SHA:-unknown}
  _recorded_sha=""
  if [ -f /data/platform/.baked-sha ]; then
    _recorded_sha=$(cat /data/platform/.baked-sha 2>/dev/null | tr -d '[:space:]')
  fi
  if [ "$_build_sha" != "unknown" ] && [ -n "$_recorded_sha" ] && \
     [ "$_build_sha" != "$_recorded_sha" ]; then
    echo "PLATFORM UPGRADE NOTICE: image SHA changed from $_recorded_sha to $_build_sha." >&2
    echo "  /data/platform is unchanged — your agent's edits are intact." >&2
    echo "  To see what's new: git -C /data/platform diff baked-${_recorded_sha}..HEAD (if that tag exists)" >&2
    echo "  To merge upstream changes: ask the agent, or run recovery_restore.sh platform-baked" >&2
    # Write a flag file that /api/debug/status surfaces so the UI can
    # surface the notice (Phase 4 UX, but we surface the flag now).
    echo "upgrade-available ${_build_sha} (was ${_recorded_sha}) $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      > /data/.platform-upgrade-available
    chown mobius:mobius /data/.platform-upgrade-available 2>/dev/null || true
    # Update the recorded SHA so we don't repeat this on the next boot
    # unless ANOTHER upgrade happens.
    echo "$_build_sha" > /data/platform/.baked-sha
    chown mobius:mobius /data/platform/.baked-sha 2>/dev/null || true
  else
    # No upgrade: remove stale flag if present.
    rm -f /data/.platform-upgrade-available 2>/dev/null || true
  fi
fi

# Auto-generate SECRET_KEY if not set (one-click deploy support).
# Persisted to /data so it survives container restarts.
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

# Start cron daemon (runs as root, jobs execute as mobius).
cron

# Verify cron started (pgrep may not exist in slim images).
if command -v pgrep > /dev/null 2>&1; then
  pgrep -x cron > /dev/null || echo "WARNING: cron daemon failed to start" >&2
fi

# Create cron log directory.
mkdir -p /data/cron-logs
chown mobius:mobius /data/cron-logs

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

# Copy frontend source to /data/shell/ on first boot (or recovery).
if [ ! -d /data/shell/src ]; then
  echo "Initializing editable shell source in /data/shell/..."
  cp -r /app/shell-src/. /data/shell/
  chown -R mobius:mobius /data/shell
  # record a hash of the origin so we can detect upstream updates
  find /app/shell-src/src -type f | sort | xargs md5sum | md5sum | cut -d' ' -f1 > /data/shell/.origin-hash
  # Stamp the image's build identity so a later image pull can tell the
  # served /data/shell/dist is stale (see the image-update refresh below).
  echo "${BUILD_SHA:-unknown}" > /data/shell/.image-build-sha
  chown mobius:mobius /data/shell/.image-build-sha
fi

# --- deliver a pulled image's new shell bundle (self-host update path) ---
# A self-hoster updates by `docker compose pull && up -d`. The new image
# carries a fresh baked Vite build at /app/static, but the persisted
# /data/shell/dist (whatever was last built) MASKS it — main.py picks the
# live /data/shell/dist over the baked /app/static at startup. The
# first-boot seed above runs only once, and the upstream-change detector
# below only writes a diff; neither re-serves the new bundle. So without
# this step a pulled image silently keeps serving the OLD UI/CLI shell.
#
# deploy-prod.sh already refreshes dist on the owner's prod, so this stays
# a no-op there: it stamps .image-build-sha on every refresh, and a deploy
# that rebuilt dist from this image leaves the marker matching BUILD_SHA.
# We only act when the image is NEWER than what we last served.
#
# Detection: two signals, OR'd, so the refresh fires on every real update
# path — not just the one that happens to carry a build-arg.
#   1. BUILD_SHA marker (fast path): the baked BUILD_SHA differs from the
#      stamped .image-build-sha. This is what deploy-prod.sh leverages to
#      stay a no-op (it stamps the marker to match BUILD_SHA after it
#      rebuilds dist itself), and it short-circuits the content compare
#      when present.
#   2. Bundle-content compare (fallback, the self-host path): the baked
#      image's served entry bundle differs from the one in /data/shell/dist.
#      Vite content-hashes the entry as `assets/index-<hash>.js` and names
#      it in index.html, so a changed frontend ⇒ a changed filename and an
#      unchanged image ⇒ the identical filename. This is the signal that
#      makes the DOCUMENTED self-host update (`git pull && docker compose up
#      -d --build`, which does NOT set BUILD_SHA → baked sha is "unknown")
#      actually deliver the new bundle. docker-compose.yml defaults
#      BUILD_SHA to "unknown", so gating on the marker alone left this path
#      silently stale — the exact bug this block exists to fix.
# Either signal is sufficient; the content compare is authoritative for
# "is the served bundle actually the baked one", independent of BUILD_SHA.
#
# A plain re-`up` of the SAME image is a no-op: same baked bundle filename
# ⇒ no content diff, and (when set) same BUILD_SHA ⇒ no marker diff. So
# local dev instances and ordinary restarts don't churn dist on every boot.
#
# Preserving user edits: we refresh ONLY /data/shell/dist (the served
# build), copied from the new image's baked /app/static (a complete build
# incl. /vendor). We do NOT touch /data/shell/src — a user who customized
# the shell source keeps it; their edits just are not reflected in the
# served bundle until they rebuild (npx vite build / rebuild_shell.sh).
# This is the minimum that makes a pull deliver the new baked UI while
# leaving customization reversible: a user-built dist is regenerable from
# their src, so refreshing it is safe.

# Extract the content-hashed Vite entry bundle name (assets/index-<hash>.js)
# from an index.html. Empty if the file is missing or has no such tag — the
# same `index-[A-Za-z0-9_-]+\.js` shape scripts/bundle-info.sh keys on.
_shell_bundle_id() {
  [ -f "$1" ] || return 0
  grep -oE 'index-[A-Za-z0-9_-]+\.js' "$1" 2>/dev/null | head -n1
}

_baked_sha="${BUILD_SHA:-unknown}"
if [ -d /data/shell/src ] && [ -f /app/static/index.html ]; then
  _stamped_sha=""
  [ -f /data/shell/.image-build-sha ] && _stamped_sha=$(cat /data/shell/.image-build-sha)

  # Signal 1: a known BUILD_SHA that differs from what we last stamped.
  _sha_newer=""
  if [ "$_baked_sha" != "unknown" ] && [ "$_baked_sha" != "$_stamped_sha" ]; then
    _sha_newer="yes"
  fi

  # Signal 2: the baked entry bundle differs from the one served from dist.
  # When dist is absent/incomplete (no index.html), the served id is empty
  # and any non-empty baked id counts as "newer" — first real seed of dist.
  _baked_bundle=$(_shell_bundle_id /app/static/index.html)
  _served_bundle=$(_shell_bundle_id /data/shell/dist/index.html)
  _bundle_newer=""
  if [ -n "$_baked_bundle" ] && [ "$_baked_bundle" != "$_served_bundle" ]; then
    _bundle_newer="yes"
  fi

  if [ -n "$_sha_newer" ] || [ -n "$_bundle_newer" ]; then
    echo "New shell bundle detected (build ${_baked_sha}, baked=${_baked_bundle:-<none>} served=${_served_bundle:-<none>}); refreshing /data/shell/dist from the baked image."
    # Lock + atomic swap. Two containers sharing /data (or a restart
    # mid-copy) must never see a half-copied or nested dist: we copy into a
    # sibling temp dir then rename() it over the old one (atomic on the same
    # filesystem). The flock serializes concurrent refreshers so only one
    # builds the temp dir + renames at a time. flock is guarded by
    # command -v so a stripped image without util-linux still refreshes
    # (unlocked) rather than failing to boot.
    _refresh_dist() {
      _new=/data/shell/.dist.new
      _old=/data/shell/.dist.old
      rm -rf "$_new" "$_old"
      # cp the directory CONTENTS into a fresh dir, so the result is
      # /data/shell/.dist.new/{index.html,assets,...} — never a nested
      # .dist.new/static. -a preserves the complete baked build incl /vendor.
      mkdir -p "$_new"
      cp -a /app/static/. "$_new/"
      chown -R mobius:mobius "$_new"
      # Atomic-ish swap: move the live dist aside, rename the new one in,
      # then drop the old. If dist is absent the first mv is a harmless
      # no-op (guarded). A reader between the two renames sees either the
      # complete old dist or the complete new one — never a partial tree.
      [ -e /data/shell/dist ] && mv -f /data/shell/dist "$_old"
      mv -f "$_new" /data/shell/dist
      rm -rf "$_old"
      echo "$_baked_sha" > /data/shell/.image-build-sha
      chown mobius:mobius /data/shell/.image-build-sha
      echo "Served shell bundle refreshed to ${_baked_bundle:-<none>} (build ${_baked_sha})."
    }
    if command -v flock >/dev/null 2>&1; then
      _lock=/data/shell/.dist-refresh.lock
      ( flock 9; _refresh_dist ) 9>"$_lock"
      chown mobius:mobius "$_lock" 2>/dev/null || true
    else
      _refresh_dist
    fi
  fi
fi

# --- enforce protected file permissions ---
# Two categories of protected files (see protected-files.txt header):
#   1. Credential surfaces — chmod 444 root prevents agent tampering.
#   2. Frozen recovery island — chmod 444 root keeps the recovery
#      chat reachable + working when the rest of the platform is
#      broken.
# Entries are absolute paths so both /data/shell/ and /app/app/
# targets fit the same enforcement loop. Runs on every boot, not
# just first boot, to re-enforce after the chown sweep above.
if [ -f /app/protected-files.txt ]; then
  while IFS= read -r line; do
    # skip comments and empty lines
    case "$line" in \#*|"") continue ;; esac
    # Absolute paths only (new format). Legacy relative paths are
    # treated as /data/shell/-relative for backward compat with any
    # external protected-files.txt overrides.
    case "$line" in
      /*) target="$line" ;;
      *)  target="/data/shell/$line" ;;
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

# --- detect upstream shell changes ---
if [ -f /data/shell/.origin-hash ]; then
  current_hash=$(find /app/shell-src/src -type f | sort | xargs md5sum | md5sum | cut -d' ' -f1)
  stored_hash=$(cat /data/shell/.origin-hash)
  if [ "$current_hash" != "$stored_hash" ]; then
    echo "Upstream shell source has changed — writing diff file."
    diff_output=$(diff -rq /data/shell/src /app/shell-src/src 2>/dev/null || true)
    export UPSTREAM_DIFF="$diff_output"
    export UPSTREAM_CHANGED="true"
    # update stored hash for next boot
    echo "$current_hash" > /data/shell/.origin-hash
    chown mobius:mobius /data/shell/.origin-hash
  fi
fi

# Install the agent self-reminders cron dispatcher (feature 088). This
# is platform-level, not a mini-app, so it lives under a reserved
# _self-reminders/ slug and runs a tiny job.sh that execs the baked
# /app/scripts/self-reminders-dispatch.sh. The scaffold writes job.sh +
# init-cron.sh and installs the entry; the replay loop below re-adds it
# on every boot like any other app cron. Create-if-absent so we never
# clobber an operator edit to the schedule. DEFAULT OFF: the dispatcher
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
  su -s /bin/sh mobius -c \
    "bash /app/scripts/init-cron-scaffold.sh _self-reminders '*/5 * * * *'" \
    2>/dev/null || true
fi

# Run per-app init scripts to restore cron entries lost on container
# restart. Don't pre-clear the crontab — agents (and the operator) may
# have installed cron entries directly via `crontab -u`, and a blanket
# `crontab -r` on every boot would silently wipe them. Init scripts
# that use idempotent patterns (e.g. write a full crontab, or check
# for existing entries before appending) survive replay. The cost of
# the previous policing was real: agent-installed crons disappeared
# on the next deploy with no signal. Per Möbius's design philosophy
# (CLAUDE.md), "code empowers the agent; it does not police it."
for init_script in /data/apps/*/init-cron.sh; do
  [ -f "$init_script" ] && su -s /bin/sh mobius -c "bash $init_script" 2>/dev/null || true
done

# Ensure mobius's crontab has the full PATH at the top. Must run AFTER
# init-cron.sh — those scripts call `crontab -u mobius` which overwrites
# the file. Without PATH, cron's minimal /usr/bin:/bin can't resolve
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

# Bootstrap the knowledge graph (/data/shared/memory/). CREATE-IF-ABSENT:
# unlike the flat experience file above, the graph is the agent's persistent
# memory and must never be reseeded over learned notes. Writes the `.ready`
# sentinel LAST; until then memory injection uses the legacy flat-file path.
python3 /app/scripts/init_memory_graph.py

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
push/*.pem
push/*.json
push/*.txt
service-token.txt
.secret-key
.recovery-secret
.pm-commit
compiled/
shell/dist/
shell/node_modules/
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
# platform/ has its own git repo; exclude it from the outer /data repo so
# `git add -A` from /data doesn't treat platform as an untracked submodule.
# The .baked-sha and .gitignore inside platform/ are tracked by platform's own
# git, not /data's.
platform/
# Phase 3 boot-state files — runtime counters, not content the agent manages.
.boot-attempt
.last-successful-boot
.platform-restore-active
.platform-upgrade-available
EOF
chown mobius:mobius /data/.gitignore 2>/dev/null || true

# Drop accidental nested git repos under /data, but preserve the intentional
# per-app repos at /data/apps/<slug>/.git AND the platform git at
# /data/platform/.git. The outer /data repo ignores those repos so `git add -A`
# does not try to treat them as submodules, while the installer/update path can
# still keep each manifest-installed app's upstream/main history across
# container restarts, and the platform git is its own repo.
find /data -regextype posix-extended -mindepth 2 -maxdepth 4 \
  -type d -name '.git' \
  ! -regex '/data/apps/[^/]+/\.git' \
  ! -regex '/data/platform/\.git' \
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

# Ensure the mobius user has a GLOBAL git identity available. The
# local-repo config above only covers /data/.git; when the agent later
# runs `git commit` inside /data/apps/<slug>/ (which may have its own
# .git with no config, or no .git and no fallback because it's not
# nested under /data/.git's worktree), commits fail with "Please tell
# me who you are." Set the global config as mobius so any future
# repository — per-app, shell, or scratch — picks up an identity.
su -s /bin/sh mobius -c "
  git config --global user.name 'Mobius Agent'
  git config --global user.email 'agent@mobius'
" 2>/dev/null || true

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


# Deferred restore: a previous boot's recovery chat may have written
# /data/.recover-pending=<mode>. Process it AS ROOT (we're still root
# at this point) so recovery_restore.sh can chown protected files
# back to root:root. Then clear the flag and continue boot.
#
# Running the restore here (not from the route handler that wrote
# the flag) is load-bearing: cp -a from /app/<X>-baked/ over the
# live /app/<X>/ must preserve root ownership on protected files
# for the frozen-island invariant to hold. The route handler runs
# as mobius (uvicorn drops privilege) and cannot `chown root:root`.
# Root can. The flag file is the handoff between the two contexts.
if [ -f /data/.recover-pending ]; then
  mode=$(cat /data/.recover-pending 2>/dev/null | tr -d '[:space:]')
  rm -f /data/.recover-pending
  restore_status=""
  case "$mode" in
    backend|scripts|shell-dist|shell-src|platform|platform-baked)
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
  # Tell the user what happened by appending to the recovery chat
  # log. They'll see this when they reload /recover/chat after the
  # container restart. Without this signal a silent failure leaves
  # them refreshing nervously with no feedback.
  if [ -n "$restore_status" ]; then
    # Build the JSON via python's json.dumps so any future addition
    # of free-form strings (error output, file paths, etc.) gets
    # correctly escaped. Shell-interpolated JSON via echo was the
    # original pattern but a single unescaped " or backslash in
    # restore_status would corrupt the JSONL line and the runner
    # would silently skip it (json.JSONDecodeError catch). The
    # extra subprocess is cheap and runs at most once per boot.
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
  # Re-enforce protected files now that the restore may have
  # touched perms.
  if [ -f /app/protected-files.txt ]; then
    while IFS= read -r line; do
      case "$line" in \#*|"") continue ;; esac
      case "$line" in
        /*) target="$line" ;;
        *)  target="/data/shell/$line" ;;
      esac
      if [ -f "$target" ]; then
        chown root:root "$target" 2>/dev/null || true
        case "$target" in
          *.sh) chmod 555 "$target" 2>/dev/null || true ;;
          *)    chmod 444 "$target" 2>/dev/null || true ;;
        esac
      fi
    done < /app/protected-files.txt
  fi
fi

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

# Install the core apps (memory-graph + reflection) + the nightly reflection
# cron once the server is healthy. Backgrounded so it doesn't block boot;
# polls /api/health itself; idempotent; non-fatal. Runs as mobius so the
# registered app files are mobius-owned.
su -s /bin/sh mobius -c "umask 022 && CLAUDE_CONFIG_DIR=/data/cli-auth/claude bash /app/scripts/install-core-apps.sh" &

# PHASE 3: Background health probe — writes /data/.last-successful-boot
# and resets the boot-attempt counter once the server is confirmed
# healthy. This is the "success" signal that prevents false-positive
# crash-loop detection.
#
# The probe polls /api/health (127.0.0.1, never routed outside the
# container) with a 60-second timeout (generous for slow first-boots
# with DB migrations). On success it writes the sentinel and zeroes the
# counter. It does NOT restart uvicorn or take any other action — it is
# purely the signal that "this boot succeeded."
#
# pgrep self-match trap: we do NOT use `until ! pgrep -f uvicorn` or
# similar — the probe waits on the outcome (/api/health 200), not on a
# process name. See feedback_pgrep_self_match_in_monitor_loops.md.
_port=${PORT:-8000}
(
  # Wait up to 90 seconds for /api/health to return 200.
  for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:${_port}/api/health" > /dev/null 2>&1; then
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

exec su -s /bin/sh mobius -c "umask 022 && cd /app && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"
