"""A warned storage continuation is durable, exact and limited to one turn."""

import asyncio
from datetime import timedelta

import pytest

from app import chat as runtime, models, schemas
from app.agent_admission import require_agent_turn_admission
from app.chat_writer import (
  StartTurn, StartTurnRecoveryChanged, get_writer,
)
from app.continuations import manual_resume_matches
from app.timeutil import now_naive_utc


def park(db, chat, reason="storage"):
  row = models.ChatRun(
    id="waiting-run", chat_id=chat.id, status="parked", provider="claude",
    park_reason=reason, parked_until=now_naive_utc() + timedelta(hours=1),
  )
  db.add(row)
  db.commit()
  return row


def request(chat, **changes):
  return StartTurn(
    chat_id=chat.id, run_token="manual-storage", owner_input=True,
    user_msg={"role": "user", "content": "", "kind": "continuation",
              "continuation_reason": "manual", "cid": "storage-control", "ts": 10},
    resume_run_id="waiting-run", allow_low_storage=True, **changes,
  )


def submit(cmd):
  return get_writer().submit(cmd).result(timeout=5)


def test_storage_continue_survives_receipt_retry_without_consuming_pending_work(db, chat):
  park(db, chat)
  pending = [{"role": "user", "content": "Later work", "cid": "later", "ts": 10}]
  chat.pending_messages = pending
  db.commit()
  result = submit(request(chat))
  assert result["history"][-1].content.startswith("Resume the interrupted")
  db.expire_all()
  control = db.get(models.ChatRun, "manual-storage").continuation_json
  assert control["allow_low_storage"] is True
  assert control["supersedes_run_token"] == "waiting-run"
  assert db.get(models.Chat, chat.id).pending_messages == pending
  assert submit(request(chat))["duplicate"] is True
  changed = request(chat)
  changed.allow_low_storage = False
  assert isinstance(submit(changed), StartTurnRecoveryChanged)


@pytest.mark.parametrize("reason", ["memory", "usage_limit", "restart"])
def test_storage_override_cannot_relabel_another_wait(db, chat, reason):
  park(db, chat, reason)
  assert isinstance(submit(request(chat)), StartTurnRecoveryChanged)
  db.expire_all()
  assert db.get(models.ChatRun, "manual-storage") is None
  assert db.get(models.ChatRun, "waiting-run").status == "parked"


def test_storage_override_requires_owner_intent_and_exact_current_run(db, chat):
  park(db, chat)
  cmd = request(chat)
  cmd.owner_input = False
  assert isinstance(submit(cmd), StartTurnRecoveryChanged)
  cmd = request(chat)
  cmd.resume_run_id = "superseded-run"
  assert isinstance(submit(cmd), StartTurnRecoveryChanged)


def test_saved_question_keeps_authority_over_storage_override(db, chat):
  park(db, chat)
  chat.pending_question_id = "owner-card"
  db.commit()
  assert submit(request(chat)).question_id == "owner-card"
  db.expire_all()
  assert db.get(models.ChatRun, "manual-storage") is None


def test_storage_override_requires_explicit_manual_run_target():
  for fields in ({}, {"continuation": "manual"}, {"resume_run_id": "waiting-run"}):
    with pytest.raises(ValueError, match="exact storage-waiting run"):
      schemas.SendMessage(content="", allow_low_storage=True, **fields)


def test_receipt_cannot_upgrade_an_ordinary_resume_to_a_storage_override():
  control = {"reason": "manual", "control_id": "cid", "supersedes_run_token": "waiting-run"}
  assert not manual_resume_matches(
    control, control_id="cid", run_id="waiting-run", allow_low_storage=True,
  )


def test_owner_continue_route_persists_override_and_rejects_changed_retry(client, auth, db, chat, monkeypatch):
  park(db, chat)
  scheduled = []

  async def capture(*args, **kwargs):
    scheduled.append(kwargs)

  monkeypatch.setattr("app.routes.chats_stream.run_chat", capture)
  body = {"content": "", "cid": "route-control", "continuation": "manual",
          "resume_run_id": "waiting-run", "allow_low_storage": True}
  first = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)
  assert first.status_code == 202, first.text
  retry = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)
  assert retry.status_code == 200 and retry.json()["status"] == "duplicate"
  changed = client.post(f"/api/chats/{chat.id}/messages",
                        json={**body, "allow_low_storage": False}, headers=auth)
  assert changed.status_code == 409
  db.expire_all()
  assert db.get(models.ChatRun, scheduled[0]["run_token"]).continuation_json["allow_low_storage"] is True
  assert len(scheduled) == 1


@pytest.mark.parametrize("reason, allowed", [("manual", True), ("storage", False)])
def test_runtime_reads_permission_from_exact_committed_manual_control(db, chat, monkeypatch, reason, allowed):
  row = models.ChatRun(
    id="admission-run", chat_id=chat.id, status="running", provider="claude",
    continuation_json={"reason": reason, "allow_low_storage": True},
  )
  db.add(row)
  db.commit()
  entered = []

  async def admission(data_dir, *, allow_low_storage=False):
    assert allow_low_storage is allowed
    if not allowed:
      return  # Ordinary admission behavior is covered separately.
    await require_agent_turn_admission(
      data_dir, allow_low_storage=allow_low_storage,
      status_reader=lambda _: {"facts": {"disk": {"free_bytes": 945 * 1024**2}},
        "pressure": {"disk": {"state": "critical", "critical_below_bytes": 1024**3}}},
    )

  async def provider(*args, **kwargs):
    entered.append(kwargs["run_token"])
    return runtime.chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  monkeypatch.setattr(runtime, "require_agent_turn_admission", admission)
  monkeypatch.setattr(runtime, "_run_chat_impl", provider)
  asyncio.run(runtime.run_chat([], chat_id=chat.id, run_token=row.id))
  assert entered == [row.id]


def test_continue_anyway_tells_the_agent_storage_is_still_low():
  from app.continuations import continuation_protocol_source

  plain = continuation_protocol_source(reason="manual", control_id="c", run_token="r")
  warned = continuation_protocol_source(
    reason="manual", control_id="c", run_token="r", allow_low_storage=True,
  )
  assert "disk safety floor" not in plain["content"]
  assert "below the shared disk safety floor" in warned["content"]
  assert "Check free space first" in warned["content"]
