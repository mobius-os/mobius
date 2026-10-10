"""Explicit holds preserve attribution without guessing from attempt outcomes."""
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app import models
from app.goal_context import project_goal
from app.goal_plans import _goal_presentation
from app.goals import admit_goal, goal_hold, stage_goal_hold
from app.schema_migrations import _add_goal_hold


def make_goal(db, chat):
  goal = models.ChatGoal(id="held-goal", chat_id=chat.id, objective="Entire outcome",
                         next_action="Verify rest")
  db.add(goal)
  db.commit()
  return goal


@pytest.mark.parametrize("actor", ["owner", "agent", "unknown"])
@pytest.mark.parametrize("cause", ["stop", "quiet_answer"])
def test_hold_is_durable_and_retry_does_not_replace_original_intent(db, chat, actor, cause):
  goal = make_goal(db, chat)
  assert stage_goal_hold(goal, cause=cause, actor=actor, source_id="intent-1",
                         run_id="attempt-1", actor_id="actor-1")
  first = deepcopy(goal_hold(goal))
  assert first == {"cause": cause, "actor": actor, "source_id": "intent-1",
                   "run_id": "attempt-1", "actor_id": "actor-1", "at": first["at"]}
  assert datetime.fromisoformat(first["at"]).tzinfo == UTC
  db.commit()
  db.expire_all()
  assert goal.status == "stopped" and goal.revision == 1
  assert not stage_goal_hold(goal, cause="stop", actor="owner", source_id="retry")
  assert goal_hold(goal) == first and goal.revision == 1
  assert goal.next_action == "Verify rest"
  assert goal.result is None and goal.completed_at is None
  assert project_goal(goal)["hold"] == first


@pytest.mark.parametrize("status", ["completed", "cannot_complete", "cancelled", "dismissed"])
def test_hold_does_not_change_closed_goal_or_terminal_outcome(db, chat, status):
  goal = make_goal(db, chat)
  goal.status, goal.result = status, "Truthful outcome"
  assert not stage_goal_hold(goal, cause="stop", actor="owner", source_id="late")
  assert goal.status == status and goal.result == "Truthful outcome"
  assert goal.revision == 0 and goal.hold_json is None


@pytest.mark.parametrize("invalid", [None, [], "owner", {}, {"actor": "owner"},
  {"cause": "stop", "actor": "owner", "source_id": "intent", "run_id": None,
   "actor_id": None, "at": "not-a-time"}])
def test_invalid_legacy_hold_never_invents_owner_attribution(db, chat, invalid):
  goal = make_goal(db, chat)
  goal.status, goal.hold_json = "stopped", invalid
  assert goal_hold(goal) is None
  physical = models.ChatRun(id="attempt", goal_id=goal.id, status="running")
  shown = _goal_presentation(db, physical, goal, None)
  assert shown["pause_reason"] == "unknown"
  assert shown["handoff"] == {"kind": "recovery", "reason": "unknown_stop"}
  assert "hold" not in project_goal(goal)


@pytest.mark.parametrize("key,value", [
  ("actor", []), ("actor", "system"), ("cause", {}), ("cause", "interruption"),
  ("source_id", ""), ("source_id", 1), ("run_id", []), ("actor_id", 7),
  ("at", "2026-10-01T00:00:00"), ("at", None),
])
def test_read_boundary_rejects_each_malformed_hold_field(db, chat, key, value):
  goal = make_goal(db, chat)
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="intent")
  goal.hold_json = {**goal.hold_json, key: value}
  assert goal_hold(goal) is None


@pytest.mark.parametrize("actor,kind,reason", [
  ("owner", "owner_hold", "owner"), ("agent", "recovery", "agent_pause"),
  ("unknown", "recovery", "unknown_stop"),
])
def test_valid_hold_precedes_unrelated_question_and_running_attempt(db, chat, actor, kind, reason):
  goal = make_goal(db, chat)
  stage_goal_hold(goal, cause="quiet_answer", actor=actor, source_id="intent")
  chat.pending_question_id = "unrelated-question"
  db.add(models.ChatRun(id="unrelated", chat_id=chat.id, status="running"))
  db.commit()
  physical = models.ChatRun(id="attempt", goal_id=goal.id, status="running")
  shown = _goal_presentation(db, physical, goal, None)
  assert shown["status"] == "paused" and shown["resumable"]
  assert shown["revision"] == goal.revision == 1
  assert shown["pause_reason"] == actor
  assert shown["handoff"] == {"kind": kind, "reason": reason}


@pytest.mark.parametrize("message,reopens", [
  ({"content": "Continue"}, True),
  ({"kind": "continuation", "continuation_reason": "manual"}, True),
  ({"content": "Unrelated question"}, False),
  ({"content": "Now finish the outstanding audit"}, False),
  ({"kind": "continuation", "continuation_reason": "restart"}, False),
])
def test_only_explicit_continue_clears_exact_goal_hold(db, chat, message, reopens):
  goal = make_goal(db, chat)
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="intent")
  saved = deepcopy(goal.hold_json)
  admit_goal(db, chat.id, goal.id, goal.objective, message)
  db.commit()
  assert goal.status == ("open" if reopens else "stopped")
  assert goal.hold_json == (None if reopens else saved)
  assert goal.revision == (2 if reopens else 1)



def test_continue_does_not_clear_another_goals_hold(db, chat):
  goal = make_goal(db, chat)
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="intent")
  admit_goal(db, chat.id, "different-goal", "Different outcome", {"content": "Continue"})
  db.commit()
  assert goal.status == "stopped" and goal_hold(goal)["source_id"] == "intent"


@pytest.mark.parametrize("status", ["completed", "cannot_complete", "cancelled"])
def test_terminal_presentation_ignores_any_stale_hold(db, chat, status):
  goal = make_goal(db, chat)
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="intent")
  goal.status, goal.result = status, "Truthful outcome"
  physical = models.ChatRun(id="attempt", goal_id=goal.id, status="running")
  shown = _goal_presentation(db, physical, goal, None)
  assert shown["status"] == status and shown["result"] == "Truthful outcome"
  assert shown["handoff"] == {"kind": "none", "reason": None}
  assert not shown["resumable"] and "pause_reason" not in shown
  assert "hold" not in project_goal(goal)


def test_fresh_schema_has_nullable_hold_and_migration_is_noop(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
  models.Base.metadata.create_all(eng)
  before = inspect(eng).get_columns("chat_goals")
  assert next(col for col in before if col["name"] == "hold_json")["nullable"]
  _add_goal_hold(eng)
  _add_goal_hold(eng)
  assert [col["name"] for col in inspect(eng).get_columns("chat_goals")] == [col["name"] for col in before]
  eng.dispose()


def test_frozen_pre_hold_upgrade_keeps_stops_and_outcomes_without_attribution(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
  with eng.begin() as conn:
    # Frozen previous ChatGoal shape, not generated from the evolving model.
    conn.execute(text("""CREATE TABLE chat_goals (
      id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(36) NOT NULL,
      objective TEXT NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'open',
      plan_json JSON, revision INTEGER NOT NULL DEFAULT 0, checkpoint TEXT,
      next_action TEXT, result TEXT, created_at DATETIME NOT NULL, completed_at DATETIME
    )"""))
    for status in ("open", "stopped", "completed", "cancelled", "cannot_complete"):
      conn.execute(text("""INSERT INTO chat_goals
        (id, chat_id, objective, status, revision, result, created_at)
        VALUES (:status, 'chat', 'Entire outcome', :status, 7, 'Saved evidence', '2026-09-30')"""),
        {"status": status})
    before = conn.execute(text("SELECT * FROM chat_goals ORDER BY id")).all()
  _add_goal_hold(eng)
  _add_goal_hold(eng)
  with eng.connect() as conn:
    after = conn.execute(text("SELECT * FROM chat_goals ORDER BY id")).all()
    assert [tuple(row[:-1]) for row in after] == [tuple(row) for row in before]
    assert all(row[-1] is None for row in after)
  eng.dispose()
