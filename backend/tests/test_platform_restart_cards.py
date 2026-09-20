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
  AppendRestartFeedback,
  AnswerQuestion,
  CancelActivationWaits,
  ResolvePlatformRestartCard,
  StartContinuation,
  get_writer,
)
from app.database import SessionLocal
from app.platform_restart import activation_notice
from app.memory_recall import EMPTY_RECALL_BINDING
from app.routes import chats_stream
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


def _install(
  chat_id, *, action_id="platform-restart:test", target_sha=None,
  status="armed", queued=False,
):
  now = now_naive_utc()
  question_id = f"question-{chat_id}"
  wait_id = f"wait-{chat_id}"
  run_id = f"run-{chat_id}"
  requirement = _requirement(action_id)
  if target_sha is not None:
    requirement["target_sha"] = target_sha
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


def test_restart_press_dispatches_once_and_retry_is_idempotent():
  q1, w1, _r1, _requirement = _install("restart-one")

  first = _submit(ResolvePlatformRestartCard(
    chat_id="restart-one", question_id=q1, selected_option_id="restart-id",
  ))
  retry = _submit(ResolvePlatformRestartCard(
    chat_id="restart-one", question_id=q1, selected_option_id="restart-id",
  ))

  # Pressing restart always dispatches; an identical retry of the same settled
  # card does not. The one-actual-restart-per-worker guarantee lives in
  # restart_util's in-process admission latch, not a durable claim row.
  assert first["dispatch"] is True
  assert first["status"] == "restart_requested"
  assert retry["dispatch"] is False
  with SessionLocal() as db:
    assert db.get(models.ChatWait, w1).action_approved_at is not None


def test_free_text_cannot_claim_restart_but_post_stop_button_still_does():
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
  assert _submit(CancelActivationWaits(chat_id="restart-guard")) == 1
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-guard")
    wait = db.get(models.ChatWait, wait_id)
    card = chat.messages[0]["blocks"][0]
    assert wait.status == "cancelled"
    assert chat.pending_question_id is None
    assert card["platform_action"]["status"] == "awaiting_owner"

  result = _submit(ResolvePlatformRestartCard(
    chat_id="restart-guard", question_id=qid,
    selected_option_id="restart-id",
  ))
  assert result["dispatch"] is True
  assert result["status"] == "restart_requested"


def test_written_restart_response_atomically_closes_wait_and_queues_feedback():
  qid, wait_id, _run, _requirement = _install("restart-feedback")
  result = _submit(AppendRestartFeedback(
    chat_id="restart-feedback", question_id=qid,
    answers={"Restart?": "Please check the rollout first"},
    user_msg={
      "role": "user", "content": "Please check the rollout first",
      "hidden": True, "cid": "restart-feedback-answer",
      "kind": "continuation", "continuation_reason": "question_answer",
      "ts": 3,
    },
  ))

  assert result["platform_action"]["status"] == "responded"
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback")
    wait = db.get(models.ChatWait, wait_id)
    card = chat.messages[0]["blocks"][0]
    assert wait.status == "cancelled"
    assert wait.cancelled_at is not None
    assert chat.pending_question_id is None
    assert card["answers"] == {"Restart?": "Please check the rollout first"}
    assert card["selected_options"] == {}
    assert card["platform_action"]["status"] == "responded"
    assert [row["cid"] for row in chat.pending_messages] == [
      "restart-feedback-answer",
    ]


def test_written_restart_response_retries_only_the_same_message_identity():
  qid, _wait_id, _run, _requirement = _install("restart-feedback-identity")
  answer = {"Restart?": "Please check the rollout first"}
  original = {
    "role": "user", "content": answer["Restart?"], "hidden": True,
    "cid": "restart-feedback-original", "kind": "continuation",
    "continuation_reason": "question_answer", "ts": 3,
  }

  first = _submit(AppendRestartFeedback(
    chat_id="restart-feedback-identity", question_id=qid,
    answers=answer, user_msg=original,
  ))
  retry = _submit(AppendRestartFeedback(
    chat_id="restart-feedback-identity", question_id=qid,
    answers=answer, user_msg=original,
  ))

  assert first["duplicate"] is False
  assert retry["duplicate"] is True
  with pytest.raises(chat_writer.RestartCardStateChanged):
    _submit(AppendRestartFeedback(
      chat_id="restart-feedback-identity", question_id=qid,
      answers=answer,
      user_msg={**original, "cid": "restart-feedback-other-tab", "ts": 4},
    ))

  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-identity")
    assert [row["cid"] for row in chat.pending_messages] == [
      "restart-feedback-original",
    ]


def test_existing_unrelated_cid_cannot_false_acknowledge_an_open_restart_card():
  qid, wait_id, _run, _requirement = _install("restart-feedback-cid-collision")
  collision = {
    "role": "user", "content": "Older unrelated message",
    "cid": "restart-feedback-collision", "ts": 1,
  }
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-cid-collision")
    chat.messages = [collision, *chat.messages]
    db.commit()

  with pytest.raises(chat_writer.RestartCardActionConflict):
    _submit(AppendRestartFeedback(
      chat_id="restart-feedback-cid-collision", question_id=qid,
      answers={"Restart?": "Please check the rollout first"},
      user_msg={
        "role": "user", "content": "Please check the rollout first",
        "hidden": True, "cid": collision["cid"], "kind": "continuation",
        "continuation_reason": "question_answer", "ts": 3,
      },
    ))

  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-cid-collision")
    wait = db.get(models.ChatWait, wait_id)
    card = chat.messages[-1]["blocks"][0]
    assert wait.status == "armed"
    assert chat.pending_question_id == qid
    assert not card.get("answers")
    assert chat.pending_messages == []


def test_promoted_written_restart_retry_is_acknowledged_without_a_new_turn(
  client, auth,
):
  qid, _wait_id, _run, _requirement = _install("restart-feedback-promoted")
  answer = {"Restart?": "Please check the rollout first"}
  original = {
    "role": "user", "content": answer["Restart?"], "hidden": True,
    "cid": "restart-feedback-promoted-cid", "kind": "continuation",
    "continuation_reason": "question_answer", "ts": 3,
  }
  _submit(AppendRestartFeedback(
    chat_id="restart-feedback-promoted", question_id=qid,
    answers=answer, user_msg=original,
  ))
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-promoted")
    chat.pending_messages = []
    chat.messages = [*chat.messages, original]
    db.commit()

  response = client.post(
    "/api/chats/restart-feedback-promoted/messages", headers=auth, json={
      "content": answer["Restart?"], "hidden": True,
      "answers": answer, "question_id": qid,
      "selected_options": {}, "cid": original["cid"],
    },
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "duplicate"
  assert response.json()["answer_turn"] == "none"
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-promoted")
    assert chat.pending_messages == []
    assert [row.get("cid") for row in chat.messages].count(original["cid"]) == 1


def test_queued_written_restart_retry_is_acknowledged_while_turn_is_running(
  client, auth, monkeypatch,
):
  qid, _wait_id, _run, _requirement = _install("restart-feedback-running")
  answer = {"Restart?": "Please check the rollout first"}
  body = {
    "content": answer["Restart?"], "hidden": True,
    "answers": answer, "question_id": qid,
    "selected_options": {}, "cid": "restart-feedback-running-cid",
  }
  monkeypatch.setattr(chats_stream, "is_chat_running", lambda _chat_id: True)

  first = client.post(
    "/api/chats/restart-feedback-running/messages", headers=auth, json=body,
  )
  retry = client.post(
    "/api/chats/restart-feedback-running/messages", headers=auth, json=body,
  )

  assert first.status_code == 202, first.text
  assert first.json()["status"] == "queued"
  assert retry.status_code == 202, retry.text
  assert retry.json()["status"] == "duplicate"
  assert retry.json()["answer_turn"] == "none"
  assert retry.json()["running"] is True
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-running")
    assert [row["cid"] for row in chat.pending_messages] == [body["cid"]]


def test_same_written_restart_text_from_another_tab_refreshes_stale_card(
  client, auth, monkeypatch,
):
  qid, _wait_id, _run, _requirement = _install("restart-feedback-other-tab")
  answer = {"Restart?": "Please check the rollout first"}
  monkeypatch.setattr(chats_stream, "_selected_model_for_chat", lambda _chat: None)

  first = client.post(
    "/api/chats/restart-feedback-other-tab/messages", headers=auth, json={
      "content": answer["Restart?"], "hidden": True,
      "answers": answer, "question_id": qid,
      "selected_options": {}, "cid": "restart-feedback-first-tab",
    },
  )

  assert first.status_code == 202, first.text
  assert first.json()["status"] == "queued"

  response = client.post(
    "/api/chats/restart-feedback-other-tab/messages", headers=auth, json={
      "content": answer["Restart?"], "hidden": True,
      "answers": answer, "question_id": qid,
      "selected_options": {}, "cid": "restart-feedback-second-tab",
    },
  )

  assert response.status_code == 410, response.text
  assert response.json()["detail"]["code"] == "question_state_changed"
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-other-tab")
    assert [row["cid"] for row in chat.pending_messages] == [
      "restart-feedback-first-tab",
    ]


def test_stale_written_restart_response_has_a_state_changed_outcome():
  qid, _wait_id, _run, _requirement = _install("restart-feedback-stale")
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-stale")
    chat.pending_question_id = None
    db.commit()

  with pytest.raises(chat_writer.RestartCardStateChanged):
    _submit(AppendRestartFeedback(
      chat_id="restart-feedback-stale", question_id=qid,
      answers={"Restart?": "Please check the rollout first"},
      user_msg={
        "role": "user", "content": "Please check the rollout first",
        "hidden": True, "cid": "restart-feedback-stale-answer",
        "kind": "continuation", "continuation_reason": "question_answer",
        "ts": 3,
      },
    ))

  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-feedback-stale")
    assert chat.pending_messages == []


def test_restart_activation_wait_does_not_expire_while_owner_is_deciding(monkeypatch):
  from app import chat_waits

  _qid, wait_id, _run, _requirement = _install("restart-no-expiry")
  past = now_naive_utc() - timedelta(seconds=1)
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    wait.deadline_at = past
    wait.next_check_at = past
    db.commit()
  monkeypatch.setattr(
    "app.platform_restart.activation_wait_verdict",
    lambda _db, _wait: ("pending", ""),
  )

  assert asyncio.run(chat_waits._check_one(wait_id)) is False
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    assert wait.status == "armed"
    assert wait.next_check_at > now_naive_utc()


def test_restart_wait_hides_storage_deadline_from_owner_and_agent_context():
  from app.chat_waits import build_active_waits_context, serialize_wait

  _qid, wait_id, _run, _requirement = _install("restart-no-visible-expiry")
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    assert wait.deadline_at is not None  # Legacy shared-schema storage only.
    assert serialize_wait(wait)["deadline_at"] is None
    assert '"deadline_at":null' in build_active_waits_context(
      db, "restart-no-visible-expiry",
    )


def test_generic_wait_cancel_cannot_strand_an_open_restart_card(client, auth):
  qid, wait_id, _run, _requirement = _install("restart-cancel-boundary")

  response = client.post(f"/api/chat-waits/{wait_id}/cancel", headers=auth)

  assert response.status_code == 409, response.text
  assert "Restart card" in response.json()["detail"]
  with SessionLocal() as db:
    chat = db.get(models.Chat, "restart-cancel-boundary")
    wait = db.get(models.ChatWait, wait_id)
    assert chat.pending_question_id == qid
    assert wait.status == "armed"


def test_generic_wait_cancel_cannot_desynchronize_a_deferred_legacy_card(
  client, auth,
):
  qid, wait_id, _run, _requirement = _install("restart-deferred-boundary")
  result = _submit(ResolvePlatformRestartCard(
    chat_id="restart-deferred-boundary", question_id=qid,
    selected_option_id="cancel-id",
  ))
  assert result["status"] == "deferred"

  response = client.post(f"/api/chat-waits/{wait_id}/cancel", headers=auth)

  assert response.status_code == 409, response.text
  with SessionLocal() as db:
    assert db.get(models.ChatWait, wait_id).status == "armed"


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


def test_not_now_defers_execution_without_abandoning_activation():
  qid, wait_id, _run, _requirement = _install("restart-cancel")
  result = _submit(ResolvePlatformRestartCard(
    chat_id="restart-cancel", question_id=qid,
    selected_option_id="cancel-id",
  ))
  assert result["dispatch"] is False
  assert result["status"] == "deferred"
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    assert wait.status == "armed"
    assert wait.action_approved_at is None


def test_route_dispatches_platform_restart_once_without_an_answer_turn(
  client, chat, auth, db, monkeypatch,
):
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-route")
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
      assert block["platform_action"]["version"] == 2
      assert [option["label"] for option in block["questions"][0]["options"]] == [
        "Restart now",
      ]
      assert "cancel_option_id" not in block["platform_action"]
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


def test_route_sends_written_restart_feedback_without_restart_authority(
  client, chat, auth, db, monkeypatch,
):
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-feedback-route")
  run_id = f"request-feedback-{chat.id}"
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
      prompt = block["questions"][0]["question"]
      wait_id = block["platform_action"]["wait_id"]
    response = client.post(
      f"/api/chats/{chat.id}/messages", headers=auth, json={
        "content": f"- {prompt}: Please run the broader checks first",
        "hidden": True,
        "answers": {prompt: "Please run the broader checks first"},
        "question_id": qid,
        "selected_options": {},
        "cid": "restart-feedback-route-answer",
      },
    )
  finally:
    unregister_active_sink(chat.id, sink)

  assert response.status_code == 202, response.text
  assert response.json()["answer_turn"] == "queued"
  assert response.json()["platform_action"]["status"] == "responded"
  with SessionLocal() as read:
    row = read.get(models.Chat, chat.id)
    wait = read.get(models.ChatWait, wait_id)
    assert wait.status == "cancelled"
    assert row.pending_question_id is None
    assert [item["cid"] for item in row.pending_messages] == [
      "restart-feedback-route-answer",
    ]


def test_stale_restart_route_signals_authoritative_card_refresh(
  client, chat, auth, monkeypatch,
):
  from concurrent.futures import Future
  from app.chat_writer import RestartCardStateChanged

  requirement = _requirement("platform-restart:stale-route")
  block = _card("stale-restart-card", "stale-wait", requirement)
  monkeypatch.setattr(
    "app.platform_restart.restart_action_block", lambda _chat, _qid: block,
  )

  class RefusingWriter:
    def submit(self, _command):
      result = Future()
      result.set_exception(RestartCardStateChanged("card already settled"))
      return result

  monkeypatch.setattr(chats_stream, "get_writer", lambda: RefusingWriter())
  response = client.post(
    f"/api/chats/{chat.id}/messages", headers=auth, json={
      "content": "", "hidden": True,
      "question_id": "stale-restart-card",
      "selected_options": {"restart": ["restart-id"]},
    },
  )

  assert response.status_code == 410, response.text
  assert response.json()["detail"]["code"] == "question_state_changed"


def test_restart_route_keeps_transient_writer_failure_retryable(
  client, chat, auth, monkeypatch,
):
  from concurrent.futures import Future

  requirement = _requirement("platform-restart:writer-failure")
  block = _card("writer-failure-card", "writer-failure-wait", requirement)
  monkeypatch.setattr(
    "app.platform_restart.restart_action_block", lambda _chat, _qid: block,
  )

  class FailingWriter:
    def submit(self, _command):
      result = Future()
      result.set_exception(RuntimeError("database temporarily unavailable"))
      return result

  monkeypatch.setattr(chats_stream, "get_writer", lambda: FailingWriter())
  response = client.post(
    f"/api/chats/{chat.id}/messages", headers=auth, json={
      "content": "", "hidden": True,
      "question_id": "writer-failure-card",
      "selected_options": {"restart": ["restart-id"]},
    },
  )

  assert response.status_code == 503, response.text
  assert "try again" in response.json()["detail"].lower()


def test_stale_written_restart_feedback_refreshes_instead_of_ghost_queuing(
  client, chat, auth, monkeypatch,
):
  from concurrent.futures import Future

  requirement = _requirement("platform-restart:stale-written-route")
  block = _card("stale-written-card", "stale-written-wait", requirement)
  monkeypatch.setattr(
    "app.platform_restart.restart_action_block", lambda _chat, _qid: block,
  )

  class RefusingWriter:
    def submit(self, _command):
      result = Future()
      result.set_exception(
        chat_writer.RestartCardStateChanged("card already settled")
      )
      return result

  monkeypatch.setattr(chats_stream, "get_writer", lambda: RefusingWriter())
  response = client.post(
    f"/api/chats/{chat.id}/messages", headers=auth, json={
      "content": "- Restart?: Please check the rollout first",
      "hidden": True,
      "answers": {"Restart?": "Please check the rollout first"},
      "question_id": "stale-written-card",
      "selected_options": {},
      "cid": "stale-written-feedback",
    },
  )

  assert response.status_code == 410, response.text
  assert response.json()["detail"]["code"] == "question_state_changed"


def test_written_restart_feedback_keeps_transient_failure_retryable(
  client, chat, auth, monkeypatch,
):
  from concurrent.futures import Future

  requirement = _requirement("platform-restart:written-writer-failure")
  block = _card("written-failure-card", "written-failure-wait", requirement)
  monkeypatch.setattr(
    "app.platform_restart.restart_action_block", lambda _chat, _qid: block,
  )

  class FailingWriter:
    def submit(self, _command):
      result = Future()
      result.set_exception(RuntimeError("database temporarily unavailable"))
      return result

  monkeypatch.setattr(chats_stream, "get_writer", lambda: FailingWriter())
  response = client.post(
    f"/api/chats/{chat.id}/messages", headers=auth, json={
      "content": "- Restart?: Please check the rollout first",
      "hidden": True,
      "answers": {"Restart?": "Please check the rollout first"},
      "question_id": "written-failure-card",
      "selected_options": {},
      "cid": "written-failure-feedback",
    },
  )

  assert response.status_code == 503, response.text
  assert "try again" in response.json()["detail"].lower()


def test_failed_restart_wait_still_honors_exact_restart_button(
  client, auth, monkeypatch,
):
  calls = []

  async def captured_restart(*_args, **_kwargs):
    calls.append("restart")

  monkeypatch.setattr("app.restart_util.restart_this_worker", captured_restart)
  qid, _wait_id, _run, _requirement = _install(
    "restart-failed-wait", status="failed",
  )

  response = client.post(
    "/api/chats/restart-failed-wait/messages", headers=auth, json={
      "content": "", "hidden": True, "question_id": qid,
      "selected_options": {"restart": ["restart-id"]},
    },
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "restart_requested"
  assert response.json()["answer_turn"] == "none"
  assert calls == ["restart"]


def test_idle_written_restart_feedback_starts_exactly_one_continuation(
  client, auth, monkeypatch,
):
  """The ordinary post-turn card path must not leave feedback queued idle."""
  chat_id = "restart-feedback-idle"
  qid, wait_id, _run, _requirement = _install(chat_id)
  scheduled = []
  monkeypatch.setattr(
    chats_stream, "_selected_model_for_chat", lambda _chat: "test-model",
  )
  monkeypatch.setattr(
    chats_stream, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  try:
    response = client.post(
      f"/api/chats/{chat_id}/messages", headers=auth, json={
        "content": "- Restart?: Please run the broader checks first",
        "hidden": True,
        "answers": {"Restart?": "Please run the broader checks first"},
        "question_id": qid,
        "selected_options": {},
        "cid": "restart-feedback-idle-answer",
      },
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "started"
    assert response.json()["answer_turn"] == "new"
    assert response.json()["message"]["cid"] == "restart-feedback-idle-answer"
    assert len(scheduled) == 1
    assert scheduled[0]["next_user"]["cid"] == "restart-feedback-idle-answer"
    with SessionLocal() as read:
      row = read.get(models.Chat, chat_id)
      wait = read.get(models.ChatWait, wait_id)
      assert wait.status == "cancelled"
      assert row.pending_question_id is None
      assert row.pending_messages == []
      assert row.messages[-1]["cid"] == "restart-feedback-idle-answer"
  finally:
    from app.chat import discard_starting
    discard_starting(chat_id)


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


def test_one_ready_boot_wakes_every_linked_restart_goal(
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
  assert asyncio.run(chat_waits._check_one(w3)) is True
  calls = []

  async def capture_start(**kwargs):
    calls.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start, "start_programmatic_chat_continuation", capture_start,
  )
  assert asyncio.run(chat_waits._deliver_resume(w1)) is True
  assert asyncio.run(chat_waits._deliver_resume(w2)) is True
  assert asyncio.run(chat_waits._deliver_resume(w3)) is True
  assert {(call["chat_id"], call["root_run_id"]) for call in calls} == {
    ("restart-goal-one", r1), ("restart-goal-two", r2),
    ("restart-goal-other", _r3),
  }
  with SessionLocal() as db:
    assert db.get(models.ChatWait, w1).resume_delivered_at is not None
    assert db.get(models.ChatWait, w2).resume_delivered_at is not None
    assert db.get(models.ChatWait, w3).resume_delivered_at is not None
