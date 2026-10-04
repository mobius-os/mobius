"""Quiet delivery inherits revocable browser authority, never owner authority."""

import pytest
from fastapi import HTTPException

from app import models, transcript_rows
from app.agent_write_channel import WriteIntent
from app.agent_write_journal import pending_failure_reports
from app.browser_access import create_invitation, revoke_grant
from app.chat_writer import (
  AdmitAgentWrites, AdmitProviderExecution, ClaimAgentWrite, PromotePending,
  SealAgentWrites, SettleAgentWrite, StartTurn, get_writer,
)


def submit(command):
  return get_writer().submit(command).result(timeout=5)


def browser_run(chat, db):
  owner = db.query(models.Owner).first()
  if owner is None:
    owner = models.Owner(username="quiet-browser-owner", hashed_password="unused")
    db.add(owner)
    db.commit()
  grant, _ = create_invitation(db, owner, "quiet browser")
  identity = {"chat_id": chat.id, "run_token": "browser-source"}
  submit(StartTurn(**identity,
    user_msg={"role": "user", "content": "Original task", "ts": 10},
    browser_grant_id=grant.id, browser_grant_epoch=grant.epoch))
  submit(AdmitProviderExecution(**identity))
  return owner, grant, identity


def admit(identity):
  return submit(AdmitAgentWrites(**identity, item_id="item", fingerprint="a" * 64,
    writes=(WriteIntent("save", "checkpoint_chat", {"summary": "Synthetic note"}),)))


def fail(identity):
  admit(identity)
  submit(ClaimAgentWrite(**identity))
  submit(SettleAgentWrite(**identity, operation_id="save", status="failed",
                          reason="Synthetic failure"))
  submit(SealAgentWrites(**identity))


def promote(identity):
  return submit(PromotePending(chat_id=identity["chat_id"], run_token="candidate",
    ending_run_token=identity["run_token"]))


@pytest.mark.parametrize("boundary", ["admit", "claim"])
def test_revocation_fences_quiet_admission_and_claim_but_allows_evidence(chat, db, boundary):
  owner, grant, identity = browser_run(chat, db)
  if boundary == "claim":
    admit(identity)
  revoke_grant(db, grant.id, owner.id)
  with pytest.raises(HTTPException):
    admit(identity) if boundary == "admit" else submit(ClaimAgentWrite(**identity))
  db.expire_all()
  intent = db.get(models.AgentWriteIntent, (identity["run_token"], "save"))
  if boundary == "admit":
    assert intent is None
  else:
    assert intent.status == "queued"
  # Sealing and late evidence do not start effects or renew execution permission.
  submit(SealAgentWrites(**identity))


def test_late_effect_result_survives_browser_revocation(chat, db):
  owner, grant, identity = browser_run(chat, db)
  admit(identity)
  submit(ClaimAgentWrite(**identity))
  revoke_grant(db, grant.id, owner.id)
  submit(SettleAgentWrite(**identity, operation_id="save", status="succeeded"))
  db.expire_all()
  assert db.get(models.AgentWriteIntent, (identity["run_token"], "save")).status == "succeeded"


@pytest.mark.parametrize("revoked", [False, True])
def test_quiet_repair_preserves_browser_lineage_and_current_grant(chat, db, revoked):
  owner, grant, identity = browser_run(chat, db)
  fail(identity)
  epoch = grant.epoch
  if revoked:
    revoke_grant(db, grant.id, owner.id)
  result = promote(identity)
  db.expire_all()
  if revoked:
    assert result["promoted"] is None
    assert db.query(models.ChatRun).count() == 1
    assert pending_failure_reports(db, chat_id=chat.id, exclude_run_id="future")
  else:
    repair_id = result["promoted"]["_run_token"]
    repair = db.get(models.ChatRun, repair_id)
    assert (repair.browser_grant_id, repair.browser_grant_epoch) == (grant.id, epoch)
    assert repair.root_run_id == identity["run_token"]
    # A grant revoked between repair creation and provider admission still fails.
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(HTTPException):
      submit(AdmitProviderExecution(chat_id=chat.id, run_token=repair_id))
    db.expire_all()
    assert db.get(models.ChatRun, repair_id).provider_execution_admitted is False


@pytest.mark.parametrize("repair_allowed", [False, True])
def test_rejected_queue_rows_survive_empty_drain_and_quiet_repair(chat, db, repair_allowed):
  owner, grant, identity = browser_run(chat, db)
  fail(identity)
  rejected_grant, _ = create_invitation(db, owner, "rejected browser")
  pending = {"role": "user", "content": "Retain rejected content", "cid": "rejected",
    "ts": 20, "_browser_grant_id": rejected_grant.id,
    "_browser_grant_epoch": rejected_grant.epoch}
  db.expire_all()
  db.get(models.Chat, chat.id).pending_messages = [pending]
  db.commit()
  revoke_grant(db, rejected_grant.id, owner.id)
  if not repair_allowed:
    revoke_grant(db, grant.id, owner.id)
  result = promote(identity)
  assert bool(result["promoted"]) == repair_allowed
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == [{**pending, "delivery_status": "rejected",
    "delivery_error": "browser_grant_unavailable"}]
  assert all(message.get("content") != pending["content"] for message in transcript_rows.history(saved))
  assert all(message.content != pending["content"] for message in result["history"])
