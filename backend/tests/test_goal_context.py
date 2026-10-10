"""Hierarchy projection preserves scope without injecting descendant trees."""
import copy
import json
from types import SimpleNamespace

import pytest

from app.goal_context import project_goal


def goal(tasks):
  return SimpleNamespace(id="goal", revision=4, objective="Entire approved outcome",
                         status="open", checkpoint="Verified progress", next_action="Continue branch",
                         plan_json={"tasks": tasks})


def task(key, parent=None, status="pending", **kwargs):
  return {"id": key, "title": key + " title", "status": status,
          "depends_on": [], **({"parent_id": parent} if parent else {}), **kwargs}


def test_leaf_focus_keeps_ancestor_contracts_siblings_and_cross_branch_dependency():
  g = goal([task("audit", status="running", completion_condition="Verify ALL branches"),
            task("build", "audit", "running", note="Preserve user data"),
            task("leaf", "build", "running", depends_on=["external"]),
            task("sibling", "build"), task("other", "audit"),
            task("hidden", "other", note="DO_NOT_INJECT_DESCENDANT"),
            task("external", status="completed", result="Dependency evidence")])
  original = copy.deepcopy(g.plan_json)
  view = project_goal(g)
  assert view["focus"] == "leaf"
  assert [t["id"] for t in view["ancestors"]] == ["audit", "build"]
  assert view["ancestors"][0]["completion_condition"] == "Verify ALL branches"
  assert view["ancestors"][1]["note"] == "Preserve user data"
  # A sibling that is also a prerequisite is listed once, with its evidence.
  assert {t["id"] for t in view["siblings"]} == {"other", "sibling"}
  assert view["dependencies"][0]["id"] == "external"
  assert view["dependencies"][0]["result"] == "Dependency evidence"
  assert "DO_NOT_INJECT_DESCENDANT" not in json.dumps(view)
  assert view["totals"]["pending"] == 3
  assert g.plan_json == original


def test_multiple_running_branches_focus_their_shared_parent():
  g = goal([task("parent", status="running"), task("a", "parent", "running"),
            task("b", "parent", "running"), task("hidden", "a")])
  view = project_goal(g)
  assert view["focus"] == "parent"
  assert {t["id"] for t in view["children"]} == {"a", "b"}
  assert "hidden" not in json.dumps(view)


def test_explicit_navigation_and_parent_verification_evidence():
  g = goal([task("parent"), task("done", "parent", "completed", result="Verified child"),
            task("blocked", "parent", "blocked", note="Need external permission")])
  view = project_goal(g, "parent")
  # A settled child is a flag; expanding it returns the evidence in full.
  assert view["children"][0]["has_result"] is True and "result" not in view["children"][0]
  assert project_goal(g, "done")["task"]["result"] == "Verified child"
  assert view["children"][1]["note"] == "Need external permission"
  with pytest.raises(ValueError):
    project_goal(g, "missing")


def test_unplanned_goal_retains_coordinator_checkpoint_without_leaking_to_helper():
  view = project_goal(goal([]))
  assert view["objective"] == "Entire approved outcome"
  assert view["checkpoint"] == "Verified progress"
  assert "checkpoint" not in project_goal(goal([]), role="helper")
  assert view["focus"] is None


def test_descendant_growth_does_not_grow_root_injection():
  small = goal([task("branch"), task("one", "branch")])
  large = goal([task("branch")] + [task(f"leaf-{i}", "branch", note="long detail "*30) for i in range(200)])
  a = json.dumps(project_goal(small)); b = json.dumps(project_goal(large))
  assert len(b) < len(a) + 10  # count digits only, not arbitrary truncation
  assert len(b) < len(json.dumps(large.plan_json)) / 20
  assert project_goal(large, "leaf-199")["task"]["note"] == "long detail "*30


def test_scoped_projection_is_read_only_and_keeps_full_plan_available(client, auth, db, chat):
  from app import models
  from app.goals import scoped_goal_context
  from tests.goal_fixtures import goal_run
  tasks = [task("root"), task("child", "root", note="CHILD_DETAILS")]
  db.add(goal_run(db, id="context-run", chat_id=chat.id, status="running",
                  goal_id="context-goal", goal_objective="Entire outcome",
                  goal_plan_json={"tasks":tasks}, goal_plan_revision=5))
  db.commit()
  goal = db.get(models.ChatGoal, "context-goal")
  overview = scoped_goal_context(db, goal)
  assert "CHILD_DETAILS" not in json.dumps(overview)
  branch = scoped_goal_context(db, goal, "child")
  assert branch["task"]["note"] == "CHILD_DETAILS"
  with pytest.raises(ValueError):
    scoped_goal_context(db, goal, "missing")
  # The scoped projection never touches the saved plan; the full plan endpoint
  # still serves every task.
  full = client.get(f"/api/chats/{chat.id}/goal-plan", headers=auth).json()["plan"]
  assert full["revision"] == 5 and len(full["tasks"]) == 2
  db.expire_all()
  assert db.get(models.ChatGoal, "context-goal").plan_json == {"tasks":tasks}
