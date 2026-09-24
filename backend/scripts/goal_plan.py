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
    except json.JSONDecodeError:
      detail = raw
    if detail == "This chat has no active Goal to plan.":
      detail += " Promote first, or run `list` then `resume ID`."
    raise SystemExit(f"goal-plan request failed ({exc.code}): {detail}") from exc
  except URLError as exc:
    raise SystemExit(f"goal-plan request failed: {exc.reason}") from exc


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


def main() -> int:
  parser = argparse.ArgumentParser(
    prog="goal-plan",
    description="Manage $CHAT_ID's Goal. Finish verified work with complete --result.",
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
  checkpoint_parser = sub.add_parser(
    "checkpoint", help="leave the next attempt its next step before a handoff",
  )
  checkpoint_parser.add_argument("--summary", default="Plan saved.")
  checkpoint_parser.add_argument("--next-action", required=True)
  complete_parser = sub.add_parser("complete", help="validate and record the verified outcome; no preflight required")
  complete_parser.add_argument("--result", required=True)
  update_parser = sub.add_parser("update", help="advance one task")
  update_parser.add_argument("task_id")
  update_parser.add_argument(
    "--status",
    choices=("pending", "running", "completed", "blocked", "failed", "cancelled"),
  )
  update_parser.add_argument("--note")
  update_parser.add_argument("--result")
  update_parser.add_argument("--progress", type=_progress, metavar="CURRENT/TOTAL")
  update_parser.add_argument("--start", metavar="TASK_ID", help="then mark this task running")
  update_parser.add_argument("--next-action", help="then leave a handoff checkpoint")
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
  if args.command in {"checkpoint", "complete"}:
    payload = _attach_for_write(chat_id)
    goal = (payload or {}).get("goal")
    body = {"goal_id": goal["id"], "expected_revision": goal["revision"]}
    if args.command == "complete":
      body["result"] = args.result
    else:
      body.update(checkpoint=args.summary, next_action=args.next_action)
    print(json.dumps(_request("PATCH", f"/api/chats/{chat_id}/goal", body)))
    return 0
  if args.command in {"show", "check-complete"}:
    path = f"/api/chats/{chat_id}/goal-plan"
    if args.command == "show" and args.goal_id:
      from urllib.parse import quote
      path += f"?goal_id={quote(args.goal_id, safe='')}"
    payload = _request("GET", path)
    if args.command == "show":
      print(json.dumps(payload, indent=2, ensure_ascii=False))
      return 0
    goal = payload.get("goal")
    current = payload.get("plan")
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
  else:
    changes = {}
    if args.status is not None:
      changes["status"] = args.status
    if args.note is not None:
      changes["note"] = args.note
    if args.result is not None:
      changes["result"] = args.result
    if args.progress is not None:
      changes["progress"] = args.progress
    if not changes:
      parser.error("update needs --status, --note, --result, or --progress")

  payload = _attach_for_write(chat_id)
  current = payload.get("plan") if isinstance(payload, dict) else None
  revision = int((current or {}).get("revision", 0))
  if args.command == "set":
    result = _request(
      "PUT", f"/api/chats/{chat_id}/goal-plan",
      {"expected_revision": revision, "tasks": tasks},
    )
  elif args.command == "add":
    result = _request(
      "POST", f"/api/chats/{chat_id}/goal-plan/tasks",
      {"expected_revision": revision, "task": added},
    )
  else:
    changes["expected_revision"] = revision
    result = _request(
      "PATCH", f"/api/chats/{chat_id}/goal-plan/tasks/{args.task_id}",
      changes,
    )
    # Each follow-up write uses the revision the previous one returned.
    if args.start:
      result = _request(
        "PATCH", f"/api/chats/{chat_id}/goal-plan/tasks/{args.start}",
        {"status": "running",
         "expected_revision": result["plan"]["revision"]},
      )
    if args.next_action:
      receipt = _request("PATCH", f"/api/chats/{chat_id}/goal", {
        "goal_id": payload["goal"]["id"],
        "expected_revision": result["plan"]["revision"],
        "checkpoint": "Plan saved.", "next_action": args.next_action,
      })
      result["plan"]["revision"] = receipt["revision"]
  plan = (result or {}).get("plan")
  summary = (plan or {}).get("summary", {})
  print(
    f"Goal plan revision {(plan or {}).get('revision', '?')}: "
    f"{summary.get('completed', 0)}/{summary.get('total', 0)} complete"
  )
  running = summary.get("running") or []
  ready = summary.get("ready") or []
  if running:
    print("Running: " + ", ".join(running))
  if ready:
    print("Ready: " + ", ".join(ready))
  return 0


if __name__ == "__main__":
  sys.exit(main())
