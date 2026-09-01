"""Owned settlement for platform-promoted Goals.

An unfinished Goal promoted during an ordinary agent turn cannot settle with
no actor responsible for the next move. One progress-bounded corrective turn
is allowed; a repeat without plan progress becomes a visible resumable error.
Explicit ``/goal`` starts retain their native provider continuation owners.
"""

from types import SimpleNamespace

import pytest

from app import models
from app.run_state import (
  GOAL_HANDOFF_REASON,
  GoalSettlementTarget,
  goal_settlement_target,
)


UNFINISHED = {"tasks": [
  {"id": "a", "status": "completed"},
  {"id": "b", "status": "running"},
]}
STUCK = {"tasks": [{"id": "a", "status": "running"}]}
SETTLED = {"tasks": [
  {"id": "a", "status": "completed"},
  {"id": "b", "status": "cancelled"},
]}


def _add_run(
  db,
  chat_id,
  run_id,
  *,
  goal_objective="Ship it",
  goal_id=None,
  root_run_id=None,
  status="completed",
  provider="codex",
  plan=None,
):
  root = root_run_id or run_id
  db.add(models.ChatRun(
    id=run_id,
    root_run_id=root,
    chat_id=chat_id,
    status=status,
    provider=provider,
    goal_objective=goal_objective,
    goal_id=goal_id if goal_id is not None else root,
    goal_plan_json=plan,
  ))
  db.commit()


def _target(db, chat, run_id="run-a"):
  return goal_settlement_target(db, chat.id, run_id)


def _correction(goal_id, index=1):
  return {
    "role": "user",
    "content": "correct the handoff",
    "kind": "continuation",
    "continuation_reason": GOAL_HANDOFF_REASON,
    "goal_id": goal_id,
    "cid": f"correction-{index}",
  }


# --- exact Goal classification ---------------------------------------------


def test_auto_promoted_unfinished_plan_returns_target(db, chat):
  _add_run(db, chat.id, "run-a", plan=UNFINISHED)
  assert _target(db, chat) == GoalSettlementTarget(
    goal_id="run-a", retry_allowed=True,
  )


def test_settled_unplanned_and_non_goal_runs_are_not_guarded(db, chat):
  _add_run(db, chat.id, "settled", plan=SETTLED)
  assert _target(db, chat, "settled") is None

  _add_run(db, chat.id, "unplanned", plan=None)
  assert _target(db, chat, "unplanned") is None

  _add_run(
    db, chat.id, "ordinary", goal_objective=None, goal_id=None, plan=None,
  )
  assert _target(db, chat, "ordinary") is None


def test_current_explicit_goal_identity_is_excluded(db, chat):
  _add_run(
    db,
    chat.id,
    "run-a",
    root_run_id="run-a",
    goal_id="explicit-goal-uuid",
    plan=UNFINISHED,
  )
  assert _target(db, chat) is None


def test_historical_explicit_goal_is_excluded_by_its_exact_root_message(db, chat):
  chat.messages = [
    {"role": "user", "content": "/goal Ship it"},
    {"role": "assistant", "id": "run-a", "content": "working"},
  ]
  db.commit()
  # Historical migration used root_run_id as goal_id for every old Goal.
  _add_run(db, chat.id, "run-a", goal_id="run-a", plan=UNFINISHED)
  assert _target(db, chat) is None


def test_unrelated_earlier_goal_command_does_not_exclude_new_auto_goal(db, chat):
  chat.messages = [
    {"role": "user", "content": "/goal Old work"},
    {"role": "assistant", "id": "old-root", "content": "done"},
    {"role": "user", "content": "Please ship this ordinary request"},
    {"role": "assistant", "id": "run-a", "content": "working"},
  ]
  db.commit()
  _add_run(db, chat.id, "run-a", plan=UNFINISHED)
  assert _target(db, chat) is not None


def test_wait_or_helper_wake_keeps_auto_goal_classification(db, chat):
  _add_run(db, chat.id, "a-origin", plan=UNFINISHED)
  # A durable wait/helper wake can start a new physical root while inheriting
  # the original Goal identity. Classification must follow the Goal origin.
  _add_run(
    db,
    chat.id,
    "z-wake",
    root_run_id="z-wake",
    goal_id="a-origin",
    plan=None,
  )
  target = _target(db, chat, "z-wake")
  assert target is not None
  assert target.goal_id == "a-origin"


def test_missing_or_unknown_run_token_returns_none(db, chat):
  assert goal_settlement_target(db, chat.id, "") is None
  assert goal_settlement_target(db, chat.id, "missing") is None


# --- one baseline correction plus one per settled task ---------------------


def test_one_correction_is_allowed_before_any_plan_progress(db, chat):
  _add_run(db, chat.id, "run-a", plan=STUCK)
  assert _target(db, chat).retry_allowed is True


def test_repeat_without_progress_exhausts_the_guard(db, chat):
  chat.messages = [_correction("run-a")]
  db.commit()
  _add_run(db, chat.id, "run-a", plan=STUCK)
  assert _target(db, chat).retry_allowed is False


def test_settled_task_earns_one_further_correction(db, chat):
  chat.messages = [_correction("run-a")]
  db.commit()
  _add_run(db, chat.id, "run-a", plan=UNFINISHED)
  assert _target(db, chat).retry_allowed is True


def test_other_goals_corrections_do_not_consume_this_goal_budget(db, chat):
  chat.messages = [_correction("another-goal")]
  db.commit()
  _add_run(db, chat.id, "run-a", plan=STUCK)
  assert _target(db, chat).retry_allowed is True


# --- truthful next-owner classification ------------------------------------


class _Sink:
  def __init__(self, blocks=None):
    self.assistant_blocks = list(blocks or [])
    self.published = []
    self._last_error = None

  def publish(self, event):
    self.published.append(event)
    if event.get("type") == "error":
      self._last_error = event.get("message")


def test_pending_owner_message_satisfies_handoff(db, chat):
  from app.chat import _goal_handoff_is_owned

  chat.pending_messages = [{"role": "user", "content": "real follow-up"}]
  db.commit()
  assert _goal_handoff_is_owned(db, chat.id, "run-a", _Sink()) is True


def test_pending_question_marker_or_open_question_block_satisfies_handoff(db, chat):
  from app.chat import _goal_handoff_is_owned

  chat.pending_question_id = "q1"
  db.commit()
  assert _goal_handoff_is_owned(db, chat.id, "run-a", _Sink()) is True

  chat.pending_question_id = None
  db.commit()
  sink = _Sink([{"type": "question", "question_id": "q2"}])
  assert _goal_handoff_is_owned(db, chat.id, "run-a", sink) is True


def test_armed_wait_or_waking_helper_satisfies_handoff(db, chat, monkeypatch):
  from app.chat import _goal_handoff_is_owned

  _add_run(db, chat.id, "run-a", plan=STUCK)
  monkeypatch.setattr(
    "app.chat_waits.armed_waits_for_chat",
    lambda *_args: [SimpleNamespace(created_by_run_id="run-a")],
  )
  monkeypatch.setattr(
    "app.delegations.background_helper_goal_ids", lambda *_args: set(),
  )
  assert _goal_handoff_is_owned(db, chat.id, "run-a", _Sink()) is True

  monkeypatch.setattr(
    "app.chat_waits.armed_waits_for_chat", lambda *_args: [],
  )
  monkeypatch.setattr(
    "app.delegations.background_helper_goal_ids",
    lambda *_args: {"run-a"},
  )
  assert _goal_handoff_is_owned(db, chat.id, "run-a", _Sink()) is True


def test_unrelated_wait_or_helper_does_not_own_this_goal(db, chat, monkeypatch):
  from app.chat import _goal_handoff_is_owned

  _add_run(db, chat.id, "run-a", plan=STUCK)
  _add_run(db, chat.id, "other-run", plan=STUCK)
  monkeypatch.setattr(
    "app.chat_waits.armed_waits_for_chat",
    lambda *_args: [SimpleNamespace(created_by_run_id="other-run")],
  )
  monkeypatch.setattr(
    "app.delegations.background_helper_goal_ids",
    lambda *_args: {"other-run"},
  )
  assert _goal_handoff_is_owned(db, chat.id, "run-a", _Sink()) is False


def test_unowned_exhausted_goal_gets_visible_resumable_failure(db, chat):
  from app.chat import _prepare_goal_handoff

  chat.messages = [_correction("run-a")]
  db.commit()
  _add_run(db, chat.id, "run-a", plan=STUCK)
  sink = _Sink()

  assert _prepare_goal_handoff(db, chat.id, "run-a", sink) is None
  assert len(sink.published) == 1
  assert sink.published[0]["type"] == "error"
  assert sink.published[0]["resumable"] is True
  assert "without a visible owner" in sink.published[0]["message"]
  assert sink._last_error == sink.published[0]["message"]


def test_unowned_goal_with_budget_returns_correction_target(db, chat):
  from app.chat import _prepare_goal_handoff

  _add_run(db, chat.id, "run-a", plan=STUCK)
  sink = _Sink()

  assert _prepare_goal_handoff(db, chat.id, "run-a", sink) == _target(db, chat)
  assert sink.published == []


def test_owned_exhausted_goal_does_not_publish_failure(db, chat):
  from app.chat import _prepare_goal_handoff

  chat.messages = [_correction("run-a")]
  chat.pending_messages = [{"role": "user", "content": "I answered"}]
  db.commit()
  _add_run(db, chat.id, "run-a", plan=STUCK)
  sink = _Sink()

  assert _prepare_goal_handoff(db, chat.id, "run-a", sink) is None
  assert sink.published == []


# --- correction message shape and re-check ---------------------------------


class _FakeWriter:
  def __init__(self):
    self.submitted = []

  def submit(self, cmd):
    self.submitted.append(cmd)
    return "ack"


@pytest.mark.asyncio
async def test_enqueue_appends_actionable_continuation(db, chat, monkeypatch):
  from app.chat import _maybe_enqueue_goal_handoff
  from app.chat_writer import AppendPending

  _add_run(db, chat.id, "run-a", plan=STUCK)
  target = _target(db, chat)
  writer = _FakeWriter()

  async def _fake_await_ack(_ack):
    return None

  monkeypatch.setattr("app.chat.get_writer", lambda: writer)
  monkeypatch.setattr("app.chat._await_ack", _fake_await_ack)
  await _maybe_enqueue_goal_handoff(
    db, chat.id, "run-a", target, SimpleNamespace(assistant_blocks=[]),
  )

  assert len(writer.submitted) == 1
  cmd = writer.submitted[0]
  assert isinstance(cmd, AppendPending)
  assert cmd.user_msg["kind"] == "continuation"
  assert cmd.user_msg["continuation_reason"] == GOAL_HANDOFF_REASON
  assert cmd.user_msg["goal_id"] == "run-a"
  assert cmd.user_msg["cid"] == "goal-handoff-run-a"
  assert "clarifying-question tool" in cmd.user_msg["content"]
  assert "prose-only request" in cmd.user_msg["content"]


@pytest.mark.asyncio
async def test_enqueue_recheck_yields_to_real_queued_work(db, chat, monkeypatch):
  from app.chat import _maybe_enqueue_goal_handoff

  _add_run(db, chat.id, "run-a", plan=STUCK)
  target = _target(db, chat)
  chat.pending_messages = [{"role": "user", "content": "owner work"}]
  db.commit()
  writer = _FakeWriter()
  monkeypatch.setattr("app.chat.get_writer", lambda: writer)

  await _maybe_enqueue_goal_handoff(
    db, chat.id, "run-a", target, SimpleNamespace(assistant_blocks=[]),
  )
  assert writer.submitted == []
