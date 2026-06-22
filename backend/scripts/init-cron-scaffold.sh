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
#   init-cron-scaffold.sh <slug> "<cron-schedule>" [job-filename] [app-id]
#
# Example:
#   init-cron-scaffold.sh news "*/10 * * * *"             # runs job.sh
#   init-cron-scaffold.sh reflection "0 6 * * *" fetch.sh   # runs fetch.sh
#   init-cron-scaffold.sh news "0 9 * * *" fetch.sh 12    # runs: fetch.sh 12
#
# The optional job filename (default job.sh) is the script the crontab
# entry actually runs. The installer passes the manifest's
# `schedule.job` so the cron points at the bundled job, not the stub.
#
# The optional 4th arg (app-id) is appended to the crontab command, so a
# reusable job that reads its target app from "$1" — the same contract
# the "Generate now" run-job endpoint already uses — fires correctly from
# cron too. Omit it for self-contained jobs that hardcode their own id.
# Without it, an arg-taking fetch.sh runs with no id and exits early: the
# exact bug that left a freshly-installed news app's cron silently dead.
#
# After running, edit /data/apps/<slug>/<job-filename> to do the work.
# init-cron.sh is checked-in scaffolding — re-running this scaffold for
# the same slug + schedule is a no-op (idempotent on every layer).

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <slug> \"<cron-schedule>\" [job-filename] [app-id]" >&2
  echo "Example: $0 news \"0 9 * * *\" fetch.sh 12" >&2
  exit 2
fi

SLUG="$1"
SCHEDULE="$2"
# Optional 3rd arg: the script the crontab entry runs, relative to the
# app dir. Defaults to job.sh (the agent's scaffold convention). The
# installer passes the manifest's `schedule.job` (e.g. fetch.sh) so the
# crontab points at the real bundled job rather than the empty stub.
JOB_NAME="${3:-job.sh}"
# Optional 4th arg: the mini-app's numeric id. When set, it's appended to
# the crontab command (`<job-path> <app-id>`) so a reusable job can read
# its target app from "$1" — the same contract the run-job endpoint uses
# for "Generate now". Empty (default) leaves the command bare, for
# self-contained jobs that hardcode their own id.
APP_ID="${4:-}"

# Guards use `case`, not `grep -qE`. A per-line regex (`grep`) passes a
# multiline value as long as ONE line matches — so a newline could sneak
# a second crontab/heredoc line past the check. `case` matches the WHOLE
# string, newline included, closing that injection footgun.

# Slug guard: anything outside `[A-Za-z0-9_-]+` silently breaks the
# init-cron.sh heredoc + the matching grep in the replay path. Hard
# fail early instead of writing a non-functional script.
case "$SLUG" in
  ""|*[!A-Za-z0-9_-]*)
    echo "ERROR: slug must match [A-Za-z0-9_-]+, got: $SLUG" >&2
    exit 2 ;;
esac

# Job-name guard: it becomes a path component and lands in the
# init-cron.sh heredoc, so keep it a clean basename — no slashes, no
# traversal, no newlines.
case "$JOB_NAME" in
  ""|*[!A-Za-z0-9_.-]*)
    echo "ERROR: job filename must match [A-Za-z0-9_.-]+, got: $JOB_NAME" >&2
    exit 2 ;;
esac

# App-id guard: it lands verbatim in the crontab command, so allow only
# digits — or empty (omitted, leaving a bare command).
case "$APP_ID" in
  "") : ;;
  *[!0-9]*)
    echo "ERROR: app-id must be numeric, got: $APP_ID" >&2
    exit 2 ;;
esac

# APP_BASE is overridable only so the scaffold is testable without a
# container; production never sets it, so the base stays /data/apps.
APP_BASE="${MOBIUS_APP_BASE:-/data/apps}"
APP_DIR="${APP_BASE}/${SLUG}"
JOB_PATH="${APP_DIR}/${JOB_NAME}"
INIT_PATH="${APP_DIR}/init-cron.sh"
# The command cron runs: the job path, plus the app id as $1 when known.
if [ -n "$APP_ID" ]; then
  CRON_CMD="${JOB_PATH} ${APP_ID}"
else
  CRON_CMD="${JOB_PATH}"
fi

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
# user to put entries back.
#
# Drop any prior line for this job path, then re-add the canonical
# entry. Idempotent (no duplicates) AND self-healing: a stale entry
# written before the app id was appended is replaced, not skipped.
# grep -vF on the full job path is prefix-safe (news vs news-2). A
# contrived line that puts this exact path in ITS OWN args would be an
# over-match, but it self-heals on the next boot replay; the install-side
# delete path (_crontab_without_app) anchors on the command precisely.
#
# Capture the existing crontab ONCE and check rc. Piping a second live
# crontab listing into the rewrite risks a transient empty read collapsing
# the whole crontab to just this one line. On rc 0 the listing is
# authoritative — keep every other line. On rc != 0 (no crontab yet, or
# unreadable) install only this entry; the entrypoint replays every
# app's init-cron.sh in turn, so each re-adds its own line.
ENTRY="$SCHEDULE $CRON_CMD"
EXISTING=\$(crontab -u mobius -l 2>/dev/null); RC=\$?
if [ "\$RC" -eq 0 ]; then
  (printf '%s\\n' "\$EXISTING" | grep -vF "$JOB_PATH"; echo "\$ENTRY") \\
    | crontab -u mobius -
else
  echo "\$ENTRY" | crontab -u mobius -
fi
INIT
chmod +x "$INIT_PATH"
echo "wrote $INIT_PATH"

# 3. Install the entry NOW so the agent doesn't need to wait for a
#    restart. Reuses the same script so install + replay logic match.
bash "$INIT_PATH"

mkdir -p "${DATA_DIR:-/data}/cron-logs"

echo
echo "Done. Verify with: crontab -u mobius -l"
echo "Edit the job: $JOB_PATH"
