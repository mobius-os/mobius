"""Only an exact, never-admitted current run can cross provider entry."""

from datetime import UTC, datetime, timedelta

import pytest

from app import models
from tests.goal_fixtures import persist_goal_fixture
from app.chat_writer import (
  AcknowledgeProviderSuccess,
  AdmitProviderExecution,
  FinishRun,
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


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "stopped"])
def test_terminal_finish_never_stands_in_for_provider_success(
  chat, db, terminal_status,
):
  from app.models import ProviderAvailability

  token = f"availability-{terminal_status}"
  chat.provider = "claude"
  db.commit()
  _start(chat.id, token)
  run = db.get(models.ChatRun, token)
  run.started_at = datetime.now(UTC).replace(tzinfo=None)
  db.add(ProviderAvailability(
    provider="claude",
    limited_until=run.started_at + timedelta(hours=1),
    unavailable_reason="usage_limit",
    updated_at=run.started_at - timedelta(minutes=1),
  ))
  db.commit()
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)

  get_writer().submit(FinishRun(
    chat_id=chat.id,
    run_token=token,
    terminal_status=terminal_status,
  )).result(timeout=5)
  db.expire_all()
  assert db.get(ProviderAvailability, "claude") is not None


@pytest.mark.parametrize("provider_responded", [True, False])
def test_provider_success_ack_heals_older_limit_only_with_a_response(
  chat, db, provider_responded,
):
  """Receipt of submitted input and quota healing are separate facts.

  A Stop after submission but before any provider output still delivered the
  carried peer note; only an observed provider response proves the older
  quota observation stale.
  """
  from app.models import ProviderAvailability

  token = f"availability-provider-success-{provider_responded}"
  chat.provider = "claude"
  db.commit()
  _start(chat.id, token)
  run = db.get(models.ChatRun, token)
  run.started_at = datetime.now(UTC).replace(tzinfo=None)
  db.add(ProviderAvailability(
    provider="claude",
    limited_until=run.started_at + timedelta(hours=1),
    unavailable_reason="usage_limit",
    updated_at=run.started_at - timedelta(minutes=1),
  ))
  db.commit()
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token=token, has_peer_context_delivery=True,
  )).result(timeout=5)
  delivered_at = datetime.now(UTC).replace(tzinfo=None)

  get_writer().submit(AcknowledgeProviderSuccess(
    chat_id=chat.id, run_token=token,
    provider_responded=provider_responded,
    peer_message_through_created_at=delivered_at,
    peer_message_through_id="peer-note-carried",
  )).result(timeout=5)

  db.expire_all()
  run = db.get(models.ChatRun, token)
  assert run.peer_message_through_id == "peer-note-carried"
  assert run.peer_message_delivery_pending is False
  assert (db.get(ProviderAvailability, "claude") is None) is provider_responded


def test_older_success_cannot_clear_a_newer_overlapping_limit(chat, db):
  from app.models import ProviderAvailability

  token = "availability-newer-overlap"
  chat.provider = "claude"
  db.commit()
  _start(chat.id, token)
  run = db.get(models.ChatRun, token)
  run.started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
  db.add(ProviderAvailability(
    provider="claude",
    limited_until=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
    unavailable_reason="usage_limit",
    updated_at=run.started_at + timedelta(minutes=1),
  ))
  db.commit()
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)
  get_writer().submit(AcknowledgeProviderSuccess(
    chat_id=chat.id, run_token=token, provider_responded=True,
  )).result(timeout=5)

  db.expire_all()
  assert db.get(ProviderAvailability, "claude") is not None


def test_peer_delivery_acknowledges_only_an_admitted_run(chat, db):
  token = "admission-peer-delivery"
  _start(chat.id, token)
  delivered_at = datetime.now(UTC).replace(tzinfo=None)
  command = AcknowledgeProviderSuccess(
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
  get_writer().submit(AcknowledgeProviderSuccess(
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
    get_writer().submit(AcknowledgeProviderSuccess(
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
