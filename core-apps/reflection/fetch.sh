#!/bin/bash
# fetch.sh — the nightly "reflection" wrapper. Thin by design: it owns
# only the OPERATIONAL concerns of an unattended cron run — no overlap,
# a wall-clock timeout, liveness heartbeats, an outcome event — and then
# hands the night to the agent.
#
# Unlike v1, this wrapper is NOT a security boundary. The Reflection agent
# runs with FULL tools and a REAL token (no staging tree, no
# Bash-less/token-less envelope, no graph validation gate). It forks
# chats, consolidates the memory graph, edits skills, fixes apps, writes
# the brief to reports/<date>.html via the storage API, opens the
# morning chat, and commits — all itself, instructed by its skill
# (/data/shared/skills/reflection.md), per Möbius's "code empowers the
# agent; it does not police it." Reversibility comes from git, not from
# walls. So this file gathers a little read-only context for the agent,
# exports the few env vars its shell needs, runs the runner under a lock
# + timeout, and records how the night finished.
#
# Invoked by cron as: /data/apps/reflection/fetch.sh <app_id>
# (the app id arrives as $1, per the cron-scaffold convention).
#
# REFLECTION_DRY=1 skips the real agent run (records a dry outcome) so the
# plumbing — lock, inputs, env, heartbeat, cron_outcome — can be smoke-
# tested without spending a nightly run.
set -uo pipefail

APP_ID="${1:-}"
API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
DATA_DIR="${DATA_DIR:-/data}"
LOG="$DATA_DIR/cron-logs/reflection.log"
LOCK="$DATA_DIR/cron-logs/reflection.lock"
HEARTBEAT="$DATA_DIR/cron-logs/reflection.heartbeat"
TOKEN_FILE="$DATA_DIR/service-token.txt"
DATE="$(date +%F)"
INPUTS="$DATA_DIR/apps/reflection/inputs"
RUNNER="${REFLECTION_RUNNER:-/app/scripts/reflection_runner.py}"
# Wall-clock cap for the whole night. Generous (the agent does real,
# multi-phase work) but bounded so a wedged run can't hold the lock past
# the next night's schedule. Overridable for tests.
RUN_TIMEOUT="${REFLECTION_TIMEOUT:-7200}"

# CLI credentials the spawned claude/codex binary reads. Exported (not
# just set) so the runner and any subprocess it forks inherit them.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"
export CODEX_HOME="${CODEX_HOME:-$DATA_DIR/cli-auth/codex}"
export API_BASE_URL DATA_DIR

mkdir -p "$DATA_DIR/cron-logs" "$INPUTS"
log() { echo "[$(date -Iseconds)] reflection: $*" >>"$LOG"; }

# Tracks whether the cron_outcome has been emitted yet, so the cleanup trap
# can emit a backstop outcome if the run exits before the explicit emit near
# the end (an unexpected failure between the two — e.g. an ENOSPC that kills a
# step). A dropped outcome makes the night invisible to the next run and the
# app, which then shows the PRIOR night's status; the backstop closes that gap.
OUTCOME_EMITTED=0

# emit_outcome <exit_code> — one cron_outcome activity event recording
# how the night finished, so the next night's agent (and the Reflection
# app) can see the run history. Routed through the API so one process
# owns the activity-log file handle. Defined early because the token
# guard below emits a failure outcome before the main run.
emit_outcome() {
  local exit_code="$1"
  # Mark "attempted" before the token guard: whether or not we can reach the
  # API, the backstop in cleanup() must not re-attempt (it would fail the same
  # way — a missing token or a full disk doesn't heal within the run).
  OUTCOME_EMITTED=1
  [[ -r "$TOKEN_FILE" ]] || return 0
  local token ts payload
  token="$(cat "$TOKEN_FILE")"
  ts="$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")"
  payload="$(printf '{"ev":"cron_outcome","ts":"%s","app_id":%s,"job":"reflection","exit_code":%s}' \
    "$ts" "${APP_ID:-0}" "$exit_code")"
  # The activity log is the PRIMARY liveness signal the next night's run
  # reads, so a dropped emit is invisible there (only this .log file keeps
  # it). Retry a transient API blip (restart/overload) with backoff before
  # giving up.
  local attempt=0
  while (( attempt < 3 )); do
    if curl -fsS --connect-timeout 10 --max-time 30 -X POST \
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

cleanup() {
  # Capture the exiting status BEFORE any other command overwrites $?. On a
  # signal-driven exit bash (5.x, verified) still runs this EXIT trap, but
  # $? here holds the STALE last-command status — often 0 — not 128+signal.
  # Reading the also-stale $RC instead once recorded exit_code:0 for a night
  # that was SIGTERMed mid-runner and produced no brief.
  local rc=$?
  # Backstop the cron_outcome: if we're exiting before the explicit
  # emit_outcome near the end (an unexpected failure in between, or an
  # external kill), still record SOMETHING, since a dropped outcome makes the
  # night invisible to the next run + the app (which then shows the PRIOR
  # night's status). Idempotent — emit_outcome sets OUTCOME_EMITTED, so the
  # normal path's explicit emit already ran and this is skipped. The backstop
  # can NEVER record success: reaching it means the normal path did not
  # complete, so a zero status is the staleness above, not a healthy run —
  # substitute 70 ("died before completing", see the exit-code legend).
  if [[ "${OUTCOME_EMITTED:-0}" != "1" ]]; then
    (( rc == 0 )) && rc=70
    emit_outcome "$rc"
  fi
  if [[ -n "${HEARTBEAT_PID:-}" ]]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
  fi
}
# EXIT-only on purpose. Verified on bash 5.2: an UNTRAPPED fatal SIGTERM (or
# group SIGINT) during the foreground runner still fires the EXIT trap
# promptly, while a TRAPPED signal is DEFERRED until the foreground child
# completes — so trapping TERM/INT would postpone the backstop by up to the
# whole RUN_TIMEOUT and lose it entirely under a kill -9 escalation.
# Installed HERE, before the lock + input gather, so an external kill during
# the gather still records an outcome — every early exit below emits first,
# which makes the backstop a no-op on those paths.
trap cleanup EXIT

# --- no-overlap lock (flock) ------------------------------------------
# fd 9 holds the lock for the life of this process; flock -n fails fast
# if a prior night is still running (a long run that overran its window).
# Exit-code legend (recorded as the cron_outcome exit_code, so the next
# run + the Reflection app can tell a real success from a no-op):
#   0    success                  3    service token missing (config)
#   2    app id missing (config)  5    skipped (a prior run holds the lock)
#   124  wall-clock timeout       127  runner missing
#   64   model error   65  usage/rate limit   66  provider auth expired
#   70   wrapper died before completing (backstop-only: external kill or an
#        unexpected failure any time after startup, including during the
#        input gather)
# The 64-66 band is the RUNNER's own (reflection_runner.py); it can never
# collide with the wrapper config codes above, so a model/usage/auth failure
# is never mislabeled as a config error in cron_outcome. 70 is emitted only
# by the cleanup trap's backstop, never by a completed run.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another reflection run holds the lock; skipping this night (exit 5)"
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

log "start (app_id=$APP_ID date=$DATE dry=${REFLECTION_DRY:-0} timeout=${RUN_TIMEOUT}s)"

# --- gather read-only inputs for the agent ----------------------------
# The agent reads these from inputs/ as its starting context. It can (and
# does) gather more itself with its token — these are just the obvious
# 24h slices so it doesn't spend its first turns on boilerplate API
# calls. All best-effort: a failed gather leaves a stale/empty file and
# the agent copes.

# activity.jsonl — last 24h of platform events (app opens, storage
# writes, cron_outcomes). The runner's goal message points the agent here.
# A SILENT gather failure is indistinguishable from a genuinely quiet night
# (both leave empty inputs and exit 0), so every gather records its transport
# failures into gather-errors.txt — a machine-readable signal the runner hands
# the agent so the brief can tell "broken" from "quiet." Empty file = all OK.
GATHER_ERRORS="$INPUTS/gather-errors.txt"
: >"$GATHER_ERRORS"
record_gather_error() { printf '%s\n' "$1" >>"$GATHER_ERRORS"; }

# The gather curls run while the flock is held and OUTSIDE the runner's
# `timeout`, so an un-timed curl against a stalled connection would hold the
# lock forever and wedge every future night at `flock -n`. --connect-timeout +
# --max-time bound each one; a stall then returns non-zero and is recorded.
curl_timed=(curl -s --connect-timeout 10 --max-time 60)

SINCE="$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
if ! "${curl_timed[@]}" "${auth[@]}" \
    "$API_BASE_URL/api/admin/activity?since=$SINCE" \
    >"$INPUTS/activity.jsonl" 2>>"$LOG"; then
  record_gather_error "activity.jsonl: fetch failed (transport error or timeout)"
fi

# reflection-run-history.txt — the agent's own recent runs. Phase 0 of the
# skill reads this file FIRST — a failure recurring across nights is
# tonight's first fix. Staged by the wrapper because the agent should not
# burn turns reconstructing its own run history. 14 days back: enough
# nights to see a recurrence. Fetched to a dot-prefixed temp first because
# grep exits 1 on no-match under pipefail, and a transport error must be
# distinguishable (via gather-errors.txt) from a genuinely empty history.
HIST_SINCE="$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
HIST_TMP="$INPUTS/.run-history-activity.jsonl"
if ! "${curl_timed[@]}" "${auth[@]}" \
    "$API_BASE_URL/api/admin/activity?since=$HIST_SINCE&app_id=$APP_ID" \
    >"$HIST_TMP" 2>>"$LOG"; then
  record_gather_error "reflection-run-history.txt: activity fetch failed (transport error or timeout)"
fi
{
  echo '# Your recent reflection runs (cron_outcome events, newest last).'
  echo '# Exit codes: 0 ok · 2 app-id missing · 3 token missing · 5 lock-skip · 64 model error · 65 usage limit · 66 auth expired · 70 died before completing · 124 timeout · 127 runner missing'
  grep '"ev":"cron_outcome"' "$HIST_TMP" | grep '"job":"reflection"' | tail -n 14 || true
  echo ''
  echo '# Your recent edits to your own skill (newest first):'
  git -C "$DATA_DIR" log --since="14 days ago" --format="%ad %s" --date=short \
    -- shared/skills/reflection.md 2>>"$LOG" || true
  echo ''
  echo "# Full wrapper log (grep WARN/ERROR for friction): $DATA_DIR/cron-logs/reflection.log"
} >"$INPUTS/reflection-run-history.txt"
rm -f "$HIST_TMP"

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
print("# the user's drawer but yours to read for the memory graph. `updated` is the")
print("# cadence signal — interview the most recently/often active first.\n")
try:
    # include_app_chats=1 surfaces app-created chats too — they're excluded from
    # the owner's drawer history but are relevant to memory consolidation.
    chats = get("/api/chats?include_app_chats=1")
    chats = chats if isinstance(chats, list) else chats.get("chats", [])
    chats = sorted(chats, key=lambda c: c.get("updated_at",""), reverse=True)
    # Cap the list, but MUCH higher than the old top-20: on a busy day the
    # 21st-most-recent chat was silently never offered for interview. A cap of
    # 60 covers realistic nightly volume; if it's ever exceeded, SURFACE the
    # truncation (a visible note here + a stderr line into the log) instead of
    # dropping chats without a trace. The agent already self-dedups
    # re-interviews via its run notes, so a generous cap only costs a longer
    # list, never a double interview.
    CAP = 60
    total = len(chats)
    if total > CAP:
        chats = chats[:CAP]
        print(f"# NOTE: {total} chats total; showing the {CAP} most recent — "
              f"{total - CAP} older chat(s) were truncated from this list.\n")
        print(f"chats.md: truncated {total - CAP} of {total} chats (cap {CAP})",
              file=sys.stderr)
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
# shared/app-feedback/<app-slug>/. Reflection can use these as durable
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
    # Exclude TODAY's report. A same-day manual retry would otherwise pick
    # this run's own (possibly partial) brief as "yesterday's" — the agent
    # would then compare against itself. We want the latest report BEFORE
    # today, so a retry still sees the genuine previous brief.
    import datetime
    today_html = datetime.date.today().isoformat() + ".html"
    reports = [n for n in reports if n != today_html]
    print(sorted(reports)[-1] if reports else "")
except Exception as exc:
    print(f"could not enumerate previous reports: {exc}", file=sys.stderr)
    print("")
PY
)"
if [[ -n "$PREV" ]]; then
  if ! "${curl_timed[@]}" "${auth[@]}" \
      "$API_BASE_URL/api/storage/apps/$APP_ID/reports/$PREV" \
      >"$INPUTS/prev-report.html" 2>>"$LOG"; then
    record_gather_error "prev-report.html: fetch failed (transport error or timeout)"
  fi
fi

# prev-question-answers.json — the partner's taps on the in-brief question
# cards a recent brief offered. The app saved them to
# question-answers/<date>.json (bare object). No live agent waited; they are
# read HERE, on the next run, so the agent can ACT on them in phase 2. Stage
# the single most recent answer file (filenames are <report_date>.json,
# ISO-sortable).
PREV_QA="$(API_BASE_URL="$API_BASE_URL" APP_ID="$APP_ID" SERVICE_TOKEN="$SERVICE_TOKEN" python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.parse, urllib.request

base = os.environ["API_BASE_URL"].rstrip("/")
app_id = os.environ["APP_ID"]
token = os.environ["SERVICE_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}
cursor = None
seen = set()
files = []

try:
    for _ in range(50):
        url = f"{base}/api/storage/apps-list/{app_id}/question-answers/"
        if cursor:
            url += "?" + urllib.parse.urlencode({"cursor": cursor})
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        for entry in data.get("entries", []):
            name = entry.get("name")
            if entry.get("type") == "file" and isinstance(name, str) and name.endswith(".json"):
                files.append(name)
        nxt = data.get("next_cursor")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    print(sorted(files)[-1] if files else "")
except Exception as exc:
    # dir-not-created-yet (404) and any error both degrade to "no answers".
    print("", file=sys.stderr)
    print("")
PY
)"
if [[ -n "$PREV_QA" ]]; then
  if ! "${curl_timed[@]}" "${auth[@]}" \
      "$API_BASE_URL/api/storage/apps/$APP_ID/question-answers/$PREV_QA" \
      >"$INPUTS/prev-question-answers.json" 2>>"$LOG"; then
    record_gather_error "prev-question-answers.json: fetch failed (transport error or timeout)"
  fi
fi

# per-app-digest.json — compact per-app analytics summary the Reflection
# agent uses to triage which apps need attention tonight. Produced from
# two sources:
#   - activity.jsonl ON DISK (already staged above) for opens_24h counts
#   - each app's signals.jsonl read via the storage API for signal counts,
#     last-5-error messages, and the has_signals flag
# ~2–3 KB for 12 apps vs 10–100 KB of raw log; gives the agent a
# digest-first orientation so it doesn't burn turns re-reading raw events.
# Graceful on API errors: a failed app-read records has_signals:false and
# an error note rather than aborting the whole step.
APP_ID_FOR_DIGEST="$APP_ID" python3 - "$API_BASE_URL" "$SERVICE_TOKEN" "$INPUTS" \
  >"$INPUTS/per-app-digest.json" 2>>"$LOG" <<'PY' || true
import json, os, sys, urllib.request, urllib.error, datetime

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

# --- opens_24h: count app_open events in the already-staged activity.jsonl ---
activity_path = os.path.join(inp_dir, "activity.jsonl")
opens_by_app = {}   # app_id (str) -> count
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
            if ev.get("ev") != "app_open":
                continue
            aid = str(ev.get("app_id", ""))
            if aid:
                opens_by_app[aid] = opens_by_app.get(aid, 0) + 1

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

    entry = {
        "app_id":      app_id,
        "slug":        slug,
        "name":        name,
        "opens_24h":   opens_24h,
        "has_signals": has_signals,
        "signal_counts": signal_counts,
        "last_5_errors": last_5_errors[-5:],
    }
    if signals_error:
        entry["signals_read_error"] = signals_error
    digests.append(entry)

print(json.dumps({"generated_at": now_utc.isoformat(), "apps": digests}, indent=2))
PY

# Record the app id where the runner's goal message and the agent can
# find it (the agent writes reports to apps/<app_id>/reports/).
printf '%s\n' "$APP_ID" >"$INPUTS/app_id"
log "gathered inputs (activity, run-history, chats, app-feedback, prev-report, per-app-digest) into $INPUTS/"

# --- heartbeat: prove liveness while the long run is in flight --------
# A background loop touches the heartbeat file every 60s. A monitor (or a
# morning glance) can `stat` it to tell "still reflection" from "wedged".
# Killed in the cleanup trap above.
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

# --- run the agent: full tools, real token, no sandbox ----------------
# The runner loads the reflection skill as the system prompt, sends the
# goal as the first user message, and drives the multi-turn loop. `timeout`
# bounds wall-clock; --signal=TERM gives the run a chance to flush before
# SIGKILL (--kill-after). The runner streams its own trace into $LOG.
RC=0
if [[ "${REFLECTION_DRY:-0}" == "1" ]]; then
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
  ( cd "$DATA_DIR" && pm-commit --allow-broad "reflection: nightly safety-net commit $DATE" \
      >>"$LOG" 2>&1 ) || true
fi

# --- deterministic morning-brief push ---------------------------------
# Delivery of the morning push is owned HERE, by the wrapper — NOT by the
# agent. The agent composes the brief and writes state.json (streak +
# one-line `last_summary` headline for the app header); the wrapper reads
# that headline and fires the push via the notifications API with the
# service token.
#
# Why the wrapper and not the agent: an agent-chosen notification tool
# proved unreliable. From 2026-06-30 the nightly agent began reaching for
# a leaked Claude Code harness `PushNotification` tool (found via
# `ToolSearch: select:PushNotification`) instead of the documented
# `curl /api/notifications/send`. That harness tool is a no-op inside
# Möbius, so no morning brief reached the partner for a week even though
# every run succeeded and every brief was written. Making the wrapper the
# sole sender — exactly as news/fetch.sh already does — removes the
# dependency on the agent picking the right tool. Best-effort: a failed
# push is logged, never fatal.
send_morning_push() {
  [[ "$RC" == "0" ]] || { log "morning push: skip (rc=$RC)"; return 0; }
  local brief="$DATA_DIR/apps/$APP_ID/reports/$DATE.html"
  [[ -f "$brief" ]] || { log "morning push: skip (no brief for $DATE)"; return 0; }
  # Trust the headline only if state.json was written by TODAY's run;
  # fall back to a generic line otherwise so the partner is still pinged.
  local headline
  headline="$(APP_ID="$APP_ID" DATE="$DATE" DATA_DIR="$DATA_DIR" python3 - <<'PY' 2>>"$LOG"
import json, os
try:
    s = json.load(open(f"{os.environ['DATA_DIR']}/apps/{os.environ['APP_ID']}/state.json"))
except Exception:
    s = {}
head = (s.get("last_summary") or "").strip()
print(head if str(s.get("last_run", "")).startswith(os.environ["DATE"]) else "")
PY
)"
  [[ -n "$headline" ]] || headline="Your nightly reflection is ready to read."
  local payload
  payload="$(APP_ID="$APP_ID" HEADLINE="$headline" python3 - <<'PY' 2>>"$LOG"
import json, os
app_id = os.environ["APP_ID"]
target = f"/shell/?app={app_id}"
print(json.dumps({
    "title": "Your morning brief is ready",
    "body": os.environ["HEADLINE"][:200],
    "source_type": "app",
    "source_id": app_id,
    "target": target,
    "actions": [{"action": "open_app", "title": "Read", "target": target}],
}))
PY
)"
  local code
  # curl_timed, not bare curl: this POST runs while fd 9 still holds the
  # nightly flock, so an unbounded stall here would wedge every future night
  # (exit 5) exactly like the gather curls this run already bounds.
  code="$("${curl_timed[@]}" -o /dev/null -w '%{http_code}' -X POST "${auth[@]}" \
    -H "Content-Type: application/json" -d "$payload" \
    "$API_BASE_URL/api/notifications/send" 2>>"$LOG")"
  case "$code" in
    200|201|204) log "morning push sent (http=$code)";;
    *)           log "WARN morning push failed (http=$code)";;
  esac
}
send_morning_push

# --- emit cron_outcome ------------------------------------------------
emit_outcome "$RC"

log "done (rc=$RC)"
exit "$RC"
