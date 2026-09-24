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


def test_completion_names_the_claimed_actions_it_performed(cli, monkeypatch):
  calls = []

  def request(method, path, body=None):
    calls.append(body)
    if method == "GET":
      return snapshot(plan=SETTLED)
    return {"state": "already_active"} if method == "POST" else {}

  monkeypatch.setattr(cli, "_request", request)
  monkeypatch.setattr(sys, "argv", [
    "goal_plan.py", "complete", "--result", "Merged", "--finished", "pr:1:merge",
  ])
  assert cli.main() == 0
  assert calls[-1]["finished_claims"] == ["pr:1:merge"]
