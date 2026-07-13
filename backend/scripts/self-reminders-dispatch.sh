#!/bin/bash
# self-reminders-dispatch.sh — fires due agent self-reminders.
#
# The agent schedules relational check-ins ("check in on the user in
# three days") via POST /api/self-reminders; this script is the cron
# half that wakes them. It runs every few minutes from a single OS cron
# entry. Boot invokes the trusted platform scaffold for this reserved job;
# unlike app-owned declarations, its init file is not executed. The job asks
# the backend to fire
# every reminder whose due time has passed.
#
# The heavy lifting lives in POST /api/self-reminders/dispatch (server-
# side: scan due, resume each chat with a hidden message, mark done) so
# this script stays a thin, testable trigger — no JSONL parsing or
# per-reminder POST loop in shell. The service token at
# /data/service-token.txt is the same long-lived owner JWT the other
# cron scripts already read.
#
# DEFAULT OFF: the dispatch endpoint fires nothing until the owner opts
# in by creating /data/shared/self-reminders.enabled. We short-circuit
# on the sentinel here too, so a disabled instance doesn't even make the
# API call — the endpoint re-checks regardless, so the gate holds even
# if this check is bypassed.

set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"
API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
SERVICE_TOKEN_FILE="${SERVICE_TOKEN_FILE:-${DATA_DIR}/service-token.txt}"
SENTINEL="${DATA_DIR}/shared/self-reminders.enabled"
LOG_DIR="${DATA_DIR}/cron-logs"

# Owner opt-in gate. Absent sentinel = do nothing. This is the cheap
# local mirror of the endpoint's own is_dispatcher_enabled check.
if [ ! -e "$SENTINEL" ]; then
  exit 0
fi

if [ ! -r "$SERVICE_TOKEN_FILE" ]; then
  mkdir -p "$LOG_DIR" 2>/dev/null
  echo "[$(date -u +%FT%TZ)] no readable service token at $SERVICE_TOKEN_FILE" \
    >> "$LOG_DIR/self-reminders.log" 2>/dev/null
  exit 1
fi

TOKEN=$(cat "$SERVICE_TOKEN_FILE")

# Best-effort fire. A non-2xx or a network error is logged but never
# fatal — cron retries on the next tick, and a still-pending reminder
# (the endpoint only marks done after a successful resume) is picked up
# again then.
mkdir -p "$LOG_DIR" 2>/dev/null
if ! curl -fsS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "$API_BASE_URL/api/self-reminders/dispatch" \
  >> "$LOG_DIR/self-reminders.log" 2>&1; then
  echo "[$(date -u +%FT%TZ)] dispatch POST failed" \
    >> "$LOG_DIR/self-reminders.log" 2>/dev/null
  exit 1
fi
