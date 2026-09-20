"""Goal obligations survive attempts; no production or provider calls."""
from datetime import UTC, datetime, timedelta
import json
import pytest
from sqlalchemy import create_engine, select
from app import models
from app.goals import admit_goal, resume_context, update_goal_record
from app.goal_plans import (
  GoalPlanConflict, GoalPlanError, goal_terminal_handoff,
  presented_goal, replace_plan, serialize_plan,
)
from app.run_state import goal_identity_for_run_start


def work(db, chat, *, attempt_status="running", task_status="running"):
  goal = models.ChatGoal(
    id="work", chat_id=chat.id, objective="Fix deployment AND collaboration",
    plan_json={"tasks":[{"id":"deploy","title":"Deploy","status":task_status,
                         "depends_on":[]}]}, revision=1,
  )
  run = models.ChatRun(id="attempt", root_run_id="attempt", chat_id=chat.id,
                      goal_id=goal.id, goal_objective=goal.objective,
                      status=attempt_status, provider="claude")
  db.add_all([goal, run]); db.commit()
  return goal, run


@pytest.mark.parametrize("status", ["failed", "interrupted", "completed", "parked"])
@pytest.mark.parametrize("message", ["continue", "continue please", "please continue"])
def test_attempt_outcome_does_not_lose_goal(db, chat, status, message):
  goal, run = work(db, chat, attempt_status=status)
  assert presented_goal(db, chat.id)["status"] == "paused"
  assert goal_identity_for_run_start(db, chat.id, {"content":message}) == (goal.objective, goal.id)
  assert goal.status == "open"


def test_resume_after_unrelated_question_and_no_provider_history(db, chat):
  goal, run = work(db, chat, attempt_status="failed")
  db.add(models.ChatRun(id="unrelated", root_run_id="unrelated", chat_id=chat.id,
                       status="completed", started_at=datetime.now(UTC)+timedelta(seconds=1)))
  db.commit()
  assert goal_identity_for_run_start(db, chat.id, {"content":"continue please"}) == (goal.objective, goal.id)
  context = resume_context(db, run.id)
  assert goal.objective in context and '"id":"deploy"' in context
  assert "resume_reason" not in context
  assert goal_identity_for_run_start(db, chat.id, {"content":"Unrelated question"}) == (None,None)


def test_completed_tasks_are_not_implicit_goal_completion(db, chat):
  goal, run = work(db, chat, task_status="completed")
  assert goal_terminal_handoff(db, chat.id, run.id) is not None
  assert presented_goal(db, chat.id)["status"] == "active"
  update_goal_record(db, run, goal, 1, result="Verified deployment and collaboration")
  assert goal.status == "completed"
  assert goal_terminal_handoff(db, chat.id, run.id) is None
  assert presented_goal(db, chat.id)["status"] == "completed"


def test_completion_cannot_race_scope_change(db, chat):
  goal, run = work(db, chat, task_status="completed")
  replace_plan(db, physical=run, root=goal, expected_revision=1, tasks=[
    {"id":"deploy","title":"Deploy","status":"completed","depends_on":[]},
    {"id":"collab","title":"Collaboration","status":"pending","depends_on":[]},
  ])
  with pytest.raises(GoalPlanError):
    update_goal_record(db, run, goal, 1, result="Stale verification")
  assert goal.status == "open"


def test_checkpoint_is_durable_and_zero_legacy_allowance_does_not_block(db, chat):
  goal, run = work(db, chat)
  update_goal_record(db, run, goal, 1, checkpoint="Implemented half", next_action="Verify peers")
  db.expire_all()
  assert goal.checkpoint == "Implemented half"
  assert goal_terminal_handoff(db, chat.id, run.id).goal_id == goal.id
  assert "Verify peers" in resume_context(db, run.id)


def test_turn_admission_has_no_allowance_or_progress_counter(db, chat):
  goal, run = work(db, chat)
  for _ in range(20):
    admit_goal(db, chat.id, goal.id, goal.objective,
               {"kind":"continuation", "continuation_reason":"goal_handoff"})
    db.commit()
    assert goal_terminal_handoff(db, chat.id, run.id) is not None
  assert "automatic_turns_remaining" not in resume_context(db, run.id)


@pytest.mark.parametrize("status", ["stopped","completed","dismissed"])
def test_stale_wakes_cannot_revive_closed_goal(db, chat, status):
  goal, run = work(db, chat)
  goal.status=status; db.commit()
  assert goal_identity_for_run_start(db, chat.id, {
    "kind":"continuation", "continuation_reason":"goal_handoff", "goal_id":goal.id,
  }) == (None,None)


@pytest.mark.parametrize("attempt_status,task_status,dismissed,expected", [
  ("failed", "running", False, "open"),
  ("completed", "pending", False, "open"),
  ("stopped", "pending", False, "stopped"),
  ("completed", "completed", False, "completed"),
  ("completed", "completed", True, "completed"),
  ("failed", "pending", True, "dismissed"),
])
def test_migration_preserves_intent_and_is_idempotent(
  tmp_path, attempt_status, task_status, dismissed, expected,
):
  from app.database import Base
  from app.schema_migrations import _durable_goal_records
  engine = create_engine(f"sqlite:///{tmp_path}/upgrade.db")
  # A frozen minimal predecessor shape: no new Goal table.
  with engine.begin() as c:
    c.exec_driver_sql('CREATE TABLE chats (id VARCHAR PRIMARY KEY, dismissed_goal_id VARCHAR)')
    c.exec_driver_sql("INSERT INTO chats VALUES (?, ?)", ("chat", "original" if dismissed else None))
  with engine.begin() as c:
    c.exec_driver_sql("""CREATE TABLE chat_runs (
      id VARCHAR PRIMARY KEY, chat_id VARCHAR, goal_id VARCHAR, goal_objective TEXT,
      status VARCHAR, goal_plan_json JSON, goal_plan_revision INTEGER,
      started_at TIMESTAMP, ended_at TIMESTAMP)""")
    c.exec_driver_sql("INSERT INTO chat_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
      "old", "chat", "original", "Entire audit", attempt_status,
      json.dumps({"tasks":[{"id":"deploy","status":task_status}]}), 7,
      "2026-09-14 12:00:00", "2026-09-14 12:01:00",
    ))
    c.exec_driver_sql("INSERT INTO chat_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
      "initial", "chat", "original", "Entire audit", "completed", "null", 0,
      "2026-09-14 11:00:00", "2026-09-14 11:01:00",
    ))
  _durable_goal_records(engine)
  _durable_goal_records(engine)
  with engine.connect() as c:
    rows=c.execute(select(models.ChatGoal.__table__)).mappings().all()
    assert len(rows)==1
    assert rows[0]["status"]==expected
    assert rows[0]["objective"]=="Entire audit"
    assert rows[0]["revision"]==7
    assert rows[0]["plan_json"]["tasks"][0]["id"]=="deploy"
  engine.dispose()


def test_earlier_unfinished_scope_remains_visible_in_resume_context(db, chat):
  goal, run = work(db, chat)
  db.add(models.ChatGoal(id="original", chat_id=chat.id, objective="Broader reliability audit"))
  db.commit()
  assert "Broader reliability audit" in resume_context(db, run.id)


def test_writer_duplicate_attempt_does_not_create_twice(db, chat):
  from app.chat_writer import StartContinuation, get_writer
  goal, run = work(db, chat, attempt_status="completed")
  cmd = StartContinuation(chat_id=chat.id, run_token="exact-next", root_run_id=run.id,
      source_work_id=run.id, content="continue", reason="manual", cid="exact-cid")
  # The concrete existing writer command fences retry identity; a resumed
  # manual admission grants once rather than manufacturing another attempt.
  first = get_writer().submit(cmd).result(timeout=5)
  db.expire_all()
  second = get_writer().submit(cmd).result(timeout=5)
  db.expire_all()
  assert db.query(models.ChatRun).filter_by(id="exact-next").count() == 1


def test_exact_completion_retry_is_idempotent(db, chat):
  goal, run = work(db, chat, task_status="completed")
  first = update_goal_record(db, run, goal, 1, result="Verified result")
  second = update_goal_record(db, run, goal, 1, result="Verified result")
  assert second == first
  assert goal.revision == 2


def test_stop_racing_stale_completion_keeps_obligation_stopped(db, chat):
  from app.chat_writer import FinishRun, get_writer
  goal, run = work(db, chat, task_status="completed")
  get_writer().submit(FinishRun(chat_id=chat.id, run_token=run.id,
                              terminal_status="stopped")).result(timeout=5)
  # Deliberately retain the stale ORM snapshot from before Stop.
  with pytest.raises(GoalPlanConflict):
    update_goal_record(db, run, goal, 1, result="Late completion")
  db.expire_all()
  assert db.get(models.ChatGoal, goal.id).status == "stopped"


def test_tokenless_stop_reaches_latest_open_goal_past_completed_goal(db, chat):
  from app.chat_writer import FinishRun, get_writer
  goal, run = work(db, chat, attempt_status="completed")
  db.add(models.ChatGoal(
    id="newer-completed", chat_id=chat.id, objective="Already finished",
    status="completed", created_at=datetime.now(UTC) + timedelta(seconds=1),
  ))
  db.commit()

  get_writer().submit(FinishRun(
    chat_id=chat.id, run_token="", terminal_status="stopped",
  )).result(timeout=5)

  db.expire_all()
  assert db.get(models.ChatGoal, goal.id).status == "stopped"
  assert db.get(models.ChatGoal, "newer-completed").status == "completed"


def test_resume_named_original_preserves_newer_branch_and_old_plan(db, chat):
  from app.chat_writer import PromoteRunToGoal, get_writer
  goal, run = work(db, chat, attempt_status="failed")
  newer = models.ChatGoal(id="narrow", chat_id=chat.id, objective="Only Kanban",
                         created_at=datetime.now(UTC)+timedelta(seconds=1))
  ordinary = models.ChatRun(id="owner-return", root_run_id="owner-return", chat_id=chat.id,
                           status="running", started_at=datetime.now(UTC)+timedelta(seconds=2))
  db.add_all([newer,ordinary]);db.commit()
  result = get_writer().submit(PromoteRunToGoal(chat_id=chat.id,run_token=ordinary.id,
      objective=goal.objective,resume_goal_id=goal.id)).result(timeout=5)
  assert result["state"] == "promoted"
  db.expire_all()
  assert ordinary.goal_id == goal.id
  assert goal.plan_json["tasks"][0]["id"] == "deploy"
  assert newer.status == "open"


def test_api_branch_add_and_completion_fence(client, db, chat):
  from app.auth import create_agent_token
  goal, run = work(db, chat)
  owner = db.query(models.Owner).first()
  headers={"Authorization":"Bearer "+create_agent_token(
    chat.id, owner.username, owner.token_epoch, run_id=run.id)}
  added=client.post(f"/api/chats/{chat.id}/goal-plan/tasks",headers=headers,json={
    "expected_revision":1,"task":{"id":"kanban","title":"Verify Kanban",
        "parent_id":"deploy","status":"pending","depends_on":[]}})
  assert added.status_code == 200,added.text
  assert [t["id"] for t in added.json()["plan"]["tasks"]] == ["deploy","kanban"]
  completion=client.patch(f"/api/chats/{chat.id}/goal",headers=headers,json={
    "goal_id":goal.id,"expected_revision":2,"result":"Unverified claim"})
  assert completion.status_code == 422,completion.text
  db.expire_all()
  assert goal.status == "open"


def test_dismissing_completed_goal_keeps_verified_outcome(db, chat):
  from app.chat_writer import ClearPresentedGoal, get_writer
  goal, run = work(db, chat, task_status="completed")
  update_goal_record(db, run, goal, 1, result="Verified entire outcome")
  receipt = get_writer().submit(ClearPresentedGoal(
    chat_id=chat.id, expected_goal_id=goal.id, preserve_execution=True,
  )).result(timeout=5)
  assert receipt["status"] == "cleared"
  db.expire_all()
  assert goal.status == "completed"
  assert goal.result == "Verified entire outcome"
  assert run.status == "running"
  assert presented_goal(db, chat.id) is None


@pytest.mark.parametrize("status", ["stopped", "completed", "dismissed"])
def test_removing_allowance_does_not_admit_closed_work(db, chat, status):
  goal, run = work(db, chat)
  goal.status = status
  db.commit()
  with pytest.raises(RuntimeError, match="Goal is closed"):
    admit_goal(db, chat.id, goal.id, goal.objective,
               {"kind":"continuation", "continuation_reason":"goal_handoff"})


def test_corrupt_plan_cannot_complete_and_full_replace_repairs_it(db, chat):
  goal, run = work(db, chat, task_status="completed")
  goal.plan_json = "not-a-plan"
  db.commit()

  with pytest.raises(GoalPlanError, match="unreadable"):
    update_goal_record(db, run, goal, 1, result="Must not silently complete")
  assert goal.status == "open"

  repaired = replace_plan(
    db, physical=run, root=goal, expected_revision=1,
    tasks=[{
      "id": "deploy", "title": "Deploy", "status": "completed",
      "depends_on": [],
    }],
  )
  assert repaired["summary"]["can_complete"] is True
  update_goal_record(db, run, goal, 2, result="Verified repaired plan")
  assert goal.status == "completed"


def test_corrupt_plan_never_authorizes_automatic_handoff(db, chat):
  goal, run = work(db, chat, task_status="completed")
  goal.plan_json = {"version": 1, "tasks": [{"id": "broken"}]}
  goal.revision = 2
  run.goal_plan_revision_at_admission = 1
  db.commit()

  handoff = goal_terminal_handoff(db, chat.id, run.id)
  assert handoff is not None
  assert handoff.automatic_allowed is False


@pytest.mark.parametrize("status", [[], {}], ids=["list", "object"])
def test_malformed_task_status_remains_repairable_without_auto_handoff(db, chat, status):
  goal, run = work(db, chat, task_status=status)
  goal.revision = 2
  run.goal_plan_revision_at_admission = 1
  db.commit()

  assert serialize_plan(db, run, goal) is None
  assert presented_goal(db, chat.id)["status"] == "active"
  handoff = goal_terminal_handoff(db, chat.id, run.id)
  assert handoff is not None
  assert handoff.automatic_allowed is False
  with pytest.raises(GoalPlanError, match="unreadable"):
    update_goal_record(db, run, goal, 2, result="Cannot complete malformed work")
  assert goal.status == "open"

  with pytest.raises(GoalPlanError, match="invalid status"):
    replace_plan(db, physical=run, root=goal, expected_revision=2, tasks=[{
      "id": "deploy", "title": "Deploy", "status": status, "depends_on": [],
    }])
  repaired_tasks = [{
    "id": "deploy", "title": "Deploy", "status": "completed", "depends_on": [],
  }]
  with pytest.raises(GoalPlanConflict, match="changed"):
    replace_plan(db, physical=run, root=goal, expected_revision=1, tasks=repaired_tasks)
  replace_plan(db, physical=run, root=goal, expected_revision=2, tasks=repaired_tasks)
  update_goal_record(db, run, goal, 3, result="Verified repaired work")
  assert goal.status == "completed"
