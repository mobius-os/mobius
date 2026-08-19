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
