#!/bin/bash
# cron-emit.sh — runs a cron job and emits its outcome to the activity log.
#
# Usage:
#   cron-emit.sh <app_id> <job_path> [arg1 arg2 ...]
#
# Crontab entries authored by init-cron-scaffold.sh + the cron-sync
# path wrap their actual command in this script so every run produces
# one `cron_outcome` event in /data/logs/activity.jsonl. The dreaming
# agent reads from that log to see which scheduled jobs ran in the
# last 24h and how they finished.
#
# Failure modes:
#   - Wrapped job exits non-zero → we still emit the event with the
#     non-zero exit_code; the log shows the failure, the crontab keeps
#     firing. Best-effort: emit failure must not retry or block.
#   - Activity emit fails (no service token, API down) → we log to
#     /data/cron-logs/<app_id>-emit-fail.log and continue. The job's
#     own exit code is what propagates to cron.
#
# The emit happens via POST to the API rather than a direct file write
# so we route through one process owning the activity-log file handle
# (no cross-process flock dance). The service-token at
# /data/service-token.txt is the same long-lived owner JWT the cron
# scripts already read for /api/* calls.

set -uo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <app_id> <job_path> [args...]" >&2
  exit 2
fi

APP_ID="$1"
JOB_PATH="$2"
shift 2

JOB_NAME="$(basename "$JOB_PATH")"
API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
SERVICE_TOKEN_FILE="${SERVICE_TOKEN_FILE:-/data/service-token.txt}"

# Wall-clock duration in ms. `date +%s%3N` is GNU coreutils; container
# uses bash + coreutils so it's available.
START_MS=$(date +%s%3N)

bash "$JOB_PATH" "$@"
EXIT_CODE=$?

END_MS=$(date +%s%3N)
DURATION_MS=$((END_MS - START_MS))

# Best-effort emit. The job's exit code is what we care about for
# cron; the emit is a sidecar signal. Failure to emit shouldn't
# affect the job's outcome from cron's perspective.
if [ -r "$SERVICE_TOKEN_FILE" ]; then
  TOKEN=$(cat "$SERVICE_TOKEN_FILE")
  TS=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
  PAYLOAD=$(printf '{"ev":"cron_outcome","ts":"%s","app_id":%s,"job":"%s","exit_code":%s,"duration_ms":%s}' \
    "$TS" "$APP_ID" "$JOB_NAME" "$EXIT_CODE" "$DURATION_MS")
  # POST to the activity-log emit endpoint. The API owns the file
  # handle + rotation policy; we just hand it the event line.
  curl -fsS -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$API_BASE_URL/api/admin/activity/emit" >/dev/null 2>&1 || {
    mkdir -p /data/cron-logs 2>/dev/null
    echo "[$(date -u +%FT%TZ)] activity-emit failed for app=$APP_ID job=$JOB_NAME" \
      >> /data/cron-logs/cron-emit.log 2>/dev/null
  }
fi

exit $EXIT_CODE
