"""A Goal is durable work, not the physical turn executing it."""
import re
from datetime import UTC, datetime

import pytest

from app import models
from app.chat_writer import GoalPromotionRejected, PromoteRunToGoal, get_writer
from app.goals import update_goal_record
from app.goal_plans import GoalPlanError
from app.run_state import goal_identity_for_run_start


def promote(db, chat, run, objective, **kwargs):
  return get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
    chat_id=chat.id, run_token=run.id, objective=objective, **kwargs,
  ))


def test_one_turn_completes_two_goals_with_distinct_ids_and_outcome_frontiers(db, chat, monkeypatch):
  from app import chat_event_sink
  run = models.ChatRun(id="physical", root_run_id="physical", chat_id=chat.id, status="running")
  db.add(run); db.commit()
  first = promote(db, chat, run, "A")
  assert re.fullmatch(r"[0-9a-f]{32}", first["goal_id"])
  assert first["goal_id"] != first["root_run_id"] == run.id
  goal_a = db.get(models.ChatGoal, first["goal_id"])
  monkeypatch.setattr(chat_event_sink, "active_sink_activity_position", lambda _: {"run_id": run.id, "block_index": 1})
  update_goal_record(db, run, goal_a, 0, complete=True)
  second = promote(db, chat, run, "B")
  assert second["goal_id"] != first["goal_id"]
  assert run.goal_id == second["goal_id"] and run.goal_objective == "B"
  goal_b = db.get(models.ChatGoal, second["goal_id"])
  assert promote(db, chat, run, "B")["state"] == "active"
  monkeypatch.setattr(chat_event_sink, "active_sink_activity_position", lambda _: {"run_id": run.id, "block_index": 3})
  update_goal_record(db, run, goal_b, 0, complete=True)
  assert goal_a.completion_run_id == goal_b.completion_run_id == run.id
  positions = dict(db.query(models.ChatActivityPosition.event_id, models.ChatActivityPosition.position).all())
  assert positions["goal-outcome:" + goal_a.id]["block_index"] == 1
  assert positions["goal-outcome:" + goal_b.id]["block_index"] == 3
  assert db.query(models.ChatGoal).count() == 2


@pytest.mark.parametrize("status", ["open", "stopped", "dismissed"])
def test_nonterminal_goal_cannot_be_rebound_even_with_new_objective(db, chat, status):
  goal = models.ChatGoal(id="a", chat_id=chat.id, objective="A", status=status)
  run = models.ChatRun(id="physical", root_run_id="physical", chat_id=chat.id,
    status="running", goal_id=goal.id, goal_objective=goal.objective)
  db.add_all([goal, run]); db.commit()
  assert isinstance(promote(db, chat, run, "B"), GoalPromotionRejected)
  assert run.goal_id == goal.id


def test_terminal_goal_does_not_hide_another_open_obligation(db, chat):
  goal = models.ChatGoal(id="a", chat_id=chat.id, objective="A", status="completed")
  outstanding = models.ChatGoal(id="other", chat_id=chat.id, objective="Outstanding")
  run = models.ChatRun(id="physical", root_run_id="physical", chat_id=chat.id,
    status="running", goal_id=goal.id, goal_objective=goal.objective)
  db.add_all([goal, outstanding, run]); db.commit()
  rejected = promote(db, chat, run, "B")
  assert rejected.reason == "unfinished_goal_exists"
  assert run.goal_id == goal.id and db.query(models.ChatGoal).count() == 2


def test_legacy_string_completion_remains_accepted_for_running_agents(db, chat):
  goal = models.ChatGoal(id="a", chat_id=chat.id, objective="A")
  run = models.ChatRun(id="physical", chat_id=chat.id, status="running", goal_id=goal.id)
  db.add_all([goal, run]); db.commit()
  update_goal_record(db, run, goal, 0, complete="done")
  assert goal.status == "completed" and goal.result == "done"


@pytest.mark.parametrize("terminal", [False, True])
def test_wait_result_reads_snapshot_not_its_rebound_creator(db, chat, terminal):
  old = models.ChatGoal(id="a", chat_id=chat.id, objective="A", status="completed" if terminal else "open")
  new = models.ChatGoal(id="b", chat_id=chat.id, objective="B")
  run = models.ChatRun(id="physical", root_run_id="physical", chat_id=chat.id,
    status="running", goal_id=new.id, goal_objective=new.objective)
  db.add_all([old, new, run]); db.flush()
  db.add(models.ChatWait(id="old-wait", chat_id=chat.id, created_by_run_id=run.id,
    goal_id=old.id, root_run_id=run.id, kind="timer", status="met", description="A's condition",
    deadline_at=datetime.now(UTC), next_check_at=datetime.now(UTC)))
  db.commit()
  message = {"kind": "wait_result", "cid": "wait-result-old-wait", "source_work_id": run.id}
  expected = (None, None) if terminal else (old.objective, old.id)
  assert goal_identity_for_run_start(db, chat.id, message) == expected
  assert run.goal_id == new.id


@pytest.mark.parametrize("status", ["open", "stopped", "completed"])
def test_peer_wake_uses_original_goal_snapshot_after_source_rebind(db, chat, status):
  old = models.ChatGoal(id="a", chat_id=chat.id, objective="A", status=status)
  new = models.ChatGoal(id="b", chat_id=chat.id, objective="B")
  run = models.ChatRun(id="physical", chat_id=chat.id, status="running", goal_id=new.id)
  db.add_all([old, new, run]); db.commit()
  message = {"kind": "peer_message", "source_work_id": run.id, "goal_id": old.id}
  assert goal_identity_for_run_start(db, chat.id, message) == (
    (old.objective, old.id) if status == "open" else (None, None))
  message["goal_id"] = None
  assert goal_identity_for_run_start(db, chat.id, message) == (None, None)


def test_rebind_to_named_open_goal_does_not_hide_another_open_goal(db, chat):
  from datetime import timedelta
  first = models.ChatGoal(id="a", chat_id=chat.id, objective="A", status="completed")
  hidden = models.ChatGoal(id="hidden", chat_id=chat.id, objective="Unfinished")
  target = models.ChatGoal(id="target", chat_id=chat.id, objective="Target",
    created_at=datetime.now(UTC) + timedelta(seconds=1))
  run = models.ChatRun(id="physical", root_run_id="physical", chat_id=chat.id,
    status="running", goal_id=first.id, goal_objective=first.objective)
  db.add_all([first, hidden, target, run]); db.commit()
  rejected = promote(db, chat, run, target.objective, resume_goal_id=target.id)
  assert rejected.reason == "unfinished_goal_exists"
  assert run.goal_id == first.id


def test_completed_goal_can_promote_successor_and_reuse_helper_name_without_old_waits(db, chat):
  from app.chat_waits import declare_wait, _goal_waits
  from app.delegations import (DelegationIntent, create_or_attach_delegation,
                              own_helper_statuses, parent_wake_blocker)
  from app.goal_plans import _delegation_tree
  run = models.ChatRun(id="shared-physical", root_run_id="shared-physical",
                       chat_id=chat.id, status="running")
  db.add(run); db.commit()
  goal_a = db.get(models.ChatGoal, promote(db, chat, run, "Goal A")["goal_id"])
  intent = dict(app_id=None, parent_chat_id=chat.id, parent_root_run_id=run.id,
                task_key="audit", prompt="Audit the current Goal", provider="codex",
                model=None, effort=None, cwd="/data")
  helper_a, attached = create_or_attach_delegation(db, DelegationIntent(**intent, goal_id=goal_a.id))
  assert not attached
  db.add(models.ChatRun(id="a-helper-finished", chat_id=helper_a.child_chat_id,
                       status="completed"))
  db.commit()
  wait = declare_wait(db, chat_id=chat.id, created_by_run_id=run.id,
                      description="A condition", kind="timer", delay_secs=60)
  update_goal_record(db, run, goal_a, goal_a.revision, complete=True)
  goal_b = db.get(models.ChatGoal, promote(db, chat, run, "Goal B")["goal_id"])
  helper_b, attached = create_or_attach_delegation(db, DelegationIntent(**intent, goal_id=goal_b.id))
  assert not attached and helper_a.id != helper_b.id
  assert helper_a.goal_id == goal_a.id and helper_b.goal_id == goal_b.id
  assert [item["id"] for item in own_helper_statuses(db, chat.id, run.id)] == [helper_b.id]
  assert [item["id"] for item in _delegation_tree(db, run, goal_b)] == [helper_b.id]
  assert [item["id"] for item in _delegation_tree(db, run, goal_a)] == [helper_a.id]
  assert wait.goal_id == goal_a.id and wait.root_run_id == run.id
  assert _goal_waits(db, chat.id, goal_b.id).count() == 0
  assert parent_wake_blocker(db, chat.id, goal_a.id, run.id)[0] == "goal_closed"
  assert goal_identity_for_run_start(db, chat.id, {
    "kind": "wait_result", "cid": f"wait-result-{wait.id}", "source_work_id": run.id,
  }) == (None, None)
