#!/bin/bash
# init-cron-scaffold.sh — author a cron task for a mini-app that
# SURVIVES container restart on Railway / Fly / Render / Docker.
#
# Why this exists: Möbius's entrypoint replays /data/apps/*/init-cron.sh
# on every boot, because /var/spool/cron/crontabs/ is INSIDE the container
# (not on the persistent volume) and is wiped on every redeploy. An agent
# that calls `crontab -u mobius` directly without also writing init-cron.sh
# loses its cron entry on the next deploy with no warning. This scaffold
# closes that gap: one command writes job.sh + init-cron.sh AND installs
# the live entry, in one idempotent step.
#
# Usage:
#   init-cron-scaffold.sh <slug> "<cron-schedule>"
#
# Example:
#   init-cron-scaffold.sh news "*/10 * * * *"
#
# After running, edit /data/apps/<slug>/job.sh to do the actual work.
# init-cron.sh is checked-in scaffolding — re-running this scaffold for
# the same slug + schedule is a no-op (idempotent on every layer).

set -e

if [ $# -lt 2 ]; then
  echo "Usage: $0 <slug> \"<cron-schedule>\"" >&2
  echo "Example: $0 news \"*/10 * * * *\"" >&2
  exit 2
fi

SLUG="$1"
SCHEDULE="$2"
APP_DIR="/data/apps/$SLUG"
JOB_PATH="$APP_DIR/job.sh"
INIT_PATH="$APP_DIR/init-cron.sh"

if [ ! -d "$APP_DIR" ]; then
  echo "ERROR: $APP_DIR does not exist. Create the mini-app first." >&2
  exit 1
fi

# 1. Write a job.sh stub (only if absent — never clobber agent work).
if [ ! -f "$JOB_PATH" ]; then
  cat > "$JOB_PATH" <<JOB
#!/bin/bash
# /data/apps/$SLUG/job.sh — scheduled work for the "$SLUG" mini-app.
# Runs as the mobius user. Stderr captured to /data/cron-logs/$SLUG.log.
set -e

SERVICE_TOKEN=\$(cat /data/service-token.txt)
API_BASE_URL=http://localhost:8000
# APP_ID=<fill in numeric id from GET /api/apps/>

# Replace with the real work. Example: invoke claude as a sub-agent.
# claude -p "..." --system-prompt-file /data/apps/$SLUG/prompt.md \\
#   --allowedTools "Bash(command)" --max-turns 30
JOB
  chmod +x "$JOB_PATH"
  echo "wrote $JOB_PATH (stub)"
else
  echo "kept existing $JOB_PATH"
fi

# 2. Write init-cron.sh. Always rewrite — the schedule is the only
#    variable, and the script body is tiny + standardised.
cat > "$INIT_PATH" <<INIT
#!/bin/sh
# Restores the cron entry for "$SLUG" on container restart.
# /var/spool/cron/crontabs/ lives inside the container, not on the
# /data volume — so it is empty after every Railway redeploy. The
# entrypoint replays every /data/apps/*/init-cron.sh as the mobius
# user to put entries back. Idempotent via grep -qF.
ENTRY="$SCHEDULE $JOB_PATH"
if ! crontab -u mobius -l 2>/dev/null | grep -qF "$JOB_PATH"; then
  (crontab -u mobius -l 2>/dev/null; echo "\$ENTRY") | crontab -u mobius -
fi
INIT
chmod +x "$INIT_PATH"
echo "wrote $INIT_PATH"

# 3. Install the entry NOW so the agent doesn't need to wait for a
#    restart. Reuses the same script so install + replay logic match.
bash "$INIT_PATH"

mkdir -p /data/cron-logs

echo
echo "Done. Verify with: crontab -u mobius -l"
echo "Edit the job: $JOB_PATH"
