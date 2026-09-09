"""Activation is an admission owner, not permission to consume later input."""

import asyncio
import copy

import pytest

from app import chat_queue, chat_waits, chat_writer, models
from app.database import SessionLocal
from app.platform_restart import activation_notice
from tests.test_platform_restart_cards import _install, _submit


def _activation_command(chat_id, wait_id, root_id, **kw):
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    content = activation_notice(wait, "met")
  return chat_writer.StartContinuation(
    chat_id=chat_id, run_token=f"activation-resume-{wait_id}",
    root_run_id=root_id, content=content, cid=f"activation-result-{wait_id}",
    reason="wait_result", message_kind="platform_activation_result",
    source_work_id=root_id, hidden=True, activation_wait_id=wait_id, **kw,
  )


def test_answer_while_publisher_finishes_keeps_b_behind_activation(monkeypatch):
  monkeypatch.setattr("app.platform_restart.requirement_matches_current_source", lambda _: True)
  qid, wait_id, run_id, _ = _install("queue-publisher", queued=True)
  with SessionLocal() as db:
    run = db.get(models.ChatRun, run_id)
    run.status = "running"
    run.ended_at = None
    db.commit()
  _submit(chat_writer.ResolvePlatformRestartCard(
    chat_id="queue-publisher", question_id=qid, selected_option_id="restart-id",
  ))
  async def finish(chat_id, token, status):
    await chat_writer.await_ack(chat_writer.get_writer().submit(chat_writer.FinishRun(
      chat_id=chat_id, run_token=token, terminal_status=status,
    )))
  with SessionLocal() as db:
    result = asyncio.run(chat_queue.drain_and_release(
      db, "queue-publisher", None, "must-not-start-b",
      ending_run_token=run_id, discard_starting=lambda _: None,
      forget_chat=lambda _: None, finish_run_strict=finish,
      current_generation=lambda _: 0,
    ))
  assert result[0] is None
  assert result[3] == chat_queue.TerminalDisposition.ACTIVATION_PARKED
  with SessionLocal() as db:
    chat = db.get(models.Chat, "queue-publisher")
    assert chat.pending_question_id is None
    assert [m["content"] for m in chat.pending_messages] == ["B"]
    assert db.get(models.ChatRun, "must-not-start-b") is None
    assert db.get(models.ChatWait, wait_id).status == "armed"


@pytest.mark.parametrize("wait_status", ["armed", "met", "failed", "expired"])
def test_actor_admission_holds_b_without_an_open_question(wait_status):
  qid, wait_id, root, _ = _install(f"actor-{wait_status}", queued=True, status=wait_status)
  with SessionLocal() as db:
    chat = db.get(models.Chat, f"actor-{wait_status}")
    chat.pending_question_id = None
    db.commit()
  result = _submit(chat_writer.PromotePending(chat_id=f"actor-{wait_status}", run_token="b"))
  assert result == chat_writer.PromotePendingBlocked("activation", wait_id=wait_id)


def test_not_now_preserves_unfinished_owner_and_later_matching_activation(monkeypatch):
  monkeypatch.setattr("app.platform_restart.requirement_matches_current_source", lambda _: True)
  qid, wait_id, root, _ = _install("deferred-owner", queued=True)
  result = _submit(chat_writer.ResolvePlatformRestartCard(
    chat_id="deferred-owner", question_id=qid, selected_option_id="cancel-id",
  ))
  assert result["answers"] == {"Restart?": "Not now"}
  assert result["platform_action"]["status"] == "deferred"
  assert not result["dispatch"]
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    assert wait.status == "armed"
    assert wait.action_approved_at is None
    assert db.query(models.PlatformRestartExecution).count() == 0
    wait.status = "met"  # Evidence from another independently approved boot.
    db.commit()
  assert isinstance(_submit(_activation_command("deferred-owner", wait_id, root)), dict)
  with SessionLocal() as db:
    assert [m["content"] for m in db.get(models.Chat, "deferred-owner").pending_messages] == ["B"]


@pytest.mark.parametrize("status", ["armed", "met"])
def test_goal_dismissal_cancels_only_its_activation_owner(status):
  qid, wait_id, root, _ = _install(f"dismiss-{status}", status=status, queued=True)
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    other = models.ChatWait(
      id=f"other-{status}", chat_id=wait.chat_id, kind="platform_activation",
      created_by_run_id=root, root_run_id=root, goal_id="unrelated-goal",
      description="Other", condition_owner="Test", status=status,
      deadline_at=wait.deadline_at, next_check_at=wait.next_check_at,
    )
    generic = models.ChatWait(
      id=f"generic-{status}", chat_id=wait.chat_id, kind="timer",
      goal_id=wait.goal_id, description="Generic", condition_owner="Test", status="armed",
      deadline_at=wait.deadline_at, next_check_at=wait.next_check_at,
    )
    db.add_all([other, generic]); db.commit()
  result = _submit(chat_writer.ClearPresentedGoal(
    chat_id=f"dismiss-{status}", expected_goal_id=f"goal-dismiss-{status}",
  ))
  assert result["status"] == "cleared"
  with SessionLocal() as db:
    assert db.get(models.ChatWait, wait_id).status == "cancelled"
    assert db.get(models.ChatWait, f"other-{status}").status == status
    assert db.get(models.ChatWait, f"generic-{status}").status == "armed"
    chat = db.get(models.Chat, f"dismiss-{status}")
    assert chat.pending_question_id is None
    assert chat.messages[0]["blocks"][0]["platform_action"]["status"] == "dismissed"
    assert "answers" not in chat.messages[0]["blocks"][0]
    assert [m["content"] for m in db.get(models.Chat, f"dismiss-{status}").pending_messages] == ["B"]
  assert asyncio.run(chat_waits._deliver_resume(wait_id)) is False


def test_same_root_database_orphan_is_not_delivery_ownership(monkeypatch):
  chat_id = "orphan-owner"
  _, wait_id, root, _ = _install(chat_id, status="met", queued=True)
  with SessionLocal() as db:
    run = db.get(models.ChatRun, root); run.status = "running"; run.ended_at = None
    db.commit()
  from app import chat as chat_mod, chat_waits
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _: False)
  monkeypatch.setattr(chat_mod, "mark_starting", lambda _: True)
  monkeypatch.setattr(chat_mod, "discard_starting", lambda _: None)
  assert asyncio.run(chat_waits._deliver_resume(wait_id)) is False
  with SessionLocal() as db:
    assert db.get(models.ChatWait, wait_id).resume_delivered_at is None
    assert db.get(models.ChatRun, f"activation-resume-{wait_id}") is None
    assert db.get(models.Chat, chat_id).pending_question_id is not None


@pytest.mark.parametrize("settled_by", ["answer", "external_activation"])
@pytest.mark.parametrize("snapshot_kind", ["history", "live"])
def test_stale_snapshots_preserve_settlement_even_without_forged_yes(monkeypatch, settled_by, snapshot_kind):
  monkeypatch.setattr("app.platform_restart.requirement_matches_current_source", lambda _: True)
  cid = f"snapshot-{settled_by}-{snapshot_kind}"
  qid, wait_id, root, _ = _install(cid, status="met" if settled_by == "external_activation" else "armed")
  with SessionLocal() as db:
    chat = db.get(models.Chat, cid)
    messages = copy.deepcopy(chat.messages)
    messages[0]["id"] = root
    chat.messages = messages
    chat.active_assistant_message_id = root
    if settled_by == "external_activation":
      run = db.get(models.ChatRun, root)
      run.status = "running"
      run.ended_at = None
    db.commit()
    stale = copy.deepcopy(messages[0])
  if settled_by == "answer":
    _submit(chat_writer.ResolvePlatformRestartCard(chat_id=cid, question_id=qid, selected_option_id="cancel-id"))
    expected = "deferred"
  else:
    _submit(_activation_command(cid, wait_id, root, activation_attach_run_token=root))
    expected = "activated"
  with SessionLocal() as db:
    if snapshot_kind == "history":
      chat_writer.update_last_assistant_message(db, cid, stale)
      db.expire_all(); block = db.get(models.Chat, cid).messages[0]["blocks"][0]
    else:
      # Exercise the persisted-history source after QuestionCommit cleared live.
      chat = db.get(models.Chat, cid); chat.live_assistant = None; db.commit()
      chat_writer.update_live_assistant(db, cid, stale)
      db.expire_all(); block = db.get(models.Chat, cid).live_assistant["blocks"][0]
    assert block["platform_action"]["status"] == expected
    if settled_by == "answer":
      assert block["selected_options"] == {"restart": ["cancel-id"]}
      assert block["answers"] == {"Restart?": "Not now"}
    else:
      assert "answers" not in block


def test_tokenless_restart_resolution_fences_publisher_snapshots():
  assert chat_writer._needs_broad_chat_fence(chat_writer.ResolvePlatformRestartCard(chat_id="x"))


@pytest.mark.parametrize("status", ["armed", "met"])
def test_real_stop_cancels_even_met_undelivered_activation(status):
  from app import chat as chat_mod, chat_waits
  cid = f"stop-{status}"
  _, wait_id, _, _ = _install(cid, status=status)
  asyncio.run(chat_mod.stop_chat_for(cid))
  with SessionLocal() as db:
    assert db.get(models.ChatWait, wait_id).status == "cancelled"
    chat = db.get(models.Chat, cid)
    assert chat.pending_question_id is None
    block = chat.messages[0]["blocks"][0]
    assert block["platform_action"]["status"] == "dismissed"
    assert "answers" not in block
  assert asyncio.run(chat_waits._deliver_resume(wait_id)) is False


def test_old_met_receipt_cannot_start_a_goalless_runner_after_tombstone():
  cid = "dismissed-receipt"
  _, wait_id, root, _ = _install(cid, status="met")
  with SessionLocal() as db:
    chat = db.get(models.Chat, cid)
    chat.dismissed_goal_id = f"goal-{cid}"
    db.commit()
  result = _submit(_activation_command(cid, wait_id, root))
  assert result == chat_writer.StartContinuationBlocked("goal_stopped")
  with SessionLocal() as db:
    assert db.get(models.ChatRun, f"activation-resume-{wait_id}") is None
    assert db.get(models.ChatWait, wait_id).status == "cancelled"


def test_active_sink_attaches_activation_once_without_a_second_runner(monkeypatch):
  from app import chat as chat_mod, chat_waits
  from app.broadcast import create_broadcast
  from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
  from app.memory_recall import EMPTY_RECALL_BINDING
  cid = "live-attachment"
  _, wait_id, root, _ = _install(cid, status="met", queued=True)
  with SessionLocal() as db:
    run = db.get(models.ChatRun, root)
    run.status = "running"
    run.ended_at = None
    db.commit()
  sink = ChatEventSink(create_broadcast(cid), cid, run_token=root, recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(cid, sink)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _: True)
  def never_schedule(**_):
    pytest.fail("same live recovery must not schedule a second runner")
  monkeypatch.setattr(chat_mod, "_schedule_continuation", never_schedule)
  try:
    assert asyncio.run(chat_waits._deliver_resume(wait_id)) is True
    assert asyncio.run(chat_waits._deliver_resume(wait_id)) is False
  finally:
    unregister_active_sink(cid, sink)
  with SessionLocal() as db:
    assert db.get(models.ChatWait, wait_id).resume_delivered_at is not None
    assert db.query(models.ChatRun).filter(models.ChatRun.chat_id == cid).count() == 1
    assert [m["content"] for m in db.get(models.Chat, cid).pending_messages] == ["B"]


@pytest.mark.parametrize("settled_by", ["answer", "external_activation"])
def test_restart_settlement_updates_separated_live_snapshot(monkeypatch, settled_by):
  monkeypatch.setattr("app.platform_restart.requirement_matches_current_source", lambda _: True)
  cid = f"live-settlement-{settled_by}"
  qid, wait_id, root, _ = _install(cid, status="met" if settled_by == "external_activation" else "armed")
  with SessionLocal() as db:
    chat = db.get(models.Chat, cid)
    chat.live_assistant = copy.deepcopy(chat.messages[0])
    if settled_by == "external_activation":
      run = db.get(models.ChatRun, root)
      run.status = "running"
      run.ended_at = None
    db.commit()
  if settled_by == "answer":
    _submit(chat_writer.ResolvePlatformRestartCard(chat_id=cid, question_id=qid, selected_option_id="cancel-id"))
  else:
    _submit(_activation_command(cid, wait_id, root, activation_attach_run_token=root))
  with SessionLocal() as db:
    live = db.get(models.Chat, cid).live_assistant
    assert live["blocks"][0]["platform_action"]["status"] == ("deferred" if settled_by == "answer" else "activated")


@pytest.mark.parametrize("wait_status", ["armed", "met", "failed", "expired"])
def test_activity_checkpoint_cannot_overtake_deferred_restart_in_writer(db, wait_status):
  """A late helper result is context for A, not permission to bypass its hold."""
  from datetime import timedelta
  from app.timeutil import now_naive_utc
  from app.delegations import _activity_continuation_run_id
  from tests.test_delegations import _seed_delegation, _seed_idle_parent_wake_root
  from tests.test_platform_restart_cards import _card, _requirement

  cid, _, delegation_id = _seed_delegation(
    db, suffix=f"restart-activity-{wait_status}",
    result_blocks=[{"type": "text", "content": "Result for interrupted A"}],
  )
  root = _seed_idle_parent_wake_root(db, delegation_id)
  question_id, wait_id = f"question-{cid}", f"wait-{cid}"
  requirement = _requirement()
  now = now_naive_utc()
  chat = db.get(models.Chat, cid)
  chat.messages = [{"role": "assistant", "blocks": [_card(question_id, wait_id, requirement)], "ts": 2}]
  chat.pending_question_id = question_id
  db.add(models.ChatWait(
    id=wait_id, chat_id=cid, created_by_run_id=root, root_run_id=root,
    linked_question_id=question_id, kind="platform_activation", status="armed",
    description="Load A's changes", condition_owner="Möbius startup",
    condition_json=requirement, created_at=now, next_check_at=now,
    deadline_at=now + timedelta(days=1), interval_secs=60,
  ))
  db.commit()
  _submit(chat_writer.ResolvePlatformRestartCard(
    chat_id=cid, question_id=question_id, selected_option_id="cancel-id",
  ))
  db.expire_all()
  wait = db.get(models.ChatWait, wait_id)
  wait.status = wait_status
  db.commit()
  chat = db.get(models.Chat, cid)
  before = copy.deepcopy(chat.messages)
  assert chat.pending_question_id is None
  command = chat_writer.StartActivityContinuation(
    chat_id=cid, root_run_id=root,
    run_token=_activity_continuation_run_id(db.get(models.Delegation, delegation_id)),
    source_work_id=root, activity_id=delegation_id,
  )
  # Bypass the async precheck to reproduce a card committing after it passed.
  assert _submit(command) == chat_writer.StartContinuationBlocked("activation_pending")
  db.expire_all()
  assert db.get(models.ChatRun, command.run_token) is None
  assert db.get(models.Chat, cid).messages == before
  assert db.get(models.Chat, cid).pending_messages == []
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  # Once activation's continuation owns the receipt, this seam works normally.
  db.get(models.ChatWait, wait_id).resume_delivered_at = now_naive_utc()
  db.commit()
  assert isinstance(_submit(chat_writer.StartActivityContinuation(
    chat_id=cid, root_run_id=root, run_token=command.run_token,
    source_work_id=root, activity_id=delegation_id,
  )), dict)
