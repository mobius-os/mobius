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
#   init-cron-scaffold.sh <slug> "<cron-schedule>" [job-filename]
#
# Example:
#   init-cron-scaffold.sh news "*/10 * * * *"            # runs job.sh
#   init-cron-scaffold.sh dreaming "0 6 * * *" fetch.sh  # runs fetch.sh
#
# The optional job filename (default job.sh) is the script the crontab
# entry actually runs. The installer passes the manifest's
# `schedule.job` so the cron points at the bundled job, not the stub.
# After running, edit /data/apps/<slug>/<job-filename> to do the work.
# init-cron.sh is checked-in scaffolding — re-running this scaffold for
# the same slug + schedule is a no-op (idempotent on every layer).

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <slug> \"<cron-schedule>\" [job-filename]" >&2
  echo "Example: $0 news \"*/10 * * * *\" fetch.sh" >&2
  exit 2
fi

SLUG="$1"
SCHEDULE="$2"
# Optional 3rd arg: the script the crontab entry runs, relative to the
# app dir. Defaults to job.sh (the agent's scaffold convention). The
# installer passes the manifest's `schedule.job` (e.g. fetch.sh) so the
# crontab points at the real bundled job rather than the empty stub.
JOB_NAME="${3:-job.sh}"

# Slug guard: anything outside `[A-Za-z0-9_-]+` silently breaks the
# init-cron.sh heredoc + the matching grep in the replay path. Hard
# fail early instead of writing a non-functional script.
if ! printf '%s' "$SLUG" | grep -qE '^[A-Za-z0-9_-]+$'; then
  echo "ERROR: slug must match [A-Za-z0-9_-]+, got: $SLUG" >&2
  exit 2
fi

# Job-name guard: it becomes a path component and lands in the
# init-cron.sh heredoc, so keep it a clean basename — no slashes, no
# traversal.
if ! printf '%s' "$JOB_NAME" | grep -qE '^[A-Za-z0-9_.-]+$'; then
  echo "ERROR: job filename must match [A-Za-z0-9_.-]+, got: $JOB_NAME" >&2
  exit 2
fi

# APP_BASE is overridable only so the scaffold is testable without a
# container; production never sets it, so the base stays /data/apps.
APP_BASE="${MOBIUS_APP_BASE:-/data/apps}"
APP_DIR="${APP_BASE}/${SLUG}"
JOB_PATH="${APP_DIR}/${JOB_NAME}"
INIT_PATH="${APP_DIR}/init-cron.sh"

if [ ! -d "$APP_DIR" ]; then
  echo "ERROR: $APP_DIR does not exist. Create the mini-app first." >&2
  exit 1
fi

# 1. Write a job stub (only if absent — never clobber agent or bundled
#    work; a manifest-bundled job script is written by the installer
#    before this runs, so this branch is skipped for it).
if [ ! -f "$JOB_PATH" ]; then
  cat > "$JOB_PATH" <<JOB
#!/bin/bash
# ${APP_DIR}/${JOB_NAME} — scheduled work for the "$SLUG" mini-app.
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
