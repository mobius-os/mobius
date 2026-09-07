"""A finished modern Wait must respect the chat's existing recovery owner."""

import asyncio
from datetime import timedelta
import sys

import pytest

from app import auth as auth_mod, chat as chat_mod, chat_waits, models
from app.broadcast import create_broadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.chat_writer import FinishRun, StartTurn, get_writer
from app.memory_recall import EMPTY_RECALL_BINDING
from app.timeutil import now_naive_utc


def _finished_wait(db, chat_id, source):
  row = chat_waits.declare_wait(
    db, chat_id=chat_id, created_by_run_id=source,
    description="Resume when the gate settles", kind="timer", delay_secs=60,
  )
  row.status = "met"
  row.met_at = now_naive_utc()
  db.commit()
  return row


def _assert_queued_once(db, chat_id, row):
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  assert [message["cid"] for message in chat.pending_messages] == [
    f"wait-result-{row.id}",
  ]
  assert db.get(models.ChatRun, f"wait-resume-{row.id}") is None
  assert db.get(models.ChatWait, row.id).resume_delivered_at is None


def test_modern_wait_queues_behind_manual_restart_hold(client, chat, db, monkeypatch):
  source = "wait-before-restart"
  now = now_naive_utc()
  db.add_all([
    models.ChatRun(id=source, root_run_id=source, chat_id=chat.id,
                   status="completed", provider="claude", started_at=now - timedelta(minutes=2)),
    models.ChatRun(id="restart-manual-hold", root_run_id="restart-manual-hold",
                   chat_id=chat.id, status="interrupted", park_reason="restart",
                   provider="claude", started_at=now - timedelta(minutes=1)),
  ])
  db.commit()
  row = _finished_wait(db, chat.id, source)
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kwargs: pytest.fail("machine wake bypassed manual Resume"))
  assert asyncio.run(chat_waits._deliver_resume(row.id)) is False
  assert asyncio.run(chat_waits._deliver_resume(row.id)) is False
  _assert_queued_once(db, chat.id, row)
  assert chat_mod.programmatic_start_blocked(db, chat.id)
  assert db.get(models.ChatRun, "restart-manual-hold").status == "interrupted"


@pytest.mark.parametrize("card", ["question", "secure"])
def test_modern_wait_queues_behind_saved_owner_input(client, chat, db, tmp_path, monkeypatch, card):
  source = f"wait-with-{card}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=source,
    user_msg={"role": "user", "content": "Wait for the gate", "ts": 1},
  )).result(timeout=5)
  sink = ChatEventSink(create_broadcast(chat.id), chat.id, run_token=source,
                       recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(chat.id, sink)
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat.id, owner.username, owner.token_epoch, run_id=source,
    expires_delta=timedelta(minutes=5),
  )
  headers = {"Authorization": f"Bearer {token}"}
  try:
    if card == "question":
      response = client.post(f"/api/chats/{chat.id}/question", headers=headers, json={"questions": [{
        "id": "proceed", "header": "Proceed",
        "question": "Proceed with the change?",
        "options": [{"label": "Proceed", "description": "Apply it."},
                    {"label": "Not now", "description": "Leave it pending."}],
      }]})
    else:
      response = client.post(f"/api/secure-inputs/{chat.id}/saved", headers=headers, json={
        "title": "Connect service", "description": "Sealed operation.",
        "fields": [{"name": "api_key", "type": "password", "label": "API key"}],
        "command": [sys.executable, "-c", "pass"], "cwd": str(tmp_path),
        "action": "run", "mode": "sealed",
      })
    assert response.status_code == 200, response.text
    db.expire_all()
    question_id = db.get(models.Chat, chat.id).pending_question_id
    assert question_id
    get_writer().submit(FinishRun(
      chat_id=chat.id, run_token=source, terminal_status="completed",
    )).result(timeout=5)
    row = _finished_wait(db, chat.id, source)
    monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kwargs: pytest.fail("machine wake bypassed saved input"))
    assert asyncio.run(chat_waits._deliver_resume(row.id)) is False
    assert asyncio.run(chat_waits._deliver_resume(row.id)) is False
    _assert_queued_once(db, chat.id, row)
    assert db.get(models.Chat, chat.id).pending_question_id == question_id
    if card == "secure":
      assert db.get(models.SavedSecureInput, question_id).status == "pending"
  finally:
    unregister_active_sink(chat.id, sink)
