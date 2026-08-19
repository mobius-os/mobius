"""A native Goal keeps one identity across physical recovery, not new Goals."""

from app import models
from app.run_state import goal_identity_for_run_start


def test_explicit_goals_mint_identity_and_continuations_inherit_it(db, chat):
  objective, first_id = goal_identity_for_run_start(
    db, chat.id, {"content": "/goal Ship it"},
  )
  assert objective == "Ship it"
  assert first_id
  db.add(models.ChatRun(
    id="first-run", root_run_id="first-run", chat_id=chat.id,
    status="interrupted", provider="codex", goal_objective=objective,
    goal_id=first_id,
  ))
  db.commit()

  resumed_objective, resumed_id = goal_identity_for_run_start(
    db, chat.id, {
      "content": "continue", "kind": "continuation",
      "continuation_reason": "restart",
    },
  )
  assert resumed_objective == objective
  assert resumed_id == first_id

  repeated_objective, repeated_id = goal_identity_for_run_start(
    db, chat.id, {"content": "/goal Ship it"},
  )
  assert repeated_objective == objective
  assert repeated_id != first_id


def test_ordinary_turn_does_not_revive_the_previous_goal(db, chat):
  db.add(models.ChatRun(
    id="old-goal", root_run_id="old-goal", chat_id=chat.id,
    status="interrupted", provider="claude", goal_objective="Old",
    goal_id="old-id",
  ))
  db.commit()
  assert goal_identity_for_run_start(
    db, chat.id, {"content": "unrelated follow-up"},
  ) == (None, None)


def test_plain_continue_keeps_a_completed_physical_run_with_unfinished_plan(
  db, chat,
):
  db.add(models.ChatRun(
    id="planned-goal", root_run_id="planned-goal", chat_id=chat.id,
    status="completed", provider="codex", goal_objective="Ship it",
    goal_id="stable-goal", goal_plan_json={
      "tasks": [{"id": "verify", "status": "running"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "continue"},
  ) == ("Ship it", "stable-goal")


def test_semantic_recovery_skips_intervening_no_goal_run_for_unfinished_plan(
  db, chat,
):
  db.add_all([
    models.ChatRun(
      id="planned-goal", root_run_id="planned-goal", chat_id=chat.id,
      status="completed", provider="codex", goal_objective="Ship it",
      goal_id="stable-goal", goal_plan_json={
        "tasks": [{"id": "verify", "status": "running"}],
      },
    ),
    models.ChatRun(
      id="intervening", root_run_id="intervening", chat_id=chat.id,
      status="interrupted", provider="codex",
    ),
  ])
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": "continue", "kind": "continuation",
    "continuation_reason": "restart",
  }) == ("Ship it", "stable-goal")


def test_continue_does_not_revive_a_settled_plan(db, chat):
  db.add(models.ChatRun(
    id="settled-goal", root_run_id="settled-goal", chat_id=chat.id,
    status="completed", provider="codex", goal_objective="Done",
    goal_id="settled-id", goal_plan_json={
      "tasks": [{"id": "verify", "status": "completed"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "continue"},
  ) == (None, None)


def test_delegation_result_inherits_only_its_originating_goal(db, chat):
  db.add_all([
    models.ChatRun(
      id="origin", root_run_id="origin", chat_id=chat.id,
      status="completed", provider="codex", goal_objective="Ship it",
      goal_id="origin-goal",
    ),
    models.ChatRun(
      id="later", root_run_id="later", chat_id=chat.id,
      status="completed", provider="codex", goal_objective="Different",
      goal_id="later-goal",
    ),
  ])
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": "<delegation_results>[]</delegation_results>",
    "kind": "delegation_result",
    "hidden": True,
    "source_work_id": "origin-goal",
  }) == ("Ship it", "origin-goal")


def test_delegation_result_without_a_goal_origin_stays_non_goal(db, chat):
  db.add(models.ChatRun(
    id="old", root_run_id="old", chat_id=chat.id,
    status="completed", provider="codex", goal_objective="Old",
    goal_id="old-goal",
  ))
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": "result",
    "kind": "delegation_result",
    "hidden": True,
    "source_work_id": "ordinary-root",
  }) == (None, None)
