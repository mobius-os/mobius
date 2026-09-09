"""Only an exact, never-admitted current run can cross provider entry."""

from datetime import UTC, datetime

import pytest

from app import models
from app.chat_writer import (
  AcknowledgePeerContextDelivery,
  AdmitProviderExecution,
  StartTurn,
  _PersistFailed,
  get_writer,
)


def _start(chat_id, token):
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=token,
    user_msg={"role": "user", "content": "Do work", "ts": 1},
  )).result(timeout=5)


@pytest.mark.parametrize("ineligible", ["foreign", "missing", "superseded", "completed", "admitted", "legacy"])
def test_admission_rejects_ineligible_physical_identity(chat, db, ineligible):
  token = "admission-run"
  _start(chat.id, token)
  command = AdmitProviderExecution(chat_id=chat.id, run_token=token)
  if ineligible == "foreign":
    command.chat_id = "different-chat"
  elif ineligible == "missing":
    command.run_token = "missing-run"
  elif ineligible == "superseded":
    _start(chat.id, "new-run")
  else:
    run = db.get(models.ChatRun, token)
    if ineligible == "completed":
      run.status = "completed"
    else:
      run.provider_execution_admitted = True if ineligible == "admitted" else None
    db.commit()
  with pytest.raises(_PersistFailed, match="not eligible"):
    get_writer().submit(command).result(timeout=5)
  db.expire_all()
  if ineligible == "superseded":
    assert db.get(models.ChatRun, "new-run").provider_execution_admitted is False


def test_admission_is_a_one_way_commit_before_provider_entry(chat, db):
  token = "admission-once"
  _start(chat.id, token)
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)
  db.expire_all()
  run = db.get(models.ChatRun, token)
  assert run.provider_execution_admitted is True
  assert run.peer_message_delivery_pending is False
  assert run.peer_message_through_created_at is None
  assert run.peer_message_through_id is None
  with pytest.raises(_PersistFailed, match="not eligible"):
    get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=token)).result(timeout=5)


def test_peer_delivery_acknowledges_only_an_admitted_run(chat, db):
  token = "admission-peer-delivery"
  _start(chat.id, token)
  delivered_at = datetime.now(UTC).replace(tzinfo=None)
  command = AcknowledgePeerContextDelivery(
    chat_id=chat.id,
    run_token=token,
    peer_message_through_created_at=delivered_at,
    peer_message_through_id="peer-note-12",
  )
  with pytest.raises(_PersistFailed, match="admitted run not found"):
    get_writer().submit(command).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)
  get_writer().submit(AcknowledgePeerContextDelivery(
    chat_id=chat.id,
    run_token=token,
    peer_message_through_created_at=delivered_at,
    peer_message_through_id="peer-note-12",
  )).result(timeout=5)

  db.expire_all()
  run = db.get(models.ChatRun, token)
  assert run.peer_message_through_created_at == delivered_at
  assert run.peer_message_through_id == "peer-note-12"
  assert run.peer_message_delivery_pending is False


def test_peer_delivery_rejects_incomplete_cursor(chat, db):
  token = "admission-incomplete-peer-cursor"
  _start(chat.id, token)
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id,
    run_token=token,
    has_peer_context_delivery=True,
  )).result(timeout=5)

  with pytest.raises(_PersistFailed, match="cursor is incomplete"):
    get_writer().submit(AcknowledgePeerContextDelivery(
      chat_id=chat.id,
      run_token=token,
      peer_message_through_id="peer-note-without-time",
    )).result(timeout=5)

  db.expire_all()
  run = db.get(models.ChatRun, token)
  assert run.provider_execution_admitted is True
  assert run.peer_message_delivery_pending is True
  assert run.peer_message_through_created_at is None
  assert run.peer_message_through_id is None
