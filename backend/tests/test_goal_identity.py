"""A native Goal keeps one identity across physical recovery, not new Goals."""

from tests.goal_fixtures import goal_run as make_goal_run

import pytest

from app import models
from app.chat_writer import AppendPending, Barrier, PromotePending, get_writer
from app.goal_plans import presented_goal
from app.run_state import (
  goal_identity_for_run_start,
  latest_provider_goal_is_dismissed,
)


def test_explicit_goals_mint_identity_and_continuations_inherit_it(db, chat):
  objective, first_id = goal_identity_for_run_start(
    db, chat.id, {"content": "/goal Ship it"},
  )
  assert objective == "Ship it"
  assert first_id
  db.add(make_goal_run(db,
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
  db.add(make_goal_run(db,
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
  db.add(make_goal_run(db,
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


def test_natural_owner_follow_up_keeps_a_paused_goal_with_unfinished_plan(
  db, chat,
):
  db.add(make_goal_run(db,
    id="planned-natural-goal", root_run_id="planned-natural-goal",
    chat_id=chat.id, status="interrupted", provider="codex",
    goal_objective="Ship it", goal_id="stable-natural-goal",
    goal_plan_json={
      "tasks": [{"id": "prepare", "status": "running"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "keep going please"},
  ) == ("Ship it", "stable-natural-goal")


def test_unrelated_owner_follow_up_does_not_revive_an_unfinished_goal(db, chat):
  db.add(make_goal_run(db,
    id="unrelated-natural-goal", root_run_id="unrelated-natural-goal",
    chat_id=chat.id, status="interrupted", provider="claude",
    goal_objective="Ship it", goal_id="unrelated-natural-id",
    goal_plan_json={
      "tasks": [{"id": "prepare", "status": "running"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "Can you explain the result?"},
  ) == (None, None)
  assert goal_identity_for_run_start(
    db, chat.id, {"content": "Please continue with a separate question"},
  ) == (None, None)


def test_natural_resume_never_overrides_explicit_stop(db, chat):
  db.add(make_goal_run(db,
    id="stopped-natural-goal", root_run_id="stopped-natural-goal",
    chat_id=chat.id, status="stopped", provider="codex",
    goal_objective="Ship it", goal_id="stopped-natural-id",
    goal_plan_json={
      "tasks": [{"id": "prepare", "status": "running"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "keep going please"},
  ) == (None, None)
  assert goal_identity_for_run_start(db, chat.id, {
    "content": "answer", "kind": "continuation",
    "continuation_reason": "question_answer",
  }) == (None, None)
  assert goal_identity_for_run_start(
    db, chat.id, {"content": "continue"},
  ) == ("Ship it", "stopped-natural-id")


def test_result_continuations_never_tunnel_through_explicit_stop(db, chat):
  db.add(make_goal_run(db,
    id="stopped-result-goal", root_run_id="stopped-result-goal",
    chat_id=chat.id, status="stopped", provider="codex",
    goal_objective="Ship it", goal_id="stopped-result-id",
    goal_plan_json={
      "tasks": [{"id": "prepare", "status": "running"}],
    },
  ))
  db.commit()

  for kind, source_work_id in (
    ("wait_result", "stopped-result-goal"),
    ("delegation_result", "stopped-result-id"),
  ):
    assert goal_identity_for_run_start(db, chat.id, {
      "content": "controller result",
      "kind": kind,
      "hidden": True,
      "source_work_id": source_work_id,
    }) == (None, None)


@pytest.mark.parametrize(("kind", "source_work_id"), [
  ("wait_result", "result-origin"),
  ("delegation_result", "result-goal-id"),
])
def test_stale_result_does_not_revive_stopped_goal_after_later_ordinary_run(
  db, chat, kind, source_work_id,
):
  db.add_all([
    make_goal_run(db,
      id="result-origin", root_run_id="result-origin", chat_id=chat.id,
      status="completed", provider="codex", goal_objective="Ship it",
      goal_id="result-goal-id", started_at=chat.created_at,
    ),
    make_goal_run(db,
      id="yy-stopped-goal", root_run_id="result-origin", chat_id=chat.id,
      status="stopped", provider="codex", goal_objective="Ship it",
      goal_id="result-goal-id", started_at=chat.created_at,
    ),
    make_goal_run(db,
      id="zz-later-ordinary", root_run_id="zz-later-ordinary",
      chat_id=chat.id, status="completed", provider="codex",
      started_at=chat.created_at,
    ),
  ])
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": "stale controller result",
    "kind": kind,
    "hidden": True,
    "source_work_id": source_work_id,
  }) == (None, None)


@pytest.mark.parametrize(("kind", "source_work_id"), [
  ("wait_result", "dismissed-result-origin"),
  ("delegation_result", "dismissed-result-goal-id"),
])
def test_stale_result_does_not_revive_dismissed_goal(
  db, chat, kind, source_work_id,
):
  db.add_all([
    make_goal_run(db,
      id="dismissed-result-origin", root_run_id="dismissed-result-origin",
      chat_id=chat.id, status="completed", provider="claude",
      goal_objective="Ship it", goal_id="dismissed-result-goal-id",
      started_at=chat.created_at,
    ),
    make_goal_run(db,
      id="zz-dismissed-later-ordinary",
      root_run_id="zz-dismissed-later-ordinary", chat_id=chat.id,
      status="completed", provider="claude", started_at=chat.created_at,
    ),
  ])
  chat.dismissed_goal_id = "dismissed-result-goal-id"
  db.flush()
  db.get(models.ChatGoal, "dismissed-result-goal-id").status = "dismissed"
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": "stale controller result",
    "kind": kind,
    "hidden": True,
    "source_work_id": source_work_id,
  }) == (None, None)


@pytest.mark.parametrize("status", ["completed", "interrupted"])
@pytest.mark.parametrize(("kind", "source_work_id"), [
  ("wait_result", "recoverable-result-origin"),
  ("delegation_result", "recoverable-result-goal-id"),
])
def test_result_recovers_origin_goal_while_latest_physical_is_recoverable(
  db, chat, kind, source_work_id, status,
):
  db.add(make_goal_run(db,
    id="recoverable-result-origin", root_run_id="recoverable-result-origin",
    chat_id=chat.id, status=status, provider="codex",
    goal_objective="Ship it", goal_id="recoverable-result-goal-id",
  ))
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": "current controller result",
    "kind": kind,
    "hidden": True,
    "source_work_id": source_work_id,
  }) == ("Ship it", "recoverable-result-goal-id")


def test_natural_resume_does_not_compete_with_question_or_wait(db, chat):
  goal = make_goal_run(db,
    id="blocked-natural-goal", root_run_id="blocked-natural-goal",
    chat_id=chat.id, status="interrupted", provider="claude",
    goal_objective="Ship it", goal_id="blocked-natural-id",
    goal_plan_json={
      "tasks": [{"id": "prepare", "status": "running"}],
    },
  )
  db.add(goal)
  chat.pending_question_id = "owner-choice"
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "please keep going"},
  ) == (None, None)

  chat.pending_question_id = None
  db.add(models.ChatWait(
    id="natural-resume-wait", chat_id=chat.id,
    created_by_run_id=goal.id, description="external work",
    kind="timer", due_at=None, interval_secs=300,
    deadline_at=goal.started_at, status="armed",
    next_check_at=goal.started_at,
  ))
  db.commit()
  assert goal_identity_for_run_start(
    db, chat.id, {"content": "continue please!"},
  ) == (None, None)


def test_natural_owner_follow_up_does_not_revive_a_settled_plan(db, chat):
  db.add(make_goal_run(db,
    id="settled-natural-goal", root_run_id="settled-natural-goal",
    chat_id=chat.id, status="completed", provider="codex",
    goal_objective="Done", goal_id="settled-natural-id",
    goal_plan_json={
      "tasks": [{"id": "verify", "status": "completed"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(
    db, chat.id, {"content": "Can you explain the result?"},
  ) == (None, None)


def test_plain_continue_with_upload_manifest_resumes_the_same_goal(db, chat):
  db.add(make_goal_run(db,
    id="planned-upload-goal",
    root_run_id="planned-upload-goal",
    chat_id=chat.id,
    status="completed",
    provider="codex",
    goal_objective="Ship it",
    goal_id="stable-upload-goal",
    goal_plan_json={
      "tasks": [{"id": "prepare", "status": "pending"}],
    },
  ))
  db.commit()

  assert goal_identity_for_run_start(db, chat.id, {
    "content": (
      "continue\n\n"
      "[Files in this session:\n"
      "- image.png → /data/chats/example/uploads/image.png "
      "(image/png, 257 KB)]"
    ),
  }) == ("Ship it", "stable-upload-goal")


def test_semantic_recovery_skips_intervening_no_goal_run_for_unfinished_plan(
  db, chat,
):
  db.add_all([
    make_goal_run(db,
      id="planned-goal", root_run_id="planned-goal", chat_id=chat.id,
      status="completed", provider="codex", goal_objective="Ship it",
      goal_id="stable-goal", goal_plan_json={
        "tasks": [{"id": "verify", "status": "running"}],
      },
    ),
    make_goal_run(db,
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
  db.add(make_goal_run(db,
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


def test_stopped_unplanned_goal_stays_resumable_after_ordinary_turns(db, chat):
  db.add_all([
    make_goal_run(db,
      id="paused-goal", root_run_id="paused-goal", chat_id=chat.id,
      status="stopped", provider="codex", goal_objective="Pause safely",
      goal_id="paused-id",
    ),
    make_goal_run(db,
      id="later-question", root_run_id="later-question", chat_id=chat.id,
      status="completed", provider="codex",
    ),
  ])
  db.commit()

  assert presented_goal(db, chat.id) == {
    "id": "paused-id",
    "objective": "Pause safely",
    "status": "paused",
    "resumable": True,
  }
  assert goal_identity_for_run_start(
    db, chat.id, {"content": "continue"},
  ) == ("Pause safely", "paused-id")


def test_direct_goal_dismissal_prevents_resume_without_a_tombstone_run(db, chat):
  db.add(make_goal_run(db,
    id="paused-goal", root_run_id="paused-goal", chat_id=chat.id,
    status="stopped", provider="codex", goal_objective="Pause safely",
    goal_id="paused-id",
  ))
  chat.dismissed_goal_id = "paused-id"
  db.flush()
  db.get(models.ChatGoal, "paused-id").status = "dismissed"
  db.commit()

  assert presented_goal(db, chat.id) is None
  assert latest_provider_goal_is_dismissed(db, chat.id) is True
  assert goal_identity_for_run_start(
    db, chat.id, {"content": "continue"},
  ) == (None, None)


def test_new_goal_remains_visible_after_the_previous_identity_was_dismissed(
  db, chat,
):
  db.add_all([
    make_goal_run(db,
      id="old-goal", root_run_id="old-goal", chat_id=chat.id,
      status="stopped", provider="codex", goal_objective="Old",
      goal_id="old-id",
    ),
    make_goal_run(db,
      id="new-goal", root_run_id="new-goal", chat_id=chat.id,
      status="running", provider="codex", goal_objective="New",
      goal_id="new-id",
    ),
  ])
  chat.dismissed_goal_id = "old-id"
  db.commit()

  assert presented_goal(db, chat.id) == {
    "id": "new-id",
    "objective": "New",
    "status": "active",
    "resumable": False,
  }
  assert latest_provider_goal_is_dismissed(db, chat.id) is False


def test_delegation_result_inherits_only_its_originating_goal(db, chat):
  db.add_all([
    make_goal_run(db,
      id="origin", root_run_id="origin", chat_id=chat.id,
      status="completed", provider="codex", goal_objective="Ship it",
      goal_id="origin-goal",
    ),
    make_goal_run(db,
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
  db.add(make_goal_run(db,
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


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_manual_recovery_preserves_unfinished_goal_and_plan(db, chat, status):
  db.add(make_goal_run(db,
    id="handoff-goal", root_run_id="handoff-goal", chat_id=chat.id,
    status=status, provider="codex", goal_objective="Finish rollout",
    goal_id="handoff-goal", goal_plan_json={
      "tasks": [{"id": "verify", "status": "running"}],
    },
  ))
  db.commit()
  assert goal_identity_for_run_start(db, chat.id, {
    "content": "continue", "kind": "continuation",
    "continuation_reason": "manual",
  }) == ("Finish rollout", "handoff-goal")
  # Ordinary follow-ups must not silently adopt this Goal.
  assert goal_identity_for_run_start(db, chat.id, {
    "content": "Explain something unrelated",
  }) == (None, None)


def test_manual_recovery_does_not_revive_a_dismissed_failed_goal(db, chat):
  db.add(make_goal_run(db,
    id="dismissed-failure", root_run_id="dismissed-failure", chat_id=chat.id,
    status="failed", provider="codex", goal_objective="Old work",
    goal_id="dismissed-failure", goal_plan_json={
      "tasks": [{"id": "verify", "status": "running"}],
    },
  ))
  chat.dismissed_goal_id = "dismissed-failure"
  db.flush()
  db.get(models.ChatGoal, "dismissed-failure").status = "dismissed"
  db.commit()
  assert goal_identity_for_run_start(db, chat.id, {
    "content": "continue", "kind": "continuation",
    "continuation_reason": "manual",
  }) == (None, None)
