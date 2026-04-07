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
mkdir -p /data/db /data/apps /data/compiled /data/shared /data/shell /data/logs /data/cron-logs /data/cli-auth
chown -R mobius:mobius /data 2>/dev/null || chmod -R 777 /data 2>/dev/null || true

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
  echo "$_token" > /data/service-token.txt
  chown mobius:mobius /data/service-token.txt
  chmod 600 /data/service-token.txt
  echo "Service token written/refreshed at /data/service-token.txt"
fi

# Copy frontend source to /data/shell/ on first boot (or recovery).
if [ ! -d /data/shell/src ]; then
  echo "Initializing editable shell source in /data/shell/..."
  cp -r /app/shell-src/. /data/shell/
  chown -R mobius:mobius /data/shell
  # record a hash of the origin so we can detect upstream updates
  find /app/shell-src/src -type f | sort | xargs md5sum | md5sum | cut -d' ' -f1 > /data/shell/.origin-hash
fi

# Initialize local git in /data/shell/ so the agent can track changes.
# This is purely local — no remote, no pushing.
if [ ! -d /data/shell/.git ]; then
  cd /data/shell
  su -s /bin/sh mobius -c "
    git init
    git config user.name 'Möbius Agent'
    git config user.email 'agent@mobius.local'
    git add -A
    git commit -m 'initial: shell source from build'
    git checkout -b agent
  " 2>/dev/null
  cd /app
fi

# --- enforce protected file permissions ---
# These files handle credential input and must not be agent-writable.
# Runs on every boot, not just first boot, to re-enforce if needed.
if [ -f /app/protected-files.txt ]; then
  while IFS= read -r line; do
    # skip comments and empty lines
    case "$line" in \#*|"") continue ;; esac
    target="/data/shell/$line"
    if [ -f "$target" ]; then
      chown root:root "$target"
      chmod 444 "$target"
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

# Run per-app init scripts to restore cron entries lost on container restart.
for init_script in /data/apps/*/init-cron.sh; do
  [ -f "$init_script" ] && su -s /bin/sh mobius -c "bash $init_script" 2>/dev/null || true
done

# Initialize agent experience file (seeds from template on first boot).
python3 /app/scripts/init_agent_context.py

# Create default theme.css if it doesn't exist.
if [ ! -f /data/shared/theme.css ]; then
  cat > /data/shared/theme.css << 'EOF'
:root {
  /* Colors */
  --bg: #0c0f14;
  --surface: #14181f;
  --surface2: #1a1f28;
  --border: #252b36;
  --border-light: #1c2029;
  --text: #d4d4d8;
  --muted: #52525b;
  --accent: #a78bfa;
  --accent-hover: #c4b5fd;
  --accent-dim: rgba(167, 139, 250, 0.1);
  --danger: #f87171;
  --green: #6ee7b7;

  /* Typography */
  --font: 'Inter', system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 15px;
}
EOF
  chown mobius:mobius /data/shared/theme.css
fi


# Drop to non-root user and start the server.
exec su -s /bin/sh mobius -c "cd /app && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"
