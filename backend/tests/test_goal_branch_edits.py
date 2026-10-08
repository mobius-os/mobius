"""Helpers grow one shared work tree without approval or wake round trips."""
import pytest

from app import models
from app.goal_plans import GoalAssignment, GoalPlanError, stage_helper_task_edits
from tests.test_goal_distributed_scenarios import (
  _goal_parent, _spawned, _helper_run, _helper_headers, _spawn, _brief,
  started,  # provider-free helper admission fixture
)


@pytest.fixture
def branch(client, owner_token, db, started):
  owner = _goal_parent(client, owner_token, db)
  row = _spawned(client, owner_token, db, owner, "builder", "backend")
  run = _helper_run(db, row, "builder-run")
  return owner, row, _helper_headers(db, row, run)


def patch(client, branch, tasks, **extra):
  _, row, headers = branch
  return client.post(f"/api/chats/{row.child_chat_id}/goal/tasks",
                     json={"tasks": tasks, **extra}, headers=headers)


def test_helper_adds_then_delegates_discovered_substep_without_parent_approval(
  client, db, branch, monkeypatch,
):
  events = []
  monkeypatch.setattr("app.goal_plans.publish_goal_changed", events.append)
  response = patch(client, branch, [
    {"id": "backend-check", "title": "Check the discovered edge", "parent_id": "backend"},
    {"id": "backend", "note": "Found one additional edge"},
  ])
  assert response.status_code == 200, response.text
  body = response.json()
  assert set(body) == {"goal_id", "revision", "tasks"}
  assert {t["id"] for t in body["tasks"]} == {"backend", "backend-check"}
  assert events == [branch[0]]  # UI invalidation, not a parent model continuation
  _, row, headers = branch
  child = _spawn(client, headers, row.child_chat_id, "edge-worker", "backend-check")
  assert child.status_code == 201, child.text
  grandchild = db.get(models.Delegation, child.json()["id"])
  run = _helper_run(db, grandchild, "edge-run")
  view = _brief(client, db, grandchild, run)["goal"]
  assert view["focus"] == "backend-check"
  assert view["assignment"]["depth"] == 2


@pytest.mark.parametrize("edit", [
  {"id": "frontend", "note": "Foreign write"},
  {"id": "release", "note": "Ancestor write"},
  {"id": "orphan", "title": "Outside"},
  {"id": "escape", "title": "Outside", "parent_id": "frontend"},
  {"id": "backend-api", "parent_id": "frontend"},
  {"id": "frontend", "parent_id": "backend"},
  {"id": "backend", "title": "Different assignment"},
  {"id": "backend", "completion_condition": "Do less"},
  {"id": "backend", "depends_on": []},
  {"id": "backend", "status": "cancelled"},
])
def test_foreign_or_assignment_contract_edit_is_atomic_refusal(client, db, branch, edit):
  goal = db.get(models.ChatGoal, "release-goal")
  before = (goal.revision, goal.plan_json)
  response = patch(client, branch, [
    {"id": "backend-api", "note": "Must also roll back"}, edit,
  ])
  assert response.status_code == 422, response.text
  db.refresh(goal)
  assert (goal.revision, goal.plan_json) == before


def test_helper_can_verify_internal_parent_but_not_its_assignment(client, db, branch):
  response = patch(client, branch, [
    {"id": "backend-api", "status": "running"},
    {"id": "backend-route", "title": "Route", "parent_id": "backend-api", "status": "completed"},
  ])
  assert response.status_code == 200, response.text
  response = patch(client, branch, [{"id": "backend-api", "status": "completed"}])
  assert response.status_code == 200, response.text
  response = patch(client, branch, [
    {"id": "backend-db", "status": "completed"},
    {"id": "backend", "status": "completed"},
  ])
  assert response.status_code == 422
  assert "assigning parent" in response.text


@pytest.mark.parametrize("extra", [
  {"complete": True}, {"cancel": "Stop"}, {"defer": "Later"},
  {"goal_id": "foreign"}, {"next_action": "New outcome"},
  {"finished_claims": ["foreign"]},
])
def test_task_tool_has_no_goal_outcome_or_attachment_authority(client, branch, extra):
  assert patch(client, branch, [{"id": "backend-api", "note": "x"}], **extra).status_code == 422


def test_disjoint_helper_patches_merge_and_retry_is_a_noop(client, owner_token, db, branch, started):
  first = patch(client, branch, [{"id": "backend-api", "note": "Backend evidence"}])
  other = _spawned(client, owner_token, db, branch[0], "designer", "frontend")
  run = _helper_run(db, other, "designer-run")
  other_branch = (branch[0], other, _helper_headers(db, other, run))
  second = patch(client, other_branch, [
    {"id": "frontend-test", "title": "Test layout", "parent_id": "frontend"},
  ])
  assert second.status_code == 200, second.text
  retry = patch(client, branch, [{"id": "backend-api", "note": "Backend evidence"}])
  assert retry.status_code == 200
  assert retry.json()["revision"] == second.json()["revision"] == first.json()["revision"] + 1
  db.expire_all()
  tasks = {t["id"]: t for t in db.get(models.ChatGoal, "release-goal").plan_json["tasks"]}
  assert tasks["backend-api"]["note"] == "Backend evidence"
  assert tasks["frontend-test"]["parent_id"] == "frontend"


@pytest.mark.parametrize("status", ["stopped", "completed", "cancelled"])
def test_helper_cannot_resume_or_change_settled_goal(client, db, branch, status):
  goal = db.get(models.ChatGoal, "release-goal")
  goal.status = status
  db.commit()
  response = patch(client, branch, [{"id": "backend-api", "note": "Too late"}])
  assert response.status_code == 422, response.text
  db.refresh(goal)
  assert goal.status == status


def test_unfiled_assignment_never_grants_whole_tree_writes(db):
  goal = models.ChatGoal(id="g", chat_id="c", objective="Work", status="open",
                         plan_json={"tasks": []})
  assignment = GoalAssignment(goal, "d", "helper", None, False, 1)
  with pytest.raises(GoalPlanError, match="assigned checklist"):
    stage_helper_task_edits(assignment, [{"id": "x", "title": "x"}])


def test_inherited_dependencies_and_cycles_still_protect_the_plan(client, branch):
  cycle = patch(client, branch, [{"id": "backend-api", "depends_on": ["backend"]}])
  assert cycle.status_code == 422
  premature = patch(client, branch, [
    {"id": "backend-api", "status": "completed"},
    {"id": "backend-leaf", "parent_id": "backend-api", "title": "Unfinished"},
  ])
  assert premature.status_code == 422


def test_inherited_active_helper_is_counted_against_its_assigned_work():
  from app.goal_plans import _active_helper_nodes
  nodes = [{"id": "parent", "plan_task": "branch", "status": "completed", "children": [
    {"id": "child", "plan_task": None, "status": "running", "children": []},
  ]}]
  active = _active_helper_nodes(nodes)
  assert [(node["id"], node["plan_task"]) for node in active] == [("child", "branch")]
  assert nodes[0]["children"][0]["plan_task"] is None  # no rewriting retained assignments


@pytest.mark.parametrize("interruption", ["stop", "cancel", "settled-run"])
def test_waiting_for_goal_lock_rechecks_authority(client, db, branch, monkeypatch, interruption):
  from contextlib import asynccontextmanager
  from datetime import datetime, UTC

  @asynccontextmanager
  async def changed_while_waiting(chat_id):
    assert chat_id in (branch[0], branch[1].child_chat_id)
    if interruption == "stop":
      db.get(models.ChatGoal, "release-goal").status = "stopped"
    elif interruption == "cancel":
      db.get(models.Delegation, branch[1].id).cancelled_at = datetime.now(UTC)
    else:
      db.get(models.ChatRun, "builder-run").status = "completed"
    db.commit()
    yield

  monkeypatch.setattr("app.chat_queue.get_transition_lock", changed_while_waiting)
  response = patch(client, branch, [{"id": "backend-api", "note": "Must not save"}])
  assert response.status_code in (409, 422), response.text
  db.expire_all()
  assert all(t.get("note") != "Must not save" for t in db.get(models.ChatGoal, "release-goal").plan_json["tasks"])


@pytest.mark.asyncio
async def test_helper_write_waits_for_its_inflight_stop_before_rechecking_run(db, branch):
  import asyncio
  from types import SimpleNamespace
  from fastapi import HTTPException
  from app import chat_queue
  from app.routes.goal_plans import HelperTaskUpdateRequest, update_helper_goal_tasks

  _, row, _ = branch
  principal = SimpleNamespace(chat_id=row.child_chat_id, delegation_id=row.id,
                              run_id="builder-run", scope="owner", app_id=None)
  child_lock = chat_queue.get_transition_lock(row.child_chat_id)
  async with child_lock:
    pending = asyncio.create_task(update_helper_goal_tasks(
      row.child_chat_id,
      HelperTaskUpdateRequest(tasks=[{"id": "backend-api", "note": "After Stop"}]),
      principal, db,
    ))
    await asyncio.sleep(0)  # dispatch the request while Stop owns the child lock
    try:
      assert not pending.done(), "Checklist write overtook the helper's in-flight Stop"
    finally:
      db.get(models.ChatRun, "builder-run").status = "stopped"
      db.commit()
  with pytest.raises(HTTPException) as refused:
    await pending
  assert refused.value.status_code == 409
  db.expire_all()
  assert all(t.get("note") != "After Stop" for t in db.get(models.ChatGoal, "release-goal").plan_json["tasks"])


def test_parent_acceptance_keeps_live_child_visible_and_can_reopen_work(client, db, branch):
  from app.goal_plans import active_goal_helpers

  _, row, headers = branch
  child_response = _spawn(client, headers, row.child_chat_id, "api-review", "backend-api")
  assert child_response.status_code == 201, child_response.text
  child = db.get(models.Delegation, child_response.json()["id"])
  run = _helper_run(db, child, "api-review-run")
  child_branch = (branch[0], child, _helper_headers(db, child, run))
  accepted = patch(client, branch, [{"id": "backend-api", "status": "completed"}])
  assert accepted.status_code == 200, accepted.text
  late = patch(client, child_branch, [{"id": "backend-api", "note": "Late work"}])
  assert late.status_code == 422, late.text
  db.expire_all()
  assert "api-review" in active_goal_helpers(
    db, db.get(models.ChatRun, "coord-run"), db.get(models.ChatGoal, "release-goal"),
  )
  reopened = patch(client, branch, [{"id": "backend-api", "status": "running"}])
  assert reopened.status_code == 200, reopened.text
  resumed = patch(client, child_branch, [{"id": "backend-api", "note": "Requested follow-up"}])
  assert resumed.status_code == 200, resumed.text
