#!/usr/bin/env python3
"""Inspect, plan, and explicitly complete the current Möbius Goal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _settings() -> tuple[str, str, str]:
  base = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  chat_id = os.environ.get("CHAT_ID") or ""
  missing = [
    name for name, value in (
      ("API_BASE_URL", base), ("AGENT_TOKEN", token), ("CHAT_ID", chat_id),
    ) if not value
  ]
  if missing:
    raise SystemExit(f"missing environment: {', '.join(missing)}")
  return base, token, chat_id


class RequestFailed(SystemExit):
  """An API refusal; a SystemExit so unhandled failures still print and exit."""

  def __init__(self, detail: str, code: int | None = None):
    prefix = f"goal-plan request failed ({code})" if code else "goal-plan request failed"
    hint = _HINTS.get(detail)
    super().__init__(f"{prefix}: {detail}" + (f" {hint}" if hint else ""))
    self.code = code
    self.detail = detail


_STALE = "goal plan changed; fetch it and retry"
_HINTS = {
  "This chat has no active Goal to plan.": (
    "Promote first (promote_goal tool), or run `goal_plan.py list` and "
    "`goal_plan.py resume ID` to attach retained work. Completed Goals "
    "cannot be replanned."
  ),
}


def _request(method: str, path: str, body=None):
  base, token, _ = _settings()
  data = None if body is None else json.dumps(body).encode("utf-8")
  request = Request(
    f"{base}{path}", data=data, method=method,
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urlopen(request, timeout=30) as response:
      raw = response.read()
      return json.loads(raw) if raw else None
  except HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
      detail = json.loads(raw).get("detail", raw)
    except (json.JSONDecodeError, AttributeError):
      detail = raw
    raise RequestFailed(str(detail), exc.code) from exc
  except URLError as exc:
    raise RequestFailed(str(exc.reason)) from exc


def _attach_for_write(chat_id: str) -> dict:
  """Bind this attempt to the exact presented Goal before changing it."""
  payload = _request("GET", f"/api/chats/{chat_id}/goal-plan")
  goal = (payload or {}).get("goal") if isinstance(payload, dict) else None
  goal_id = goal.get("id") if isinstance(goal, dict) else None
  if not goal_id:
    raise SystemExit("No Goal record; resume the original Goal before updating it.")
  _request(
    "POST", f"/api/chats/{chat_id}/goal/resume", {"goal_id": goal_id},
  )
  refreshed = _request("GET", f"/api/chats/{chat_id}/goal-plan")
  refreshed_goal = (
    (refreshed or {}).get("goal") if isinstance(refreshed, dict) else None
  )
  if not isinstance(refreshed_goal, dict) or refreshed_goal.get("id") != goal_id:
    raise SystemExit("The presented Goal changed while this attempt attached to it.")
  return refreshed


def _parse_task(value: str) -> dict:
  parts = value.split("|", 2)
  if len(parts) < 2:
    raise argparse.ArgumentTypeError(
      "task must be ID|Title or ID|Title|dependency,dependency"
    )
  task_id, title = (part.strip() for part in parts[:2])
  dependencies = []
  if len(parts) == 3 and parts[2].strip():
    dependencies = [item.strip() for item in parts[2].split(",") if item.strip()]
  return {
    "id": task_id,
    "title": title,
    "status": "pending",
    "depends_on": dependencies,
  }


def _progress(value: str) -> dict:
  try:
    current, total = (int(part) for part in value.split("/", 1))
  except (TypeError, ValueError) as exc:
    raise argparse.ArgumentTypeError("progress must look like 2/3") from exc
  return {"current": current, "total": total}


def _completion_blockers(plan: dict | None) -> list[str]:
  """Return visible unfinished-task names for the completion preflight."""
  if plan is None:
    return []
  tasks = [
    task for task in plan.get("tasks") or []
    if isinstance(task, dict)
  ]
  by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
  summary_blockers = (plan.get("summary") or {}).get("completion_blockers")
  if isinstance(summary_blockers, list):
    return [
      str(
        by_id.get(str(blocker), {}).get("title")
        or blocker
        or "Unnamed task"
      )
      for blocker in summary_blockers
    ]
  return [
    str(task.get("title") or task.get("id") or "Unnamed task")
    for task in tasks
    if task.get("status") not in {"completed", "cancelled"}
  ]


def _status_line(plan: dict | None) -> str:
  """One actionable line, so a mutation needs no follow-up show/context."""
  if not plan:
    return "No Goal plan yet."
  summary = plan.get("summary") or {}
  tasks = [task for task in plan.get("tasks") or [] if isinstance(task, dict)]
  parts = [
    f"Goal plan revision {plan.get('revision', '?')}: "
    f"{summary.get('completed', 0)}/{summary.get('total', len(tasks))} complete"
  ]
  groups = (
    ("Running", summary.get("running") or []),
    ("Ready", summary.get("ready") or []),
    ("Ready to verify", [t["id"] for t in tasks if t.get("ready_to_verify")]),
    ("Blocked", [t["id"] for t in tasks if t.get("status") == "blocked"]),
    ("Failed", [t["id"] for t in tasks if t.get("status") == "failed"]),
  )
  parts.extend(f"{label}: {', '.join(ids)}" for label, ids in groups if ids)
  blockers = summary.get("completion_blockers")
  if summary.get("can_complete"):
    parts.append("All tasks settled; after verifying the outcome run complete --result")
  elif isinstance(blockers, list) and len(parts) == 1 and blockers:
    parts.append("Waiting on: " + ", ".join(str(b) for b in blockers))
  return ". ".join(parts) + "."


def _with_fresh_revision(send, revision: int, chat_id: str):
  """Send once; on a stale-revision conflict, refetch the revision and resend.

  Every write is an absolute field assignment, so resending it on top of a
  newer revision is what the server's own conflict message asks for. Any other
  refusal (validation, lost ownership, no Goal) is surfaced unchanged.
  """
  try:
    return send(revision)
  except RequestFailed as exc:
    if exc.code != 409 or exc.detail not in {_STALE, "Goal changed; fetch it and retry"}:
      raise
  payload = _request("GET", f"/api/chats/{chat_id}/goal-plan") or {}
  goal = payload.get("goal") or {}
  return send(int(goal.get("revision", 0)))


def main() -> int:
  parser = argparse.ArgumentParser(
    prog="goal-plan",
    description="Manage $CHAT_ID's Goal. Finish verified work with complete --result.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=(
      "one call per transition:\n"
      "  set --task 'a|Inspect' --task 'b|Build|a' --start a\n"
      "  update a --status completed --result 'evidence' --start b\n"
      "  update x y --status cancelled\n"
      "  update b --status completed --next-action 'Exact next step'  (handoff)\n"
      "Plan writes already count as progress; checkpoint only before a handoff."
    ),
  )
  sub = parser.add_subparsers(dest="command", required=True)
  context_parser = sub.add_parser("context", help="inspect current focus or a named branch without the full tree")
  context_parser.add_argument("--task")
  context_parser.add_argument("--goal-id")
  show_parser = sub.add_parser("show", help="print Goal lifecycle status and the current or named plan")
  show_parser.add_argument("--goal-id")
  sub.add_parser("list", help="list retained Goal obligations in this chat")
  sub.add_parser("stop", help="honor an explicit owner Stop using the existing chat stop controller")
  resume_parser = sub.add_parser("resume", help="attach this ordinary attempt to existing unfinished work")
  resume_parser.add_argument("goal_id")
  sub.add_parser(
    "check-complete",
    help="read-only task diagnostic; does not complete the Goal",
  )
  set_parser = sub.add_parser("set", help="create or revise the complete plan")
  set_parser.add_argument(
    "--task", action="append", type=_parse_task, default=[],
    metavar="ID|Title|DEP1,DEP2",
  )
  add_parser = sub.add_parser("add", help="append a newly discovered subgoal")
  add_parser.add_argument("task_id")
  add_parser.add_argument("title")
  add_parser.add_argument("--parent")
  add_parser.add_argument("--depends-on", action="append", default=[])
  add_parser.add_argument("--completion-condition")
  set_parser.add_argument(
    "--tasks-json", help="JSON array alternative to repeated --task",
  )
  set_parser.add_argument(
    "--start", action="append", default=[], metavar="TASK_ID",
    help="then mark this task running (repeatable)",
  )
  checkpoint_parser = sub.add_parser(
    "checkpoint",
    help="record handoff notes for the next attempt (plan updates already count as progress)",
  )
  checkpoint_parser.add_argument("--next-action", required=True)
  checkpoint_parser.add_argument(
    "--summary", help="verified progress; defaults to the plan status line",
  )
  complete_parser = sub.add_parser("complete", help="validate and record the verified outcome; no preflight required")
  complete_parser.add_argument("--result", required=True)
  update_parser = sub.add_parser(
    "update",
    help="change one or more tasks, optionally start the next and checkpoint, in one call",
  )
  update_parser.add_argument("task_ids", nargs="*", metavar="TASK_ID")
  update_parser.add_argument(
    "--status",
    choices=("pending", "running", "completed", "blocked", "failed", "cancelled"),
  )
  update_parser.add_argument("--note")
  update_parser.add_argument("--result")
  update_parser.add_argument("--progress", type=_progress, metavar="CURRENT/TOTAL")
  update_parser.add_argument(
    "--start", action="append", default=[], metavar="TASK_ID",
    help="then mark this task running (repeatable)",
  )
  update_parser.add_argument("--next-action", help="then save a handoff checkpoint")
  update_parser.add_argument("--summary", help="checkpoint summary; needs --next-action")
  args = parser.parse_args()

  _, _, chat_id = _settings()
  if args.command == "context":
    from urllib.parse import urlencode
    query = urlencode({k: v for k, v in {"task": args.task, "goal_id": args.goal_id}.items() if v})
    print(json.dumps(_request("GET", f"/api/chats/{chat_id}/goal-context?{query}"), ensure_ascii=False))
    return 0
  if args.command == "stop":
    print(json.dumps(_request("POST", "/api/chat/stop", {"chat_id":chat_id})))
    return 0
  if args.command == "resume":
    print(json.dumps(_request("POST", f"/api/chats/{chat_id}/goal/resume", {"goal_id":args.goal_id})))
    return 0
  if args.command == "list":
    print(json.dumps(_request("GET", f"/api/chats/{chat_id}/goals"), indent=2))
    return 0
  if args.command in {"show", "check-complete"}:
    path = f"/api/chats/{chat_id}/goal-plan"
    if args.command == "show" and args.goal_id:
      from urllib.parse import quote
      path += f"?goal_id={quote(args.goal_id, safe='')}"
    payload = _request("GET", path)
  else:
    # Every write binds this attempt to the exact presented Goal first.
    payload = _attach_for_write(chat_id)
  if args.command == "show":
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
  goal = payload.get("goal")
  current = payload.get("plan")
  if args.command == "complete":
    if not goal:
      raise SystemExit("No Goal record; resume the original Goal before updating it.")
    body = {"goal_id": goal["id"], "expected_revision": goal["revision"], "result": args.result}
    # Never retried on a stale revision: a verified completion must not land
    # over plan changes it did not see.
    print(json.dumps(_request("PATCH", f"/api/chats/{chat_id}/goal", body)))
    return 0
  if args.command == "check-complete":
    if goal is None:
      raise SystemExit("No Goal record to check.")
    print(f"Goal status: {goal['status']} (read-only; unchanged).")
    blockers = _completion_blockers(current)
    if blockers:
      raise SystemExit(
        "Unfinished tasks or delegations: " + ", ".join(blockers)
      )
    print("No todo plan to check." if current is None else "All required tasks are settled.")
    if goal["status"] == "open":
      print(
        'The Goal is still open. After verifying the outcome, run '
        'goal_plan.py complete --result "Verified outcome". '
        'That command validates completion and records it.'
      )
    return 0

  # Plan and Goal share one revision counter; a checkpointed Goal without a
  # plan yet is already past revision 0.
  revision = int((current or goal or {}).get("revision", 0))
  steps: list[tuple[str, str, dict]] = []
  applied: list[str] = []
  next_action = getattr(args, "next_action", None)
  if args.command == "set":
    if args.tasks_json and args.task:
      parser.error("use either --tasks-json or --task, not both")
    if args.tasks_json:
      try:
        tasks = json.loads(args.tasks_json)
      except json.JSONDecodeError as exc:
        parser.error(f"invalid --tasks-json: {exc}")
    else:
      tasks = args.task
    if not tasks:
      parser.error("provide at least one --task or --tasks-json")
    # A whole-plan replacement is never resent over a newer revision.
    result = _request(
      "PUT", f"/api/chats/{chat_id}/goal-plan",
      {"expected_revision": revision, "tasks": tasks},
    )
    current = (result or {}).get("plan")
    revision = int((current or {}).get("revision", revision))
    if not args.start:
      print(_status_line(current))
      return 0
    applied.append("plan saved")
  elif args.command == "add":
    added = {
      "id": args.task_id,
      "title": args.title,
      "status": "pending",
      "depends_on": args.depends_on,
    }
    if args.parent:
      added["parent_id"] = args.parent
    if args.completion_condition:
      added["completion_condition"] = args.completion_condition
    result = _with_fresh_revision(lambda rev: _request(
      "POST", f"/api/chats/{chat_id}/goal-plan/tasks",
      {"expected_revision": rev, "task": added},
    ), revision, chat_id)
    print(_status_line((result or {}).get("plan")))
    return 0

  # set --start / update / checkpoint: an ordered list of writes, each on the
  # revision the previous write returned. Stop at the first refusal and say
  # what landed.
  if args.command == "update":
    changes = {
      key: value for key, value in (
        ("status", args.status), ("note", args.note),
        ("result", args.result), ("progress", args.progress),
      ) if value is not None
    }
    if args.task_ids and not changes:
      parser.error("update TASK_ID needs --status, --note, --result, or --progress")
    if changes and not args.task_ids:
      parser.error("--status/--note/--result/--progress need a TASK_ID")
    if not (args.task_ids or args.start or next_action):
      parser.error("update needs a TASK_ID, --start, or --next-action")
    if args.summary is not None and next_action is None:
      parser.error("--summary is the checkpoint summary; add --next-action")
    for task_id in args.task_ids:
      steps.append((f"{task_id} {args.status or 'updated'}", task_id, changes))
  for task_id in getattr(args, "start", []):
    steps.append((f"{task_id} running", task_id, {"status": "running"}))
  if next_action is not None and not goal:
    raise SystemExit("No Goal record; resume the original Goal before updating it.")

  plan = current
  receipt = None
  for label, task_id, change in steps:
    def patch_task(rev, task_id=task_id, change=change):
      return _request(
        "PATCH", f"/api/chats/{chat_id}/goal-plan/tasks/{task_id}",
        {"expected_revision": rev, **change},
      )
    try:
      result = _with_fresh_revision(patch_task, revision, chat_id)
    except RequestFailed as exc:
      if applied:
        print("Applied: " + "; ".join(applied))
        print(_status_line(plan))
      print(f"Not applied: {label} and anything after it.")
      raise exc
    plan = (result or {}).get("plan") or plan
    revision = int((plan or {}).get("revision", revision))
    applied.append(label)
  if next_action is not None:
    summary = args.summary or _status_line(plan)
    try:
      receipt = _with_fresh_revision(lambda rev: _request(
        "PATCH", f"/api/chats/{chat_id}/goal",
        {"goal_id": goal["id"], "expected_revision": rev,
         "checkpoint": summary, "next_action": next_action},
      ), revision, chat_id)
    except RequestFailed:
      if applied:
        print("Applied: " + "; ".join(applied))
        print(_status_line(plan))
      raise
    if plan is not None:
      plan = {**plan, "revision": (receipt or {}).get("revision", revision)}
  line = _status_line(plan) if plan is not None else (
    f"Goal revision {(receipt or {}).get('revision', '?')}: no plan."
  )
  if next_action is not None:
    line += f" Checkpoint saved; next: {next_action}"
  print(line)
  return 0


if __name__ == "__main__":
  sys.exit(main())
