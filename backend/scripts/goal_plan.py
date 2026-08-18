#!/usr/bin/env python3
"""Publish and update the visible todo plan for the current Möbius Goal."""

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
    raise SystemExit(f"goal-plan request failed ({exc.code}): {detail}") from exc
  except URLError as exc:
    raise SystemExit(f"goal-plan request failed: {exc.reason}") from exc


def _current(chat_id: str):
  payload = _request("GET", f"/api/chats/{chat_id}/goal-plan")
  return payload.get("plan") if isinstance(payload, dict) else None


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
  return [
    str(task.get("title") or task.get("id") or "Unnamed task")
    for task in tasks
    if task.get("status") not in {"completed", "cancelled"}
  ]


def main() -> int:
  parser = argparse.ArgumentParser(
    prog="goal-plan",
    description="Manage the visible todo plan for $CHAT_ID's active Goal.",
  )
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("show", help="print the current plan")
  sub.add_parser(
    "check-complete",
    help="verify that every required task is completed or cancelled",
  )
  set_parser = sub.add_parser("set", help="create or revise the complete plan")
  set_parser.add_argument(
    "--task", action="append", type=_parse_task, default=[],
    metavar="ID|Title|DEP1,DEP2",
  )
  set_parser.add_argument(
    "--tasks-json", help="JSON array alternative to repeated --task",
  )
  update_parser = sub.add_parser("update", help="advance one task")
  update_parser.add_argument("task_id")
  update_parser.add_argument(
    "--status",
    choices=("pending", "running", "completed", "blocked", "failed", "cancelled"),
  )
  update_parser.add_argument("--note")
  update_parser.add_argument("--progress", type=_progress, metavar="CURRENT/TOTAL")
  args = parser.parse_args()

  _, _, chat_id = _settings()
  current = _current(chat_id)
  if args.command == "show":
    print(json.dumps(current, indent=2, ensure_ascii=False))
    return 0
  if args.command == "check-complete":
    # One-step Goals deliberately have no plan and may complete normally.
    if current is None:
      print("Goal has no todo plan; completion is allowed.")
      return 0
    blockers = _completion_blockers(current)
    if blockers:
      raise SystemExit(
        "Goal cannot complete; unfinished todo tasks: " + ", ".join(blockers)
      )
    print("Goal todo list is complete.")
    return 0

  revision = int((current or {}).get("revision", 0))
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
    result = _request(
      "PUT", f"/api/chats/{chat_id}/goal-plan",
      {"expected_revision": revision, "tasks": tasks},
    )
  else:
    changes = {"expected_revision": revision}
    if args.status is not None:
      changes["status"] = args.status
    if args.note is not None:
      changes["note"] = args.note
    if args.progress is not None:
      changes["progress"] = args.progress
    if len(changes) == 1:
      parser.error("update needs --status, --note, or --progress")
    result = _request(
      "PATCH", f"/api/chats/{chat_id}/goal-plan/tasks/{args.task_id}",
      changes,
    )
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
