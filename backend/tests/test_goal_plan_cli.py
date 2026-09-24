"""The Goal CLI distinguishes inspecting work from recording its completion."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def cli(monkeypatch):
  path = Path(__file__).resolve().parents[1] / "scripts" / "goal_plan.py"
  spec = importlib.util.spec_from_file_location("goal_plan_cli", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, "_settings", lambda: ("unused", "unused", "chat"))
  return module


def invoke(cli, monkeypatch, args, payload):
  calls = []

  def request(method, path, body=None):
    calls.append((method, path, body))
    assert method == "GET", "Inspection must never change Goal state"
    return payload

  monkeypatch.setattr(cli, "_request", request)
  monkeypatch.setattr(sys, "argv", ["goal_plan.py", *args])
  return calls


def snapshot(status="open", plan=None):
  return {"goal": {"id": "goal", "status": status, "revision": 7}, "plan": plan}


SETTLED = {
  "tasks": [{"id": "verify", "title": "Verify", "status": "completed"}],
  "summary": {"completion_blockers": [], "can_complete": True},
}


@pytest.mark.parametrize("status", ["open", "completed", "stopped"])
@pytest.mark.parametrize("named", [False, True])
def test_show_preserves_goal_lifecycle_and_plan(cli, monkeypatch, capsys, status, named):
  payload = snapshot(status, SETTLED)
  args = ["show", "--goal-id", "goal"] if named else ["show"]
  calls = invoke(cli, monkeypatch, args, payload)
  assert cli.main() == 0
  assert json.loads(capsys.readouterr().out) == payload
  suffix = "?goal_id=goal" if named else ""
  assert calls == [("GET", f"/api/chats/chat/goal-plan{suffix}", None)]


@pytest.mark.parametrize("plan", [None, SETTLED])
def test_diagnostic_does_not_mistake_settled_tasks_for_closed_goal(
  cli, monkeypatch, capsys, plan,
):
  invoke(cli, monkeypatch, ["check-complete"], snapshot(plan=plan))
  assert cli.main() == 0
  output = capsys.readouterr().out
  assert "Goal status: open (read-only; unchanged)." in output
  assert "The Goal is still open" in output
  assert "complete --result" in output
  assert "completion is allowed" not in output


def test_diagnostic_does_not_invite_recompletion_of_closed_goal(cli, monkeypatch, capsys):
  invoke(cli, monkeypatch, ["check-complete"], snapshot("completed", SETTLED))
  assert cli.main() == 0
  output = capsys.readouterr().out
  assert "Goal status: completed" in output
  assert "complete --result" not in output


def test_diagnostic_does_not_claim_absent_goal_is_ready(cli, monkeypatch):
  invoke(cli, monkeypatch, ["check-complete"], {"goal": None, "plan": None})
  with pytest.raises(SystemExit, match="No Goal record to check"):
    cli.main()


def test_diagnostic_reports_task_and_delegation_blockers(cli, monkeypatch):
  plan = {
    "tasks": [{"id": "audit", "title": "Verify release", "status": "pending"}],
    "summary": {"completion_blockers": ["audit", "helper"]},
  }
  invoke(cli, monkeypatch, ["check-complete"], snapshot(plan=plan))
  with pytest.raises(SystemExit, match="Verify release, helper"):
    cli.main()


def test_completion_uses_existing_authority_without_a_separate_preflight(
  cli, monkeypatch, capsys,
):
  calls = []
  receipt = {"goal_id": "goal", "status": "completed", "revision": 8}

  def request(method, path, body=None):
    calls.append((method, path, body))
    if method == "GET":
      return snapshot(plan=SETTLED)
    if method == "POST":
      return {"state": "already_active"}
    return receipt

  monkeypatch.setattr(cli, "_request", request)
  monkeypatch.setattr(sys, "argv", ["goal_plan.py", "complete", "--result", "Verified release"])
  assert cli.main() == 0
  assert json.loads(capsys.readouterr().out) == receipt
  assert calls == [
    ("GET", "/api/chats/chat/goal-plan", None),
    ("POST", "/api/chats/chat/goal/resume", {"goal_id": "goal"}),
    ("GET", "/api/chats/chat/goal-plan", None),
    ("PATCH", "/api/chats/chat/goal", {
      "goal_id": "goal", "expected_revision": 7, "result": "Verified release",
    }),
  ]


def record_writes(cli, monkeypatch, args):
  """Run a write command against a fake API whose revision counts writes."""
  state = {"revision": 3}
  writes = []

  def request(method, path, body=None):
    if method == "GET":
      return snapshot(plan={"revision": state["revision"]}) | {
        "goal": {"id": "goal", "status": "open", "revision": state["revision"]},
      }
    if path.endswith("/goal/resume"):
      return {}
    writes.append((method, path.rsplit("/", 1)[1], body))
    state["revision"] += 1
    if path.endswith("/goal"):
      return {"goal_id": "goal", "revision": state["revision"]}
    return {"plan": {"revision": state["revision"], "summary": {"running": ["b"]}}}

  monkeypatch.setattr(cli, "_request", request)
  monkeypatch.setattr(sys, "argv", ["goal_plan.py", *args])
  assert cli.main() == 0
  return writes


def test_one_update_finishes_a_task_starts_the_next_and_leaves_a_handoff(
  cli, monkeypatch, capsys,
):
  writes = record_writes(cli, monkeypatch, [
    "update", "a", "--status", "completed", "--start", "b",
    "--next-action", "Build b",
  ])
  assert [(target, body["expected_revision"]) for _, target, body in writes] == [
    ("a", 3), ("b", 4), ("goal", 5),
  ]
  assert writes[1][2]["status"] == "running"
  assert writes[2][2]["next_action"] == "Build b"
  assert "Goal plan revision 6" in capsys.readouterr().out


def test_checkpoint_needs_only_the_next_action(cli, monkeypatch):
  [(method, target, body)] = record_writes(
    cli, monkeypatch, ["checkpoint", "--next-action", "Finish a"],
  )
  assert (method, target) == ("PATCH", "goal")
  assert body["next_action"] == "Finish a" and body["checkpoint"]


@pytest.mark.parametrize(("status", "detail", "remedy"), [
  (409, {"code": "no_active_goal",
         "message": "This chat has no active Goal to plan."},
   r"no active Goal to plan\. Promote first, or run `list` then `resume ID`"),
  (422, {"code": "progress_incomplete", "task_id": "t", "current": 1,
         "total": 2, "message": "t cannot complete at 1/2 progress"},
   r"at 1/2 progress\. .*update t --progress 2/2 --status completed"),
  (422, {"code": "invalid_plan", "message": "duplicate task id: t"},
   r"\(422\): duplicate task id: t$"),
  (409, "goal plan changed; fetch it and retry", r"\(409\): goal plan changed"),
])
def test_typed_refusals_name_this_helpers_own_remedy(
  cli, monkeypatch, status, detail, remedy,
):
  import io
  from urllib.error import HTTPError

  def refuse(request, timeout):
    body = io.BytesIO(json.dumps({"detail": detail}).encode())
    raise HTTPError(request.full_url, status, "Refused", {}, body)

  monkeypatch.setattr(cli, "_settings", lambda: ("http://mobius.test", "token", "chat"))
  monkeypatch.setattr(cli, "urlopen", refuse)
  with pytest.raises(SystemExit, match=remedy):
    cli._request("GET", "/api/chats/chat/goal-plan")


def test_writing_without_a_goal_names_the_way_back(cli, monkeypatch):
  monkeypatch.setattr(cli, "_request", lambda method, path, body=None: {"goal": None, "plan": None})
  monkeypatch.setattr(sys, "argv", ["goal_plan.py", "update", "a", "--status", "completed"])
  with pytest.raises(SystemExit, match=r"Promote first, or run `list` then `resume ID`"):
    cli.main()
