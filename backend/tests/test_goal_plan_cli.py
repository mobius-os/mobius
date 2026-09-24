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


class FakeGoalApi:
  """A minimal stateful Goal API: one shared revision, CAS on every write."""

  def __init__(self, cli, tasks=None, revision=3, plan=True):
    self.cli = cli
    self.revision = revision
    self.tasks = {t["id"]: dict(t) for t in (tasks or [])} if plan else None
    self.calls = []
    self.refuse = {}  # task id -> (detail, code) for the next PATCH
    self.race_once = False

  def plan(self):
    if self.tasks is None:
      return None
    tasks = list(self.tasks.values())
    done = {t["id"] for t in tasks if t["status"] in {"completed", "cancelled"}}
    blockers = [t["id"] for t in tasks if t["id"] not in done]
    return {
      "revision": self.revision,
      "tasks": tasks,
      "summary": {
        "completed": sum(t["status"] == "completed" for t in tasks),
        "total": len(tasks),
        "running": [t["id"] for t in tasks if t["status"] == "running"],
        "ready": [
          t["id"] for t in tasks if t["status"] == "pending"
          and set(t.get("depends_on") or []) <= done
        ],
        "can_complete": not blockers,
        "completion_blockers": blockers,
      },
    }

  def _cas(self, body, stale):
    if self.race_once:
      self.race_once = False
      self.revision += 1  # another writer landed first
    if body["expected_revision"] != self.revision:
      raise self.cli.RequestFailed(stale, 409)
    self.revision += 1

  def __call__(self, method, path, body=None):
    self.calls.append((method, path, body))
    if path.endswith("/goal/resume"):
      # Every write first attaches this attempt to the presented Goal.
      return {"state": "already_active"}
    if method == "GET":
      return {"goal": {"id": "goal", "status": "open", "revision": self.revision},
              "plan": self.plan()}
    if path.endswith("/goal"):
      self._cas(body, "Goal changed; fetch it and retry")
      return {"goal_id": "goal", "status": "open", "revision": self.revision}
    if method == "PUT":
      self._cas(body, self.cli._STALE)
      self.tasks = {t["id"]: dict(t) for t in body["tasks"]}
      return {"plan": self.plan()}
    task_id = path.rsplit("/", 1)[1]
    if task_id in self.refuse:
      raise self.cli.RequestFailed(*self.refuse.pop(task_id))
    self._cas(body, self.cli._STALE)
    self.tasks[task_id].update(
      {k: v for k, v in body.items() if k != "expected_revision"}
    )
    return {"plan": self.plan()}

  def writes(self):
    return [
      (m, p.rsplit("/", 1)[1], b) for m, p, b in self.calls
      if m != "GET" and not p.endswith("/goal/resume")
    ]


LEAVES = [
  {"id": "a", "title": "A", "status": "running", "depends_on": []},
  {"id": "b", "title": "B", "status": "pending", "depends_on": ["a"]},
  {"id": "c", "title": "C", "status": "pending", "depends_on": ["a"]},
]


def run_cli(cli, monkeypatch, api, *args):
  monkeypatch.setattr(cli, "_request", api)
  monkeypatch.setattr(sys, "argv", ["goal_plan.py", *args])
  return cli.main()


def test_finishing_a_leaf_and_starting_the_next_is_one_command(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, LEAVES)
  run_cli(cli, monkeypatch, api, "update", "a", "--status", "completed",
          "--result", "verified", "--start", "b")
  assert api.writes() == [
    ("PATCH", "a", {"expected_revision": 3, "status": "completed", "result": "verified"}),
    ("PATCH", "b", {"expected_revision": 4, "status": "running"}),
  ]
  out = capsys.readouterr().out.strip()
  assert out == "Goal plan revision 5: 1/3 complete. Running: b. Ready: c."


def test_one_status_change_applies_to_several_tasks(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, LEAVES)
  run_cli(cli, monkeypatch, api, "update", "b", "c", "--status", "cancelled")
  assert [(task, body["expected_revision"]) for _, task, body in api.writes()] == [
    ("b", 3), ("c", 4),
  ]
  assert "revision 5" in capsys.readouterr().out


def test_set_can_start_the_first_leaf_without_another_call(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, plan=False, revision=2)
  run_cli(cli, monkeypatch, api, "set", "--task", "a|A", "--task", "b|B|a",
          "--start", "a")
  writes = api.writes()
  # A checkpointed Goal without a plan is already past revision 0.
  assert writes[0][0] == "PUT" and writes[0][2]["expected_revision"] == 2
  assert writes[1] == ("PATCH", "a", {"expected_revision": 3, "status": "running"})
  assert capsys.readouterr().out.strip().endswith("0/2 complete. Running: a.")


def test_a_stale_revision_is_refetched_and_the_write_resent_once(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, LEAVES)
  api.race_once = True
  run_cli(cli, monkeypatch, api, "update", "a", "--status", "completed")
  assert [body["expected_revision"] for _, _, body in api.writes()] == [3, 4]
  assert api.tasks["a"]["status"] == "completed"
  assert "revision 5" in capsys.readouterr().out


def test_other_refusals_stop_and_report_what_was_applied(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, LEAVES)
  api.refuse["b"] = ("dependency a is not complete", 422)
  with pytest.raises(SystemExit, match="dependency a is not complete"):
    run_cli(cli, monkeypatch, api, "update", "a", "--status", "completed",
            "--start", "b", "--start", "c")
  out = capsys.readouterr().out
  assert "Applied: a completed" in out
  assert "Not applied: b running and anything after it." in out
  assert [task for _, task, _ in api.writes()] == ["a", "b"]


def test_update_can_leave_a_handoff_checkpoint_in_the_same_call(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, LEAVES)
  run_cli(cli, monkeypatch, api, "update", "a", "--status", "completed",
          "--next-action", "Build b")
  task_write, goal_write = api.writes()
  assert task_write[1] == "a"
  assert goal_write[1] == "goal"
  assert goal_write[2]["expected_revision"] == 4
  assert goal_write[2]["next_action"] == "Build b"
  assert goal_write[2]["checkpoint"].startswith("Goal plan revision 4: 1/3 complete")
  assert capsys.readouterr().out.strip().endswith("Checkpoint saved; next: Build b")


def test_checkpoint_needs_only_the_next_action(cli, monkeypatch, capsys):
  api = FakeGoalApi(cli, LEAVES)
  run_cli(cli, monkeypatch, api, "checkpoint", "--next-action", "Finish a")
  [(_, target, body)] = api.writes()
  assert target == "goal" and body["next_action"] == "Finish a" and body["checkpoint"]


def test_missing_goal_refusal_names_the_way_back(cli):
  error = cli.RequestFailed("This chat has no active Goal to plan.", 409)
  assert "promote_goal" in str(error) and "resume ID" in str(error)
