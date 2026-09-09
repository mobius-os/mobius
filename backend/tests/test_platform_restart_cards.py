"""Typed Restart actions stay exact, at-most-once, and ahead of queued work."""

from __future__ import annotations

from datetime import timedelta
import asyncio

import pytest

from app import chat_writer, models
from app import auth as auth_mod
from app.broadcast import create_broadcast
from app.chat_event_sink import (
  ChatEventSink, register_active_sink, unregister_active_sink,
)
from app.chat_writer import (
  AnswerQuestion,
  ResolvePlatformRestartCard,
  StartContinuation,
  get_writer,
)
from app.database import SessionLocal
from app.platform_restart import activation_notice
from app.memory_recall import EMPTY_RECALL_BINDING
from app.timeutil import now_naive_utc


def _requirement(action_id="platform-restart:test"):
  return {
    "version": 1,
    "action_id": action_id,
    "source_boot_id": "boot-old",
    "target_sha": "a" * 40,
    "paths": ["backend/app/example.py"],
    "files": {
      "backend/app/example.py": {
        "state": "file", "mode": "100644", "sha256": "b" * 64,
      },
    },
  }


def _card(question_id, wait_id, requirement):
  return {
    "type": "question",
    "question_id": question_id,
    "response_mode": "continuation",
    "questions": [{
      "id": "restart", "header": "Restart Möbius", "question": "Restart?",
      "options": [
        {"id": "cancel-id", "label": "Not now", "description": "Wait.",
         "on_answer": "close"},
        {"id": "restart-id", "label": "Restart now", "description": "Restart.",
         "on_answer": "close"},
      ],
    }],
    "platform_action": {
      "version": 1, "type": "restart",
      "action_id": requirement["action_id"],
      "restart_option_id": "restart-id", "cancel_option_id": "cancel-id",
      "wait_id": wait_id, "requirement": requirement,
      "status": "awaiting_owner",
    },
  }


def _install(chat_id, *, action_id="platform-restart:test", status="armed", queued=False):
  now = now_naive_utc()
  question_id = f"question-{chat_id}"
  wait_id = f"wait-{chat_id}"
  run_id = f"run-{chat_id}"
  requirement = _requirement(action_id)
  with SessionLocal() as db:
    db.add(models.Chat(
      id=chat_id, title="Restart", pending_question_id=question_id,
      messages=[{"role": "assistant", "blocks": [
        _card(question_id, wait_id, requirement),
      ], "ts": 2}],
      pending_messages=(
        [{"role": "user", "content": "B", "cid": f"b-{chat_id}", "ts": 3}]
        if queued else []
      ),
    ))
    db.add(models.ChatRun(
      id=run_id, chat_id=chat_id, root_run_id=run_id, status="completed",
      provider="claude", started_at=now - timedelta(minutes=1), ended_at=now,
      goal_objective=f"Goal {chat_id}", goal_id=f"goal-{chat_id}",
    ))
    db.add(models.ChatWait(
      id=wait_id, chat_id=chat_id, created_by_run_id=run_id,
      root_run_id=run_id, goal_id=f"goal-{chat_id}",
      linked_question_id=question_id, description="Load source",
      condition_owner="Möbius startup", kind="platform_activation",
      condition_json=requirement, interval_secs=60,
      deadline_at=now + timedelta(days=1), next_check_at=now,
      status=status, created_at=now,
      met_at=now if status == "met" else None,
    ))
    db.commit()
  return question_id, wait_id, run_id, requirement


def _submit(command):
  return get_writer().submit(command).result(timeout=5)


def test_duplicate_and_cross_chat_cards_share_one_durable_dispatch(monkeypatch):
  monkeypatch.setattr(
    "app.platform_restart.requirement_matches_current_source", lambda _r: True,
  )
  q1, w1, _r1, requirement = _install("restart-one")
  q2, w2, _r2, _ = _install("restart-two", action_id=requirement["action_id"])

  first = _submit(ResolvePlatformRestartCard(
    chat_id="restart-one", question_id=q1, selected_option_id="restart-id",
  ))
  retry = _submit(ResolvePlatformRestartCard(
    chat_id="restart-one", question_id=q1, selected_option_id="restart-id",
  ))
  joined = _submit(ResolvePlatformRestartCard(
    chat_id="restart-two", question_id=q2, selected_option_id="restart-id",
  ))

  assert first["dispatch"] is True
  assert retry["dispatch"] is False
  assert joined["dispatch"] is False
  with SessionLocal() as db:
    assert db.query(models.PlatformRestartExecution).count() == 1
    assert db.get(models.ChatWait, w1).action_approved_at is not None
    assert db.get(models.ChatWait, w2).action_approved_at is not None


def test_free_text_legacy_and_post_stop_cannot_claim_restart(monkeypatch):
  monkeypatch.setattr(
    "app.platform_restart.requirement_matches_current_source", lambda _r: True,
  )
  qid, wait_id, _run, _requirement = _install("restart-guard")
  with pytest.raises(Exception):
    _submit(ResolvePlatformRestartCard(
      chat_id="restart-guard", question_id=qid,
      selected_option_id="Restart now",
    ))
  with pytest.raises(Exception):
    _submit(AnswerQuestion(
      chat_id="restart-guard", question_id=qid,
      answers={"restart": "Restart now"},
    ))
  with SessionLocal() as db:
    row = db.get(models.ChatWait, wait_id)
    row.status = "cancelled"
    row.cancelled_at = now_naive_utc()
    db.commit()
  with pytest.raises(Exception):
    _submit(ResolvePlatformRestartCard(
      chat_id="restart-guard", question_id=qid,
      selected_option_id="restart-id",
    ))
  with SessionLocal() as db:
    assert db.query(models.PlatformRestartExecution).count() == 0


def test_source_change_after_claim_refuses_side_effect_admission(monkeypatch):
  from app import chat as chat_mod
  from app import restart_util

  monkeypatch.setattr(
    "app.platform_restart.requirement_matches_current_source", lambda _r: True,
  )
  qid, wait_id, _run, requirement = _install("restart-source-race")
  claimed = _submit(ResolvePlatformRestartCard(
    chat_id="restart-source-race", question_id=qid,
    selected_option_id="restart-id",
  ))
  assert claimed["dispatch"] is True

  monkeypatch.setattr(
    "app.platform_restart.requirement_matches_current_source", lambda _r: False,
  )
  restart_util._RESTART_ADMITTED = False
  chat_mod.draining = False
  asyncio.run(restart_util.restart_this_worker(
    action_id=requirement["action_id"],
  ))
  with SessionLocal() as db:
    execution = db.get(models.PlatformRestartExecution, requirement["action_id"])
    assert execution.status == "source_changed"
    assert db.get(models.ChatWait, wait_id).status == "failed"
  assert chat_mod.draining is False
  assert restart_util._RESTART_ADMITTED is False


def test_activation_continuation_precedes_and_preserves_queued_b():
  qid, wait_id, root_id, _requirement = _install(
    "restart-order", status="met", queued=True,
  )
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    content = activation_notice(wait, "met")

  result = _submit(StartContinuation(
    chat_id="restart-order", run_token=f"activation-resume-{wait_id}",
    root_run_id=root_id, content=content,
    cid=f"activation-result-{wait_id}", reason="wait_result",
    message_kind="platform_activation_result", source_work_id=root_id,
    hidden=True, activation_wait_id=wait_id,
  ))
  assert isinstance(result, dict)
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-order")
    assert [m["content"] for m in chat.pending_messages] == ["B"]
    assert chat.messages[-1]["cid"] == f"activation-result-{wait_id}"
    assert chat.messages[-1]["hidden"] is True
    assert chat.pending_question_id is None
    card = chat.messages[0]["blocks"][0]
    assert card["platform_action"]["status"] == "activated"
    assert "answers" not in card  # external proof is never a forged Yes
    run = db.get(models.ChatRun, f"activation-resume-{wait_id}")
    assert run.root_run_id == root_id
    assert run.goal_id == "goal-restart-order"


def test_not_now_defers_execution_without_abandoning_activation(monkeypatch):
  monkeypatch.setattr(
    "app.platform_restart.requirement_matches_current_source", lambda _r: True,
  )
  qid, wait_id, _run, _requirement = _install("restart-cancel")
  result = _submit(ResolvePlatformRestartCard(
    chat_id="restart-cancel", question_id=qid,
    selected_option_id="cancel-id",
  ))
  assert result["dispatch"] is False
  assert result["status"] == "deferred"
  with SessionLocal() as db:
    assert db.get(models.ChatWait, wait_id).status == "armed"
    assert db.query(models.PlatformRestartExecution).count() == 0


def test_route_dispatches_platform_restart_once_without_an_answer_turn(
  client, chat, auth, db, monkeypatch,
):
  requirement = _requirement("platform-restart:route")
  monkeypatch.setattr(
    "app.platform_restart.build_restart_requirement", lambda: requirement,
  )
  monkeypatch.setattr(
    "app.platform_restart.requirement_matches_current_source", lambda _r: True,
  )
  calls = []

  async def captured_restart(*_args, **_kwargs):
    calls.append("restart")

  monkeypatch.setattr(
    "app.restart_util.restart_this_worker", captured_restart,
  )
  run_id = f"request-{chat.id}"
  _submit(chat_writer.StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Prepare", "ts": 1},
  ))
  sink = ChatEventSink(
    create_broadcast(chat.id), chat.id, run_token=run_id,
    recall_binding=EMPTY_RECALL_BINDING,
  )
  register_active_sink(chat.id, sink)
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=run_id,
    expires_delta=timedelta(minutes=5),
  )
  try:
    saved = client.post(
      f"/api/chats/{chat.id}/restart-request", json={},
      headers={"Authorization": f"Bearer {token}"},
    )
    assert saved.status_code == 200, saved.text
    qid = saved.json()["question_id"]
    with SessionLocal() as read:
      row = read.get(models.Chat, chat.id)
      block = row.messages[-1]["blocks"][-1]
      restart_id = block["platform_action"]["restart_option_id"]
    body = {
      "content": "", "hidden": True, "question_id": qid,
      "selected_options": {"restart": [restart_id]},
    }
    first = client.post(
      f"/api/chats/{chat.id}/messages", json=body, headers=auth,
    )
    retry = client.post(
      f"/api/chats/{chat.id}/messages", json=body, headers=auth,
    )
  finally:
    unregister_active_sink(chat.id, sink)

  assert first.status_code == 202, first.text
  assert first.json()["answer_turn"] == "none"
  assert retry.status_code == 202, retry.text
  assert calls == ["restart"]


def test_activation_delivery_repairs_committed_but_unscheduled_run(
  monkeypatch,
):
  from app import chat as chat_mod, chat_waits

  _qid, wait_id, _root_id, _requirement = _install(
    "restart-repair", status="met", queued=True,
  )
  attempts = []

  def schedule(**kwargs):
    attempts.append(kwargs["run_token"])
    if len(attempts) == 1:
      chat_mod.discard_starting(kwargs["chat_id"])
      return False
    return True

  monkeypatch.setattr(chat_mod, "_schedule_continuation", schedule)
  assert asyncio.run(chat_waits._deliver_resume(wait_id)) is False
  assert asyncio.run(chat_waits._deliver_resume(wait_id)) is True

  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-repair")
    assert [m["content"] for m in chat.pending_messages] == ["B"]
    assert sum(
      m.get("cid") == f"activation-result-{wait_id}"
      for m in chat.messages
    ) == 1
    assert db.query(models.ChatRun).filter(
      models.ChatRun.id == f"activation-resume-{wait_id}",
    ).count() == 1
    assert db.get(models.ChatWait, wait_id).resume_delivered_at is not None
  assert attempts == [
    f"activation-resume-{wait_id}", f"activation-resume-{wait_id}",
  ]


def test_activation_attaches_to_same_root_recovery_without_duplicate_run():
  from app.chat_writer import StartContinuationAttached

  _qid, wait_id, root_id, _requirement = _install(
    "restart-attached", status="met", queued=True,
  )
  with SessionLocal() as db:
    source = db.get(models.ChatRun, root_id)
    source.status = "running"
    source.ended_at = None
    wait = db.get(models.ChatWait, wait_id)
    content = activation_notice(wait, "met")
    db.commit()

  result = _submit(StartContinuation(
    chat_id="restart-attached", activation_attach_run_token=root_id,
    run_token=f"activation-resume-{wait_id}", root_run_id=root_id,
    content=content, cid=f"activation-result-{wait_id}",
    reason="wait_result", message_kind="platform_activation_result",
    source_work_id=root_id, hidden=True, activation_wait_id=wait_id,
  ))
  assert isinstance(result, StartContinuationAttached)
  with SessionLocal() as db:
    assert db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == "restart-attached",
    ).count() == 1
    chat = db.get(models.Chat, "restart-attached")
    assert [m["content"] for m in chat.pending_messages] == ["B"]
    assert chat.pending_question_id is None


def test_one_boot_fans_out_to_matching_goals_and_leaves_nonmatch_pending(
  monkeypatch,
):
  from app import chat_start, chat_waits

  _q1, w1, r1, requirement = _install(
    "restart-goal-one", action_id="platform-restart:shared",
  )
  _q2, w2, r2, _ = _install(
    "restart-goal-two", action_id="platform-restart:shared",
  )
  _q3, w3, _r3, other = _install(
    "restart-goal-other", action_id="platform-restart:other",
  )
  other["files"]["backend/app/example.py"]["sha256"] = "c" * 64
  with SessionLocal() as db:
    third = db.get(models.ChatWait, w3)
    third.condition_json = other
    card = db.get(models.Chat, "restart-goal-other").messages
    card[0]["blocks"][0]["platform_action"]["requirement"] = other
    db.get(models.Chat, "restart-goal-other").messages = card
    created = max(db.get(models.ChatWait, wid).created_at for wid in (w1, w2, w3))
    db.add(models.PlatformBootSnapshot(
      boot_id="boot-shared", source_kind="platform", source_sha="a" * 40,
      loaded_files_json=requirement["files"], service_ready=True,
      captured_at=created + timedelta(seconds=1),
    ))
    db.commit()

  assert asyncio.run(chat_waits._check_one(w1)) is True
  assert asyncio.run(chat_waits._check_one(w2)) is True
  assert asyncio.run(chat_waits._check_one(w3)) is False
  calls = []

  async def capture_start(**kwargs):
    calls.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start, "start_programmatic_chat_continuation", capture_start,
  )
  assert asyncio.run(chat_waits._deliver_resume(w1)) is True
  assert asyncio.run(chat_waits._deliver_resume(w2)) is True
  assert {(call["chat_id"], call["root_run_id"]) for call in calls} == {
    ("restart-goal-one", r1), ("restart-goal-two", r2),
  }
  with SessionLocal() as db:
    assert db.get(models.ChatWait, w1).resume_delivered_at is not None
    assert db.get(models.ChatWait, w2).resume_delivered_at is not None
    assert db.get(models.ChatWait, w3).status == "armed"
