#!/usr/bin/env python3
# Usage: python3 /data/platform/backend/scripts/reflection-evidence.py [--hours 72] [--limit 5] [--traces 12] [--memory-writer-packet] [CHAT_ID]
"""One consolidated evidence bundle for Reflection and related agent coaching.

Instead of running many sequential tools (read the memory run-status, tail the
update log, cross-check read-traces against the chat DB, tail reflection
metrics, list Reflection run artifacts, curl the skills API), this prints all
of it as a single readable report so coaching can start from one command.

Read-only, defensive (missing pieces are skipped, never fatal), python3 stdlib
+ sqlite3 only. It gathers evidence; it does not judge — Agent Coaching or
Reflection reads the bundle and forms the assessment.
"""

import argparse
import ast
import datetime as dt
import json
import os
import sqlite3
import subprocess
import urllib.parse
import urllib.request

DB_PATH = "/data/db/ultimate.db"
MEM_STATE = "/data/shared/memory/app-state"
MEM_RUN_STATUS = os.path.join(MEM_STATE, "run-status.json")
MEM_RUN_LOG = os.path.join(MEM_STATE, "run-log")
MEM_UPDATE_LOG = os.path.join(MEM_STATE, "update-log")
MEM_READ_TRACE = os.path.join(MEM_STATE, "read-trace")
MEM_RECALL_STATS = os.path.join(MEM_STATE, "recall-stats.json")
MEM_RECALL_AUDIT = os.path.join(MEM_STATE, "recall-audit")
MEM_REPOSITORY = "/data/shared/memory/repository"
MEMORY_SKILL = "/data/shared/skills/memory.md"
MEMORY_RUNNER = "/data/apps/memory/memory_runner.py"
REFLECTION_DIR = "/data/apps/reflection"
REFLECTION_METRICS = os.path.join(REFLECTION_DIR, "reflection-run-metrics.jsonl")
REFLECTION_RUNS = os.path.join(REFLECTION_DIR, "runs")
TOOL_FRICTION = os.path.join(REFLECTION_DIR, "tool_friction.py")

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def api_json(path):
    """Read one owner API JSON response; return None on any unavailable input."""
    base = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
    token = os.environ.get("AGENT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def installed_app(slug):
    apps = api_json("/api/apps/")
    if not isinstance(apps, list):
        return None
    return next(
        (
            app for app in apps
            if isinstance(app, dict) and app.get("slug") == slug
        ),
        None,
    )


def latest_cron_outcome(app_id, hours=168):
    since = (now_utc() - dt.timedelta(hours=hours)).isoformat()
    base = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
    token = os.environ.get("AGENT_TOKEN")
    if not token:
        return None
    query = urllib.parse.urlencode({"since": since, "app_id": app_id})
    req = urllib.request.Request(
        f"{base}/api/admin/activity?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    latest = None
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            for raw in resp:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if row.get("ev") != "cron_outcome":
                    continue
                if latest is None or str(row.get("ts") or "") > str(latest.get("ts") or ""):
                    latest = row
    except Exception:
        return None
    return latest


def parse_ts(value):
    """Parse an ISO-8601 timestamp defensively; return an aware datetime or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def short(text, width=64):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def sub(title):
    print("\n" + title)
    print("-" * len(title))


def open_db():
    """Open the chat DB read-only so this tool can never mutate live data."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def chat_titles(con, ids):
    """Map chat id -> title for the given ids (best effort; missing ids omitted)."""
    out = {}
    if not con or not ids:
        return out
    ids = list(ids)
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            q = "select id, title from chats where id in (%s)" % ",".join("?" * len(chunk))
            for cid, title in con.execute(q, chunk):
                out[cid] = title
    except sqlite3.Error:
        pass
    return out


# --------------------------------------------------------------------------- #
# Section: Memory consolidation agent
# --------------------------------------------------------------------------- #
def section_memory(limit):
    sub("MEMORY — scheduled consolidation agent")

    app = installed_app("memory")
    supervisor = (
        latest_cron_outcome(app.get("id")) if isinstance(app, dict) else None
    )
    status = None
    try:
        with open(MEM_RUN_STATUS) as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        pass

    if not status:
        print("  run-status: (not found — Memory app may not be installed)")
    else:
        started, finished = parse_ts(status.get("started_at")), parse_ts(status.get("finished_at"))
        dur = f"{(finished - started).total_seconds():.0f}s" if started and finished else "?"
        topo = (status.get("topology") or {})
        before, after = topo.get("before") or {}, topo.get("after") or {}
        print(f"  run_id={status.get('run_id','?')}  status={status.get('status','?')}  "
              f"model={status.get('model','?')}  provider={status.get('provider','?')}  "
              f"new_commit={status.get('new_commit')}")
        print(f"  last run: started {status.get('started_at','?')}  ({dur})")
        if after:
            dn = _delta(after.get("nodes"), before.get("nodes"))
            de = _delta(after.get("edges"), before.get("edges"))
            print(f"  graph now: {after.get('nodes','?')} nodes ({dn}) / "
                  f"{after.get('edges','?')} edges ({de}) / {after.get('problems','?')} problems")
    if supervisor:
        print(
            "  outer scheduler receipt (not run-id linked): "
            f"exit={supervisor.get('exit_code','?')}  "
            f"at={supervisor.get('ts','?')}  job={supervisor.get('job','?')}"
        )
    elif app:
        print("  outer scheduler receipt: (none recorded in the last 7 days)")
    if status:
        queued = status.get("queued_chat_count")
        sourced = status.get("source_chat_count")
        starved = status.get("chat_input_starved")
        if starved is None:
            starved = isinstance(queued, int) and queued > 0 and sourced == 0
        print(f"  queued_chats={status.get('queued_chat_count','?')}  "
              f"source_chats={status.get('source_chat_count','?')}  "
              f"input_starved={'yes' if starved else 'no'}  "
              f"changed={len(status.get('changed_paths') or [])}  "
              f"deleted={len(status.get('deleted_paths') or [])}")

    # Last few consolidation outcomes from the append-only update log.
    entries = _read_update_log(limit)
    if not entries:
        print("  update-log: (none found)")
        return
    print(f"  recent update-log outcomes (last {len(entries)}):")
    print(f"  aggregate: changed={sum(len(e.get('changed_paths') or []) for e in entries)}  "
          f"deleted={sum(len(e.get('deleted_paths') or []) for e in entries)}  "
          f"published={sum(1 for e in entries if e.get('status') == 'published')}/{len(entries)}")
    for e in entries:
        counts = e.get("counts") or {}
        ts = (e.get("timestamp") or "")[:16]
        print(f"    {ts or '?':16}  run={e.get('run_id') or '?'}  "
              f"changed={len(e.get('changed_paths') or [])}  "
              f"deleted={len(e.get('deleted_paths') or [])}  "
              f"problems={counts.get('problems','?')}  "
              f"followups={len(e.get('followups') or [])}  status={e.get('status','?')}")
        if e.get("summary"):
            print(f"        summary: {short(e['summary'], 88)}")
        for fu in (e.get("followups") or [])[:2]:
            print(f"        followup: {short(fu, 88)}")

    try:
        with open(MEM_RECALL_STATS) as fh:
            recall = json.load(fh)
    except (OSError, ValueError):
        recall = None
    if recall:
        print("  recall audit: "
              f"reads={recall.get('reads_audited', 0)}  "
              f"miss={float(recall.get('miss_rate', recall.get('important_miss_rate', 0)) or 0):.1%}  "
              f"overreach={float(recall.get('overreach_rate', 0) or 0):.1%}  "
              f"no-memory={float(recall.get('no_memory_rate', 0) or 0):.1%}  "
              f"host-override={float(recall.get('model_to_host_selection_override_rate', 0) or 0):.1%}")
        print("  miss classes: "
              f"route={recall.get('route_misses', 0)}  "
              f"continuation={recall.get('continuation_misses', 0)}  "
              f"selection={recall.get('selection_misses', 0)}")


def _delta(a, b):
    try:
        d = int(a) - int(b)
        return f"{d:+d}"
    except (TypeError, ValueError):
        return "?"


def _read_update_log(limit):
    if not os.path.isdir(MEM_UPDATE_LOG):
        return []
    files = sorted(f for f in os.listdir(MEM_UPDATE_LOG) if f.endswith(".jsonl"))
    entries = []
    for name in files:
        try:
            with open(os.path.join(MEM_UPDATE_LOG, name)) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    entries.sort(key=lambda e: e.get("timestamp") or "")
    return entries[-limit:]


def _read_json(path):
    try:
        with open(path) as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _memory_run_for_id(run_id):
    """Return the terminal Memory receipt for one exact run identity."""
    if not run_id or not os.path.isdir(MEM_RUN_LOG):
        return None
    for name in sorted(os.listdir(MEM_RUN_LOG), reverse=True):
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(MEM_RUN_LOG, name)) as fh:
                terminal = None
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(row, dict)
                        and row.get("run_id") == run_id
                        and row.get("status")
                        in {"published", "failed", "degraded", "abandoned"}
                    ):
                        terminal = row
        except OSError:
            continue
        if terminal is not None:
            return terminal
    return None


def _read_text(path, limit=80_000):
    try:
        with open(path) as fh:
            value = fh.read(limit + 1)
    except OSError:
        return None
    if len(value) > limit:
        return value[:limit] + "\n[bounded by evidence helper]\n"
    return value


def _function_source(path, function_name):
    """Return one current function without importing live app code."""
    source = _read_text(path, limit=300_000)
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            return ast.get_source_segment(source, node)
    return None


def _recall_audits_for_run(run_id):
    rows = []
    if not run_id or not os.path.isdir(MEM_RECALL_AUDIT):
        return rows
    for name in sorted(os.listdir(MEM_RECALL_AUDIT)):
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(MEM_RECALL_AUDIT, name)) as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("run_id") == run_id:
                        rows.append(row)
        except OSError:
            continue
    return rows


def _applied_memory_diff(outcome):
    previous = outcome.get("previous_commit")
    commit = outcome.get("commit")
    paths = list(dict.fromkeys(
        list(outcome.get("changed_paths") or [])
        + list(outcome.get("deleted_paths") or [])
    ))
    if not previous or not commit or not paths or not os.path.isdir(MEM_REPOSITORY):
        return "(no changed memory paths to diff)"
    try:
        proc = subprocess.run(
            [
                "git", "-C", MEM_REPOSITORY, "diff", "--no-ext-diff",
                "--unified=3", str(previous), str(commit), "--", *paths,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(diff unavailable: {type(exc).__name__})"
    if proc.returncode != 0:
        return f"(diff unavailable: git rc={proc.returncode})"
    value = proc.stdout or "(changed paths produced no textual diff)"
    if len(value) > 80_000:
        return value[:80_000] + "\n[bounded by evidence helper]\n"
    return value


def section_memory_writer_packet():
    """One bounded packet for reviewing a writer outcome."""
    sub("MEMORY WRITER — latest decision-evidence packet")
    outcomes = _read_update_log(1)
    if not outcomes:
        print("  (no published Memory outcome available)")
        return
    outcome = outcomes[-1]
    run_id = outcome.get("run_id")
    audits = _recall_audits_for_run(run_id)
    current_status = _read_json(MEM_RUN_STATUS)
    status = _memory_run_for_id(run_id)
    if (
        run_id
        and status is None
        and isinstance(current_status, dict)
        and current_status.get("run_id") == run_id
    ):
        status = current_status
    skill = _read_text(MEMORY_SKILL)
    prompt_builder = _function_source(MEMORY_RUNNER, "_proposal_prompt")

    print(f"  run_id={run_id or '?'}  status={outcome.get('status','?')}  "
          f"audits={len(audits)}  changed={len(outcome.get('changed_paths') or [])}  "
          f"deleted={len(outcome.get('deleted_paths') or [])}")
    native = outcome.get("writer_self_reviews") or []
    if native:
        print("  testimony=native writer self-review captured during the run")
    else:
        print("  native self-review=unavailable; any later review is stateless evidence")
    if not run_id:
        print("  WARNING: writer outcome has no run_id; no terminal receipt was matched.")
    elif current_status and current_status.get("run_id") != run_id:
        print("  WARNING: current run-status belongs to a different run; "
              "the outcome below is the latest published writer outcome.")
    print("\nMATCHED TERMINAL RUN RECEIPT:")
    print(json.dumps(status or {}, ensure_ascii=False, indent=2, sort_keys=True))
    print("\nNATIVE WRITER SELF-REVIEWS:")
    print(json.dumps(native, ensure_ascii=False, indent=2, sort_keys=True))
    print("\nLATEST WRITER OUTCOME:")
    print(json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True))
    print("\nAPPLIED MEMORY DIFF (changed/deleted paths only):")
    print(_applied_memory_diff(outcome))
    print("\nRECALL AUDIT VERDICTS FOR THIS RUN:")
    print(json.dumps(audits, ensure_ascii=False, indent=2, sort_keys=True))
    print("\nCURRENT MEMORY GOVERNING SKILL:")
    print(skill or "(Memory skill unavailable)")
    print("\nCURRENT WRITER PROMPT BUILDER:")
    print(prompt_builder or "(writer prompt builder unavailable)")


# --------------------------------------------------------------------------- #
# Section: Reflection nightly agent
# --------------------------------------------------------------------------- #
def section_reflection(limit):
    sub("REFLECTION — nightly run agent")

    runs = []
    recent = []
    try:
        with open(REFLECTION_METRICS) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        runs.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass

    if not runs:
        print("  run metrics: (none found)")
    else:
        recent = runs[-limit:]
        ok = sum(1 for r in recent if r.get("exit_code") == 0)
        wrote = sum(1 for r in recent if r.get("brief_written"))
        print(f"  recent runs (last {len(recent)}): {ok}/{len(recent)} exit=0, "
              f"{wrote}/{len(recent)} brief written")
        for r in recent:
            started = (r.get("started_at") or "")[:16]
            print(f"    {started or '?':16}  exit={r.get('exit_code','?')}  "
                  f"{r.get('duration_seconds','?')}s  "
                  f"brief={'yes' if r.get('brief_written') else 'no'}"
                  f"{'  DRY-RUN' if r.get('dry_run') else ''}")

    # Enumerate actual metric-backed run days, including days that left no
    # artifact directory. Filenames are listed as artifacts only: they cannot
    # prove that a provider interview completed successfully.
    run_days = list(dict.fromkeys(
        str(row.get("started_at") or "")[:10]
        for row in recent
        if len(str(row.get("started_at") or "")) >= 10
    ))
    if not run_days and os.path.isdir(REFLECTION_RUNS):
        run_days = sorted(
            day for day in os.listdir(REFLECTION_RUNS)
            if os.path.isdir(os.path.join(REFLECTION_RUNS, day))
        )[-limit:]
    print(f"  artifacts for recent runs ({len(run_days)} days):")
    for day in run_days:
        directory = os.path.join(REFLECTION_RUNS, day)
        files = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
        print(f"    {day}: {', '.join(files) if files else '(empty)'}")


def section_tool_friction(hours):
    """Print the shared mechanical-friction baseline without re-scanning here."""
    sub(f"TOOL FRICTION — recurring mechanical work (last {hours}h)")
    if not os.path.isfile(TOOL_FRICTION):
        print("  (tool-friction collector not installed)")
        return
    try:
        result = subprocess.run(
            ["python3", TOOL_FRICTION, "--hours", str(hours)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  (collector unavailable: {exc})")
        return
    output = (result.stdout or result.stderr).strip()
    print(output or f"  (collector exited {result.returncode} without output)")


# --------------------------------------------------------------------------- #
# Section: Memory read-traces cross-referenced to chat titles
# --------------------------------------------------------------------------- #
def section_read_traces(con, hours, traces_n):
    sub(f"MEMORY READ-TRACES — what recall actually served (last {hours}h)")

    if not os.path.isdir(MEM_READ_TRACE):
        print("  (read-trace dir not found)")
        return

    cutoff = now_utc() - dt.timedelta(hours=hours)
    rows = []
    for name in os.listdir(MEM_READ_TRACE):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(MEM_READ_TRACE, name)) as fh:
                obj = json.load(fh)
        except (OSError, ValueError):
            continue
        at = parse_ts(obj.get("at"))
        if not at or at < cutoff:
            continue
        traversal = obj.get("traversal") if isinstance(obj.get("traversal"), dict) else {}
        decisions = traversal.get("decisions") or []
        attempts = [
            attempt
            for decision in decisions if isinstance(decision, dict)
            for attempt in (decision.get("attempts") or []) if isinstance(attempt, dict)
        ]
        failures = sum(
            1 for attempt in attempts
            if not attempt.get("skipped") and attempt.get("outcome") != "ok"
        )
        rows.append((
            at, name[:-5], len(obj.get("files") or []),
            traversal.get("elapsed_ms"), len(decisions), failures,
        ))

    rows.sort(reverse=True)
    if not rows:
        print(f"  0 read-traces in the last {hours}h.")
        return

    titles = chat_titles(con, [cid for _, cid, *_ in rows])
    chat_attributed = sum(1 for _, cid, *_ in rows if cid in titles)
    print(f"  {len(rows)} read-traces in window "
          f"({chat_attributed} map to a chat, {len(rows) - chat_attributed} app/acceptance):")
    for at, cid, nfiles, elapsed_ms, decisions, failures in rows[:traces_n]:
        title = titles.get(cid)
        label = short(title, 52) if title else f"(non-chat: {cid[:12]})"
        latency = f"{elapsed_ms / 1000:.1f}s" if isinstance(elapsed_ms, (int, float)) else "?s"
        print(f"    {at.strftime('%Y-%m-%d %H:%M')}  {label:52}  "
              f"{nfiles} notes  {decisions} decisions  {latency}  "
              f"provider_failures={failures}")


# --------------------------------------------------------------------------- #
# Section: platform-wide skill reads
# --------------------------------------------------------------------------- #
def section_skill_loads(hours):
    sub(f"SKILL READS — platform-wide (last {hours}h, /api/admin/activity/skills)")

    since = (now_utc() - dt.timedelta(hours=hours)).isoformat()
    data = api_json(
        f"/api/admin/activity/skills?since={urllib.parse.quote(since)}"
    )
    if data is None:
        print("  (skills API unavailable)")
        return

    skills = data.get("skills") if isinstance(data, dict) else None
    if not skills:
        print("  (no skill reads recorded)")
        return
    for row in skills:
        modern = any(
            key in row
            for key in ("complete", "partial", "failed", "unverified", "unknown")
        )
        complete = row.get("complete", 0)
        partial = row.get("partial", 0)
        failed = row.get("failed", 0)
        unverified = row.get("unverified", 0)
        legacy = row.get("unknown", 0 if modern else row.get("count", 0))
        print(
            f"    {short(row.get('skill','?'), 28):28}  "
            f"total={row.get('count','?')} complete={complete} "
            f"partial={partial} failed={failed} "
            f"unverified={unverified} legacy={legacy}"
        )


# --------------------------------------------------------------------------- #
# Section: optional focus chat
# --------------------------------------------------------------------------- #
def section_focus_chat(con, chat_id):
    sub(f"FOCUS CHAT — {chat_id}")
    if not con:
        print("  (chat DB unavailable)")
        return
    try:
        row = con.execute(
            "select c.title, c.provider, c.created_at, c.updated_at, "
            "(select r.status from chat_runs r where r.chat_id=c.id "
            "order by r.started_at desc, r.id desc limit 1), "
            "coalesce(c.session_id,''), c.messages from chats c where c.id=?",
            (chat_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        print(f"  (query failed: {exc})")
        return
    if not row:
        print("  (no such chat)")
        return
    title, provider, created, updated, run_state, session, messages = row
    try:
        nmsg = len(json.loads(messages)) if messages else 0
    except (ValueError, TypeError):
        nmsg = "?"
    print(f"  title:    {title}")
    print(f"  provider: {provider or 'claude'}   messages: {nmsg}   run: {run_state or '-'}")
    print(f"  created:  {created}   updated: {updated}")
    print(f"  session:  {session or '(none)'}")

    trace_path = os.path.join(MEM_READ_TRACE, f"{chat_id}.json")
    if os.path.exists(trace_path):
        try:
            obj = json.load(open(trace_path))
            print(f"  memory recall: {len(obj.get('files') or [])} notes served, "
                  f"last at {obj.get('at','?')}")
        except (OSError, ValueError):
            print("  memory recall: trace present but unreadable")
    else:
        print("  memory recall: no read-trace for this chat")

    fork = "fork-chat.sh" if not session else "fork-session.sh"
    target = chat_id if fork == "fork-chat.sh" else f"{provider or 'claude'} {session} <cwd>"
    print(f"  coach with: /data/apps/reflection/{fork} "
          f"{target} \"<coaching-prompt>\"")


def main():
    ap = argparse.ArgumentParser(
        description="One-call evidence bundle for Reflection and agent coaching.")
    ap.add_argument("--hours", type=int, default=72,
                    help="lookback window for read-traces and skill reads (default 72)")
    ap.add_argument("--limit", type=int, default=5,
                    help="how many recent memory/reflection runs to list (default 5)")
    ap.add_argument("--traces", type=int, default=12,
                    help="how many recent read-traces to list (default 12)")
    ap.add_argument("--memory-writer-packet", action="store_true",
                    help="append a bounded review of the latest Memory writer outcome")
    ap.add_argument("chat_id", nargs="?", default=None,
                    help="optional chat id to profile as a focus block")
    args = ap.parse_args()

    con = open_db()
    header(f" REFLECTION EVIDENCE BUNDLE   generated {now_utc().strftime('%Y-%m-%d %H:%M UTC')}"
           f"   window: last {args.hours}h")

    section_memory(args.limit)
    if args.memory_writer_packet:
        section_memory_writer_packet()
    section_reflection(args.limit)
    section_tool_friction(args.hours)
    section_read_traces(con, args.hours, args.traces)
    section_skill_loads(args.hours)
    if args.chat_id:
        section_focus_chat(con, args.chat_id)

    if con:
        con.close()
    print()


if __name__ == "__main__":
    main()
