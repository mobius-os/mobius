#!/bin/bash
# fetch.sh — the nightly "dreaming" wrapper. Thin by design: it owns
# only the OPERATIONAL concerns of an unattended cron run — no overlap,
# a wall-clock timeout, liveness heartbeats, an outcome event — and then
# hands the night to the agent.
#
# Unlike v1, this wrapper is NOT a security boundary. The Dreaming agent
# runs with FULL tools and a REAL token (no staging tree, no
# Bash-less/token-less envelope, no graph validation gate). It forks
# chats, consolidates the Mind graph, edits skills, fixes apps, writes
# the brief to reports/<date>.html via the storage API, opens the
# morning chat, and commits — all itself, instructed by its skill
# (/data/shared/skills/dreaming.md), per Möbius's "code empowers the
# agent; it does not police it." Reversibility comes from git, not from
# walls. So this file gathers a little read-only context for the agent,
# exports the few env vars its shell needs, runs the runner under a lock
# + timeout, and records how the night finished.
#
# Invoked by cron as: /data/apps/dreaming/fetch.sh <app_id>
# (the app id arrives as $1, per the cron-scaffold convention).
#
# DREAMING_DRY=1 skips the real agent run (records a dry outcome) so the
# plumbing — lock, inputs, env, heartbeat, cron_outcome — can be smoke-
# tested without spending a nightly run.
set -uo pipefail

APP_ID="${1:-}"
API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
DATA_DIR="${DATA_DIR:-/data}"
LOG="$DATA_DIR/cron-logs/dreaming.log"
LOCK="$DATA_DIR/cron-logs/dreaming.lock"
HEARTBEAT="$DATA_DIR/cron-logs/dreaming.heartbeat"
TOKEN_FILE="$DATA_DIR/service-token.txt"
DATE="$(date +%F)"
INPUTS="$DATA_DIR/apps/dreaming/inputs"
# Wall-clock start, captured BEFORE any early exit (lock / token / app-id) so
# emit_outcome can always record how long the night ran (duration_ms).
START_EPOCH="$(date +%s)"
RUNNER="${DREAMING_RUNNER:-/app/scripts/dreaming_runner.py}"
# Wall-clock cap for the whole night. Generous (the agent does real,
# multi-phase work) but bounded so a wedged run can't hold the lock past
# the next night's schedule. Overridable for tests.
RUN_TIMEOUT="${DREAMING_TIMEOUT:-7200}"

# CLI credentials the spawned claude/codex binary reads. Exported (not
# just set) so the runner and any subprocess it forks inherit them.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"
export CODEX_HOME="${CODEX_HOME:-$DATA_DIR/cli-auth/codex}"
export API_BASE_URL DATA_DIR

mkdir -p "$DATA_DIR/cron-logs" "$INPUTS"
log() { echo "[$(date -Iseconds)] dreaming: $*" >>"$LOG"; }

# emit_outcome <exit_code> — one cron_outcome activity event recording
# how the night finished, so the next night's agent (and the Dreaming
# app) can see the run history. Routed through the API so one process
# owns the activity-log file handle. Defined early because the token
# guard below emits a failure outcome before the main run.
emit_outcome() {
  local exit_code="$1"
  [[ -r "$TOKEN_FILE" ]] || return 0
  local token ts payload dur_ms
  token="$(cat "$TOKEN_FILE")"
  ts="$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")"
  # How long the night ran, in ms (second-resolution clock x1000 — finer
  # precision isn't meaningful for a multi-minute nightly run). Lets the next
  # run's self-history flag a near-timeout night, not just pass/fail.
  dur_ms=$(( ( $(date +%s) - START_EPOCH ) * 1000 ))
  payload="$(printf '{"ev":"cron_outcome","ts":"%s","app_id":%s,"job":"dreaming","exit_code":%s,"duration_ms":%s}' \
    "$ts" "${APP_ID:-0}" "$exit_code" "$dur_ms")"
  # The activity log is the PRIMARY liveness signal the next night's run
  # reads, so a dropped emit is invisible there (only this .log file keeps
  # it). Retry a transient API blip (restart/overload) with backoff before
  # giving up.
  local attempt=0
  while (( attempt < 3 )); do
    if curl -fsS -X POST \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$API_BASE_URL/api/admin/activity/emit" >/dev/null 2>>"$LOG"; then
      return 0
    fi
    attempt=$(( attempt + 1 ))
    (( attempt < 3 )) && { log "WARN cron_outcome emit attempt $attempt failed; retrying"; sleep $(( 2 ** attempt )); }
  done
  log "WARN cron_outcome emit failed after 3 attempts (rc=$exit_code); NOT recorded in activity log"
  return 1
}

# --- no-overlap lock (flock) ------------------------------------------
# fd 9 holds the lock for the life of this process; flock -n fails fast
# if a prior night is still running (a long run that overran its window).
# Exit-code legend (recorded as the cron_outcome exit_code, so the next
# run + the Dreaming app can tell a real success from a no-op):
#   0  success           3  service token missing
#   2  app id missing    5  skipped (a prior run still holds the lock)
#   124 wall-clock timeout    other  agent run error
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another dreaming run holds the lock; skipping this night (exit 5)"
  emit_outcome 5
  exit 5
fi

# --- token: export for the agent's shell (NOT a boundary) -------------
# The agent does its own privileged work (API reads, storage writes,
# notifications, git) using this token. We export it; we do NOT mediate
# the agent's use of it. A missing token means the agent can't reach the
# API, so fail loud rather than run a crippled night.
if [[ ! -r "$TOKEN_FILE" ]]; then
  log "ERROR service token unreadable ($TOKEN_FILE) — is the instance signed out? exiting"
  emit_outcome 3
  exit 3
fi
SERVICE_TOKEN="$(cat "$TOKEN_FILE")"
# Both names: AGENT_TOKEN is what the skill's curl examples use; the
# wrapper-era scripts read SERVICE_TOKEN. Export both so either works.
export SERVICE_TOKEN AGENT_TOKEN="$SERVICE_TOKEN"
auth=(-H "Authorization: Bearer $SERVICE_TOKEN")

# App id ($1) scopes storage + the cron_outcome. Checked AFTER the token
# block (not before, as it was) so a missing id is still recorded in the
# activity log — emit_outcome needs the token, so an earlier exit was
# invisible there, asymmetric with the token-missing path above.
if [[ -z "$APP_ID" ]]; then
  log "ERROR no app id passed as \$1; exiting"
  emit_outcome 2
  exit 2
fi

log "start (app_id=$APP_ID date=$DATE dry=${DREAMING_DRY:-0} timeout=${RUN_TIMEOUT}s)"

# --- gather read-only inputs for the agent ----------------------------
# The agent reads these from inputs/ as its starting context. It can (and
# does) gather more itself with its token — these are just the obvious
# 24h slices so it doesn't spend its first turns on boilerplate API
# calls. All best-effort: a failed gather leaves a stale/empty file and
# the agent copes.

# activity.jsonl — last 24h of platform events (app opens, storage
# writes, cron_outcomes). The runner's goal message points the agent here.
SINCE="$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
curl -s "${auth[@]}" "$API_BASE_URL/api/admin/activity?since=$SINCE" \
  >"$INPUTS/activity.jsonl" 2>>"$LOG" || true

# changed-since-last-run.txt — files changed in /data since the agent's last SUCCESSFUL
# nightly run. The marker records the HEAD that run ENDED at, so this diff is the DAY's
# work and naturally excludes the agent's own prior-night commits (no exclude-own-commits
# machinery needed). Diff-from-marker (not a fixed window) catches every committer; the
# agent is trusted to skip unchanged items. No marker yet -> a note; fall back to the 24h
# slices. Pathspecs (drop binary/DB/log/cache churn) come from the module (single source).
MARKER="$DATA_DIR/apps/dreaming/last-run.json"
LAST_SHA="$(PYTHONPATH=/app python3 -c "from app import dreaming_checkpoint as dc; m=dc.read_marker('$MARKER') or {}; print((m.get('repos') or {}).get('data',''))" 2>/dev/null)"
if [ -n "$LAST_SHA" ] && git -C "$DATA_DIR" cat-file -e "${LAST_SHA}^{commit}" 2>/dev/null; then
  mapfile -t _ps < <(PYTHONPATH=/app python3 -c "from app import dreaming_checkpoint as dc; print(chr(10).join(dc.EXCLUDE_PATHSPECS))" 2>/dev/null)
  git -C "$DATA_DIR" diff --name-only "$LAST_SHA"..HEAD -- "${_ps[@]}" \
    >"$INPUTS/changed-since-last-run.txt" 2>>"$LOG" || true
else
  echo "(no prior marker — first tracked run; use the 24h slices below)" >"$INPUTS/changed-since-last-run.txt"
fi

# dreaming-run-history.txt — the agent's OWN track record, so it can reflect on
# recurring failures (e.g. repeated max_turns deaths) instead of dreaming with
# amnesia each night. Three best-effort sources: the cron_outcome ledger (this
# skill's exit codes over ~14 nights), recent WARN/ERROR/steering lines from
# its own dreaming.log, and its last self-edits to this skill. All wrapped so a
# failed source degrades to a note rather than aborting.
HIST="$INPUTS/dreaming-run-history.txt"
SINCE_14D="$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "$SINCE")"
curl -s "${auth[@]}" "$API_BASE_URL/api/admin/activity?since=$SINCE_14D" \
  >"$INPUTS/.activity-14d.jsonl" 2>>"$LOG" || true
{
  echo "# Your own recent runs — read this BEFORE deciding what to improve tonight."
  echo
  echo "## Outcomes (last ~14 nights, from cron_outcome activity events)"
  # Heredoc reads the staged file via argv (a heredoc IS stdin, so it can't also
  # consume a pipe) — lets the program use any quoting freely.
  python3 - "$INPUTS/.activity-14d.jsonl" <<'PY' 2>>"$LOG" || echo "(outcome history unavailable)"
import sys, json
legend = {0: "success", 2: "agent run error / max_turns (instant ~0s = wrapper config, e.g. missing app-id)",
          3: "token missing", 5: "skipped (overlap)", 124: "wall-clock timeout"}
rows = []
try:
    with open(sys.argv[1]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("ev") == "cron_outcome" and ev.get("job") == "dreaming":
                rows.append(ev)
except Exception:
    pass
if not rows:
    print("(no prior cron_outcome events yet — first tracked nights)")
for ev in rows[-14:]:
    code = ev.get("exit_code")
    ts = (ev.get("ts") or "")[:19]
    dur = ev.get("duration_ms")
    dur_s = "  %ds" % round(dur / 1000) if isinstance(dur, (int, float)) else ""
    print("%s  exit=%s  %s%s" % (ts, code, legend.get(code, "agent error"), dur_s))
PY
  echo
  echo "## Recent friction (WARN/ERROR/steering from your dreaming.log, last 40)"
  if [ -s "$LOG" ]; then
    grep -aE 'WARN|ERROR|error_max_turns|injected turn-budget|run ended in error' \
      "$LOG" 2>/dev/null | tail -40 || true
  else
    echo "(no dreaming.log yet)"
  fi
  echo
  echo "## Your last 10 edits to THIS skill (git log -- shared/skills/dreaming.md)"
  git -C "$DATA_DIR" log --oneline -10 -- shared/skills/dreaming.md 2>>"$LOG" || echo "(no history)"
} >"$HIST" 2>>"$LOG" || true
rm -f "$INPUTS/.activity-14d.jsonl"

# chats.md — recent chats list (titles + ids + provider), so the agent
# knows which sessions to fork-and-interview without re-deriving the list.
python3 - "$API_BASE_URL" "$SERVICE_TOKEN" >"$INPUTS/chats.md" 2>>"$LOG" <<'PY' || true
import json, sys, urllib.request
base, token = sys.argv[1], sys.argv[2]
def get(path):
    req = urllib.request.Request(base+path, headers={"Authorization": "Bearer "+token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)
print("# Recent chats (fork + interview the ones with activity)\n")
print("# `[app]` rows are app-driven chats (created_by_app_id set): hidden from")
print("# the user's drawer but yours to read for the Mind graph. `updated` is the")
print("# cadence signal — interview the most recently/often active first.\n")
try:
    # include_app_chats=1 surfaces app-created chats too — they're excluded from
    # the owner's drawer history but are relevant to memory consolidation.
    chats = get("/api/chats?include_app_chats=1")
    chats = chats if isinstance(chats, list) else chats.get("chats", [])
    chats = sorted(chats, key=lambda c: c.get("updated_at",""), reverse=True)[:20]
    for c in chats:
        cid = c.get("id"); title = c.get("title") or "(untitled)"
        prov = c.get("provider") or "claude"
        updated = c.get("updated_at","")
        tag = "  [app]" if c.get("created_by_app_id") else ""
        print(f"- `{cid}`  [{prov}]{tag}  {title}  (updated {updated})")
    if not chats:
        print("(no chats)")
except Exception as e:
    print(f"(could not list chats: {e})")
PY

# app-feedback.md — cross-app feedback forms written under
# shared/app-feedback/<app-slug>/. Dreaming can use these as durable
# product/editorial signals without needing to know each app's numeric id.
python3 - "$API_BASE_URL" "$SERVICE_TOKEN" >"$INPUTS/app-feedback.md" 2>>"$LOG" <<'PY' || true
import json, sys, urllib.parse, urllib.request
base, token = sys.argv[1].rstrip("/"), sys.argv[2]
headers = {"Authorization": "Bearer "+token}

def get_json(path):
    req = urllib.request.Request(base+path, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def list_entries(prefix, limit=500, max_pages=20):
    cursor = None
    seen = set()
    entries = []
    for _ in range(max_pages):
        path = "/api/storage/shared-list/" + urllib.parse.quote(prefix.strip("/"), safe="/")
        params = {"limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        path += "?" + urllib.parse.urlencode(params)
        data = get_json(path)
        entries.extend(data.get("entries", []))
        nxt = data.get("next_cursor")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return entries

print("# Recent app feedback\n")
try:
    entries = []
    app_dirs = []
    for entry in list_entries("app-feedback"):
        name = entry.get("name")
        path = entry.get("path")
        if entry.get("type") == "dir" and isinstance(path, str):
            app_dirs.append(path)
        elif entry.get("type") == "dir" and isinstance(name, str):
            app_dirs.append("app-feedback/" + name)
        elif entry.get("type") == "file" and str(name or "").endswith(".json"):
            entries.append(entry)
    for app_dir in sorted(set(app_dirs)):
        for entry in list_entries(app_dir):
            if entry.get("type") == "file" and str(entry.get("name", "")).endswith(".json"):
                entries.append(entry)
    entries = sorted(entries, key=lambda e: e.get("modified_at", ""), reverse=True)[:20]
    if not entries:
        print("(no app feedback)")
    for entry in entries:
        path = entry.get("path") or f"app-feedback/{entry.get('name','')}"
        try:
            item = get_json("/api/storage/shared/" + urllib.parse.quote(path, safe="/"))
            app = item.get("app") or item.get("app_id") or "app"
            signal = item.get("signal") or "note"
            date = item.get("report_date") or item.get("created_at") or ""
            text = (item.get("text") or "").replace("\n", " ").strip()
            print(f"- [{app}] {signal} {date}: {text or '(no note)'}")
        except Exception as exc:
            print(f"- {path}: could not read ({exc})")
except Exception as e:
    print(f"(could not list app feedback: {e})")
PY

# prev-report.html — yesterday's brief, so the agent doesn't repeat
# itself. Enumerate every cursor page and fetch the newest report.
PREV="$(API_BASE_URL="$API_BASE_URL" APP_ID="$APP_ID" SERVICE_TOKEN="$SERVICE_TOKEN" python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.parse, urllib.request

base = os.environ["API_BASE_URL"].rstrip("/")
app_id = os.environ["APP_ID"]
token = os.environ["SERVICE_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}
cursor = None
seen = set()
reports = []

try:
    for _ in range(50):
        url = f"{base}/api/storage/apps-list/{app_id}/reports/"
        if cursor:
            url += "?" + urllib.parse.urlencode({"cursor": cursor})
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        for entry in data.get("entries", []):
            name = entry.get("name")
            if entry.get("type") == "file" and isinstance(name, str) and name.endswith(".html"):
                reports.append(name)
        nxt = data.get("next_cursor")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    print(sorted(reports)[-1] if reports else "")
except Exception as exc:
    print(f"could not enumerate previous reports: {exc}", file=sys.stderr)
    print("")
PY
)"
if [[ -n "$PREV" ]]; then
  curl -s "${auth[@]}" "$API_BASE_URL/api/storage/apps/$APP_ID/reports/$PREV" \
    >"$INPUTS/prev-report.html" 2>>"$LOG" || true
fi

# per-app-digest.json — compact per-app analytics summary the Dreaming
# agent uses to triage which apps need attention tonight. Produced from
# THREE sources:
#   - activity.jsonl ON DISK (already staged above) for opens_24h counts
#   - activity.jsonl app_error events for UNCAUGHT crashes (app_errors_24h +
#     recent_app_errors per app; shell_errors_24h for owner-shell errors) —
#     these fire even when the app never called signal('error')
#   - each app's signals.jsonl read via the storage API for signal counts,
#     last-5-error messages (EXPLICIT signal('error') calls), and has_signals
# The two error channels are kept as SEPARATE fields on purpose: last_5_errors
# is what an app explicitly reported; recent_app_errors is what the browser
# caught uncaught. ~2–3 KB for 12 apps vs 10–100 KB of raw log; gives the agent
# a digest-first orientation so it doesn't burn turns re-reading raw events.
# Graceful on API errors: a failed app-read records has_signals:false and
# an error note rather than aborting the whole step.
PYTHONPATH=/app python3 - "$API_BASE_URL" "$SERVICE_TOKEN" "$INPUTS" \
  >"$INPUTS/per-app-digest.json" 2>>"$LOG" <<'PY' || true
import json, os, sys, urllib.request, urllib.error, datetime

# app_error classification lives in app.dreaming_digest (unit-tested). Import
# defensively: if PYTHONPATH=/app is somehow unavailable (an older instance),
# the digest still builds, just without the uncaught-error fields.
try:
    from app import dreaming_digest
    _summarize_app_errors = dreaming_digest.summarize_app_errors
except Exception:
    _summarize_app_errors = None

base    = sys.argv[1].rstrip("/")
token   = sys.argv[2]
inp_dir = sys.argv[3]
headers = {"Authorization": "Bearer " + token}
now_utc = datetime.datetime.now(datetime.timezone.utc)
cutoff  = now_utc - datetime.timedelta(hours=24)

# --- helpers ---

def api_get(path, timeout=20):
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def storage_get_text(app_id, path, timeout=15):
    """Fetch a text file from an app's storage; return None on 404/error."""
    url = f"{base}/api/storage/apps/{app_id}/{path}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None

# --- scan the already-staged 24h activity.jsonl ONCE for two things:
#     app_open counts (opens_24h) and app_error events (uncaught crashes).
#     Kept as two independent accumulators so the signals.jsonl channel below
#     can't corrupt them. ---
activity_path = os.path.join(inp_dir, "activity.jsonl")
opens_by_app = {}        # app_id (str) -> count
app_error_events = []    # raw app_error rows, classified after the loop
if os.path.exists(activity_path):
    with open(activity_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("ev")
            if kind == "app_open":
                aid = str(ev.get("app_id", ""))
                if aid:
                    opens_by_app[aid] = opens_by_app.get(aid, 0) + 1
            elif kind == "app_error":
                app_error_events.append(ev)

# Classify the uncaught errors (the staged file is already the 24h window, so
# no extra time filtering is needed). Shell errors (no app_id) bucket separately.
err_summary = (
    _summarize_app_errors(app_error_events)
    if _summarize_app_errors
    else {"by_app": {}, "shell": {"count": 0, "recent": []}}
)

# --- fetch app list ---
try:
    apps = api_get("/api/apps/")
    if isinstance(apps, dict):
        apps = apps.get("apps", [])
except Exception as e:
    # API unavailable — write an empty digest so the agent knows it failed
    print(json.dumps({"_error": str(e), "apps": []}))
    sys.exit(0)

digests = []
for app in apps:
    app_id  = str(app.get("id", ""))
    slug    = app.get("name") or app.get("slug") or app_id
    name    = app.get("display_name") or slug
    if not app_id:
        continue

    opens_24h = opens_by_app.get(app_id, 0)

    # Parse signals.jsonl for this app from the storage API.
    signal_counts = {}
    last_5_errors = []
    has_signals   = False
    signals_error = None
    try:
        raw = storage_get_text(app_id, "signals.jsonl")
        if raw:
            has_signals = True
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    sig = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Count by name, limited to the last 24h
                ts_str = sig.get("ts", "")
                try:
                    ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    # Make tz-aware for comparison
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=datetime.timezone.utc)
                    if ts < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
                sname = sig.get("name", "")
                if sname:
                    signal_counts[sname] = signal_counts.get(sname, 0) + 1
                # Collect last-5 error messages (newest last in file → reverse later)
                if sname == "error":
                    msg = sig.get("message") or sig.get("msg") or ""
                    if msg:
                        last_5_errors.append(str(msg)[:200])
    except Exception as e:
        signals_error = str(e)[:200]

    app_err = err_summary["by_app"].get(app_id, {})
    entry = {
        "app_id":      app_id,
        "slug":        slug,
        "name":        name,
        "opens_24h":   opens_24h,
        "has_signals": has_signals,
        "signal_counts": signal_counts,
        # Two error channels, kept separate: last_5_errors = signalled
        # (signal('error')); app_errors_24h/recent_app_errors = uncaught
        # (activity app_error) — the primary crash signal, fires without a call.
        "last_5_errors": last_5_errors[-5:],
        "app_errors_24h": app_err.get("count", 0),
        "recent_app_errors": app_err.get("recent", []),
    }
    if signals_error:
        entry["signals_read_error"] = signals_error
    digests.append(entry)

print(json.dumps({
    "generated_at": now_utc.isoformat(),
    "apps": digests,
    # Owner-shell errors (app_error with no app_id) have no per-app home;
    # surface them at the top level so they aren't lost.
    "shell_errors_24h": err_summary["shell"]["count"],
    "recent_shell_errors": err_summary["shell"]["recent"],
}, indent=2))
PY

# Record the app id where the runner's goal message and the agent can
# find it (the agent writes reports to apps/<app_id>/reports/).
printf '%s\n' "$APP_ID" >"$INPUTS/app_id"
log "gathered inputs (activity, chats, app-feedback, prev-report, per-app-digest) into $INPUTS/"

# --- heartbeat: prove liveness while the long run is in flight --------
# A background loop touches the heartbeat file every 60s. A monitor (or a
# morning glance) can `stat` it to tell "still dreaming" from "wedged".
# Killed in the cleanup trap below.
#
# fd 9 (the flock handle) is CLOSED in the child (`9>&-`) so the lock is
# held ONLY by the main process. Without this, the backgrounded child
# inherits fd 9 and keeps the lock alive past the parent's exit until the
# child is reaped — so the NEXT night's run would spuriously see "another
# run holds the lock" and skip. The cleanup trap kills the child and
# waits for it so the lock is fully released by the time we exit.
heartbeat_loop() {
  while true; do
    date -Iseconds >"$HEARTBEAT" 2>/dev/null || true
    sleep 60
  done
}
heartbeat_loop 9>&- &
HEARTBEAT_PID=$!
cleanup() {
  if [[ -n "${HEARTBEAT_PID:-}" ]]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- run the agent: full tools, real token, no sandbox ----------------
# The runner loads the dreaming skill as the system prompt, sends the
# goal as the first user message, and drives the multi-turn loop. `timeout`
# bounds wall-clock; --signal=TERM gives the run a chance to flush before
# SIGKILL (--kill-after). The runner streams its own trace into $LOG.
RC=0
if [[ "${DREAMING_DRY:-0}" == "1" ]]; then
  log "DRY run: skipping agent; recording dry outcome"
  RC=0
elif [[ ! -r "$RUNNER" ]]; then
  log "ERROR runner not found/readable at $RUNNER; exiting"
  RC=127
else
  timeout --signal=TERM --kill-after=60 "$RUN_TIMEOUT" \
    python3 "$RUNNER" >>"$LOG" 2>&1
  RC=$?
  if [[ "$RC" == "124" ]]; then
    log "WARN agent run hit the ${RUN_TIMEOUT}s timeout (terminated)"
  elif [[ "$RC" != "0" ]]; then
    log "WARN agent run exited non-zero (rc=$RC)"
  fi
fi

# --- final safety-net commit ------------------------------------------
# The agent commits as it goes (pm-commit per chunk). This is a backstop:
# if the run was killed mid-chunk, sweep any agent-touched files into one
# commit so nothing is left dirty + unreversible. pm-commit's denylist +
# 50-file guard keep this honest; --allow-broad because a full night can
# legitimately touch many files (skills, memory notes, app sources).
if command -v pm-commit >/dev/null 2>&1; then
  ( cd "$DATA_DIR" && pm-commit --allow-broad "dreaming: nightly safety-net commit $DATE" \
      >>"$LOG" 2>&1 ) || true
fi

# --- emit cron_outcome ------------------------------------------------
emit_outcome "$RC"

# --- advance the last-run marker (success only) -----------------------
# Record the /data HEAD the agent ended at, so next night's diff-from-marker is the DAY's
# new work and skips tonight's own commits. ONLY on a clean run (rc 0) — a failed/killed
# night leaves the marker so its window is re-reviewed (re-reading is cheap; missing isn't).
if [[ "$RC" == "0" ]]; then
  _head="$(git -C "$DATA_DIR" rev-parse HEAD 2>/dev/null || true)"
  [ -n "$_head" ] && PYTHONPATH=/app python3 -c "from app import dreaming_checkpoint as dc; import datetime; dc.write_marker('$MARKER', {'repos': {'data': '$_head'}, 'ts': datetime.datetime.now(datetime.timezone.utc).isoformat()})" >>"$LOG" 2>&1 || true
fi

# --- failure push (card 125) ------------------------------------------
# A failed/killed night must actively reach the owner (not just sit in the activity log),
# so they don't wake to nothing. rc 5 is a benign no-overlap skip — don't alarm on it.
# The body is honest about whether a brief actually landed: a non-zero main run can still
# leave a brief behind (the runner's guaranteed-brief fallback rescues one), so "no morning
# brief" would cry wolf on a rescued night. Check the brief file the agent writes to the
# app's numeric storage dir before wording the push.
if [[ "$RC" != "0" && "$RC" != "5" ]]; then
  if [[ -f "$DATA_DIR/apps/$APP_ID/reports/$DATE.html" ]]; then
    PUSH_BODY="Last night ended rc=$RC but a recovery brief was salvaged — open Dreaming. See /data/cron-logs."
  else
    PUSH_BODY="Last night ended rc=$RC — no morning brief. See /data/cron-logs."
  fi
  curl -s "${auth[@]}" -X POST "$API_BASE_URL/api/notifications/send" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"title":"Dreaming run failed","body":sys.argv[1]}))' "$PUSH_BODY")" \
    >>"$LOG" 2>&1 || true
fi

log "done (rc=$RC)"
exit "$RC"
