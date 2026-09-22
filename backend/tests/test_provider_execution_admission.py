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


def test_provider_success_ack_heals_older_provider_limit(chat, db):
  from app.models import ProviderAvailability

  token = "availability-provider-success"
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

  get_writer().submit(AcknowledgeProviderSuccess(
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)

  db.expire_all()
  assert db.get(ProviderAvailability, "claude") is None


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
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)

  db.expire_all()
  assert db.get(ProviderAvailability, "claude") is not None


def test_goal_admission_captures_the_current_plan_revision(chat, db):
  token = "goal-admission"
  _start(chat.id, token)
  run = db.get(models.ChatRun, token)
  run.goal_id = token
  run.goal_objective = "Finish the work"
  run.goal_plan_json = {
    "version": 1,
    "tasks": [{
      "id": "finish", "title": "Finish", "status": "running",
      "depends_on": [],
    }],
  }
  run.goal_plan_revision = 3
  persist_goal_fixture(db, run)
  db.commit()

  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token=token,
  )).result(timeout=5)

  db.expire_all()
  assert db.get(
    models.ChatRun, token,
  ).goal_plan_revision_at_admission == 3


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
