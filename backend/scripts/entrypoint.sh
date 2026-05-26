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
mkdir -p /data/db /data/apps /data/compiled /data/shared /data/shell /data/logs /data/cron-logs /data/cli-auth /data/agent-browser-profiles

# /data/agent-browser-profiles holds PER-CHAT Chrome user-data dirs
# (chat-<chat_id>/...) for agent-browser. The path is set per-chat by
# `app.chat._build_subprocess_env` so the agent's repeated screenshots
# within one chat reuse cached SW + assets + warm bundle (faster + a
# closer match to the partner's persistent PWA state). Per-chat
# isolation avoids the lock conflict that would happen if two parallel
# agent chats both tried to launch Chrome against a shared dir.
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
# that doesn't hold secrets.
chmod -R a+rX /app/app /app/scripts 2>/dev/null || true
chown -R mobius:mobius /app/app /app/scripts 2>/dev/null || true

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

# Check if existing token is still valid with more than 30 days remaining.
token_file = '/data/service-token.txt'
if os.path.exists(token_file):
    try:
        existing = open(token_file).read().strip()
        payload = decode_access_token(existing)
        exp = payload.get('exp', 0)
        remaining = exp - datetime.now(UTC).timestamp()
        if remaining > 30 * 86400:  # more than 30 days left — keep it
            exit(0)
    except Exception:
        pass  # expired or invalid — fall through to regenerate

token = create_access_token({'sub': owner.username}, expires_delta=timedelta(days=90))
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

# Initialize agent experience file (seeds from template on first boot).
python3 /app/scripts/init_agent_context.py

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
service-token.txt
.secret-key
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
EOF
chown mobius:mobius /data/.gitignore 2>/dev/null || true

# Drop nested git repos under /data so the outer /data/.git is the
# ONE repo that covers everything sensible. If we leave inner .git
# directories in place (the shell came in as a clone; the agent may
# have run `git init` in /data/apps/<slug>/...), `git add` from
# /data root treats them as submodules and warns with "adding
# embedded git repository". An agent in chat 380581a8 surfaced
# this gotcha. Removing the inner .git makes shell + apps just
# tracked files in the outer repo — agent has one git history that
# captures every edit it makes.
find /data -mindepth 2 -maxdepth 4 -type d -name '.git' -prune \
  -exec rm -rf {} + 2>/dev/null || true

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
  git init /data
  git -C /data config user.name 'Mobius Agent'
  git -C /data config user.email 'agent@mobius'
  git -C /data add -A
  git -C /data commit -m 'init' --allow-empty
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


# Drop to non-root user and start the server.
# umask 022: newly created files default to 644 (rw-r--r--) so the
# mobius server can read script/source files copied into the image at
# build time (whose default chmod inherits from the host umask of the
# user who created them — sometimes 027/077). Without this, runner
# scripts created via Write that ship with mode 640 are unreadable by
# the mobius user at runtime, causing subprocess "permission denied"
# failures that look like generic CLI crashes.
umask 022

exec su -s /bin/sh mobius -c "umask 022 && cd /app && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"
