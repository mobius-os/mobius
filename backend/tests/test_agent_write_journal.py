"""Quiet-write delivery uses the real single writer, never a second queue DB."""
from app import transcript_rows
from app.chat_writer import create_chat
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

from app import models
from app.agent_write_channel import WriteIntent
from app.chat_writer import (AdmitAgentWrites, ClaimAgentWrite, SettleAgentWrite,
  SealAgentWrites, InterruptAgentWrites, ReadAgentWriteOutcomes, StartTurn,
  FinishRun, ReconcileStartupChat, get_writer)
from app.schema_migrations import _add_agent_write_journal


def submit(command):
  return get_writer().submit(command).result(timeout=5)


def start(chat, token="quiet-test-run"):
  submit(StartTurn(chat_id=chat.id, run_token=token,
    user_msg={"role":"user", "content":"Synthetic test", "ts":10}, title_source="Test"))
  return {"chat_id":chat.id, "run_token":token}


def save(owner, *writes, item="item1", fingerprint="a"*64):
  return submit(AdmitAgentWrites(**owner, item_id=item, fingerprint=fingerprint, writes=tuple(writes)))


def write(id="w1", value=1):
  return WriteIntent(id, "checkpoint_chat", {"summary":value})


def states(owner):
  return [(r["id"], r["status"]) for r in submit(ReadAgentWriteOutcomes(**owner))["writes"]]


def test_admission_commits_before_ack_and_one_worker_claims_in_order(chat, db):
  owner=start(chat)
  assert save(owner, write(), write("w2"))["new_count"]==2
  db.expire_all()
  assert db.query(models.AgentWriteIntent).count()==2
  first=submit(ClaimAgentWrite(**owner))
  assert first["write"]["id"]=="w1"
  assert submit(ClaimAgentWrite(**owner))=={"status":"busy"}
  assert states(owner)==[("w1","executing"),("w2","queued")]
  submit(SettleAgentWrite(**owner, operation_id="w1", status="succeeded"))
  assert submit(ClaimAgentWrite(**owner))["write"]["id"]=="w2"


def test_replay_preserves_failed_and_unknown_effects_without_redispatch(chat):
  owner=start(chat)
  save(owner,write())
  submit(ClaimAgentWrite(**owner))
  submit(SettleAgentWrite(**owner,operation_id="w1",status="unknown",reason="Response lost"))
  assert save(owner,write())["new_count"]==1  # Same immutable admission receipt.
  assert submit(ClaimAgentWrite(**owner))["status"]=="empty"
  assert states(owner)==[("w1","unknown")]
  replay=save(owner,write(),item="another-item")
  assert replay["negative_replays"]==[{"id":"w1","status":"unknown","source_run_id":owner["run_token"]}]
  assert submit(ClaimAgentWrite(**owner))["status"]=="empty"


def test_changed_replay_is_type_sensitive_and_atomic(chat):
  owner=start(chat)
  save(owner,write(value=1))
  assert save(owner,write(value=True),write("w2"))["reason"]=="changed_intent"
  assert states(owner)==[("w1","queued")]
  assert save(owner,write("w3"),fingerprint="b"*64)["reason"]=="changed_item"
  assert states(owner)==[("w1","queued")]


def test_seal_stops_new_admissions_but_existing_work_can_drain(chat):
  owner=start(chat)
  save(owner,write())
  submit(SealAgentWrites(**owner))
  assert save(owner,write("w2"),item="item2")["reason"]=="intake_closed"
  assert submit(ClaimAgentWrite(**owner))["status"]=="claimed"


def test_stop_fences_execution_and_preserves_late_observed_completion(chat):
  owner=start(chat)
  save(owner,write(),write("w2"))
  submit(ClaimAgentWrite(**owner))
  submit(FinishRun(**owner,terminal_status="stopped"))
  assert states(owner)==[("w1","unknown"),("w2","cancelled")]
  assert submit(ClaimAgentWrite(**owner))["status"]=="stale_run"
  assert save(owner,write("w3"))["status"]=="stale_run"
  submit(SettleAgentWrite(**owner,operation_id="w1",status="succeeded"))
  assert states(owner)==[("w1","succeeded"),("w2","cancelled")]


def test_boot_recovery_retains_unknown_outcome_without_renewed_authority(chat, db):
  owner=start(chat)
  save(owner,write(),write("w2"))
  submit(ClaimAgentWrite(**owner))
  db.expire_all()
  submit(ReconcileStartupChat(chat_id=chat.id, messages=list(transcript_rows.history(db.get(models.Chat, chat.id))),
    running_run_ids=(owner["run_token"],),recovered_at=datetime.now(UTC)))
  assert states(owner)==[("w1","unknown"),("w2","cancelled")]
  assert submit(ClaimAgentWrite(**owner))["status"]=="stale_run"


def test_successor_does_not_accept_its_predecessors_late_writes(chat):
  old=start(chat)
  save(old,write())
  new=start(chat,"successor")
  assert states(old)==[("w1","cancelled")]
  assert save(old,write("w2"))["status"]=="stale_run"
  assert save(new,write())["status"]=="accepted"


def test_another_chat_cannot_read_claim_or_settle_the_run(chat, db):
  owner=start(chat)
  save(owner,write())
  other=create_chat(id="unrelated",title="Other",messages=[])
  db.add(other);db.commit()
  wrong={**owner,"chat_id":other.id}
  for cmd in (ReadAgentWriteOutcomes(**wrong),ClaimAgentWrite(**wrong),
      SettleAgentWrite(**wrong,operation_id="w1",status="succeeded")):
    assert submit(cmd)["status"]=="stale_run"
  assert states(owner)==[("w1","queued")]


def test_capacity_is_atomic_and_rejection_explanation_is_durable(chat, monkeypatch):
  from app import agent_write_journal as journal
  owner=start(chat)
  monkeypatch.setattr(journal,"MAX_RUN_WRITES",1)
  assert save(owner,write(),write("w2"))["reason"]=="run_capacity"
  assert states(owner)==[]
  result=submit(ReadAgentWriteOutcomes(**owner))
  assert result["diagnostics"]==[{"stage":"admission","reason":"run_capacity","item_id":"item1"}]


def test_rejected_batch_and_dedup_only_batch_keep_immutable_item_receipts(chat, db, monkeypatch):
  from app import agent_write_journal as journal
  owner=start(chat)
  save(owner,write())
  assert save(owner,write(),item="duplicate-only")["new_count"]==0
  assert save(owner,write("new"),item="duplicate-only",fingerprint="b"*64)["reason"]=="changed_item"
  monkeypatch.setattr(journal,"MAX_RUN_WRITES",1)
  rejected=save(owner,write("rejected"),item="rejection")
  assert rejected["reason"]=="run_capacity"
  monkeypatch.setattr(journal,"MAX_RUN_WRITES",128)
  assert save(owner,write("rejected"),item="rejection")==rejected
  assert save(owner,write("new"),item="rejection",fingerprint="b"*64)["reason"]=="changed_item"
  db.expire_all()
  receipts=db.get(models.AgentWriteStream,owner["run_token"]).item_receipts
  assert set(receipts)=={"item1","duplicate-only","rejection"}
  assert states(owner)==[("w1","queued")]


def test_rejection_after_diagnostic_cap_changes_report_but_identical_replay_does_not(chat, db, monkeypatch):
  from app import agent_write_journal as journal

  owner = start(chat)
  monkeypatch.setattr(journal, "MAX_RUN_WRITES", 0)
  for index in range(journal.MAX_DIAGNOSTICS):
    assert save(owner, write(), item=f"rejected-{index}")["status"] == "rejected"
  db.expire_all()
  run = db.get(models.ChatRun, owner["run_token"])
  before = journal.failure_report(db, run)

  rejection = save(owner, write(), item="overflow")
  db.expire_all()
  after = journal.failure_report(db, run)
  assert after["fingerprint"] != before["fingerprint"]
  assert len(after["diagnostics"]) == journal.MAX_DIAGNOSTICS
  assert after["diagnostics"][-1]["additional_diagnostics_omitted"] == 1
  assert save(owner, write(), item="overflow") == rejection
  db.expire_all()
  assert journal.failure_report(db, run) == after
  assert states(owner) == []


def test_failure_report_selects_negative_metadata_without_loading_saved_arguments(chat, db):
  from sqlalchemy import event
  from app import agent_write_journal as journal

  owner = start(chat)
  save(owner, write("success", value="private success" * 1000), write("failure"))
  submit(ClaimAgentWrite(**owner))
  submit(SettleAgentWrite(**owner, operation_id="success", status="succeeded"))
  submit(ClaimAgentWrite(**owner))
  submit(SettleAgentWrite(**owner, operation_id="failure", status="failed", reason="synthetic failure"))
  db.expire_all()
  run = db.get(models.ChatRun, owner["run_token"])
  statements = []

  def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
    statements.append(statement)

  engine = db.get_bind()
  event.listen(engine, "before_cursor_execute", capture)
  try:
    report = journal.failure_report(db, run)
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  assert [entry["id"] for entry in report["writes"]] == ["failure"]
  assert report["writes"][0]["reason"] == "synthetic failure"
  assert "private success" not in str(report)
  intent_queries = [sql for sql in statements if "agent_write_intents" in sql]
  assert intent_queries
  assert all("arguments_json" not in sql for sql in intent_queries)


def test_interrupt_never_requeues_ambiguous_work(chat):
  owner=start(chat)
  save(owner,write(),write("w2"))
  submit(ClaimAgentWrite(**owner))
  submit(InterruptAgentWrites(**owner))
  assert states(owner)==[("w1","unknown"),("w2","cancelled")]
  assert submit(ClaimAgentWrite(**owner))["status"]=="empty"


def test_migration_is_idempotent_on_frozen_previous_schema_and_fresh_database(tmp_path):
  for name in ("fresh","previous"):
    path=tmp_path/f"{name}.sqlite"
    if name=="previous":
      with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parent/"fixtures/schema_0013.sql").read_text())
    engine=create_engine(f"sqlite:///{path}")
    _add_agent_write_journal(engine)
    _add_agent_write_journal(engine)
    inspector=inspect(engine)
    assert {"agent_write_streams","agent_write_intents"}.issubset(inspector.get_table_names())
    assert {c["name"] for c in inspector.get_columns("agent_write_intents")} >= {"status","arguments_json","source_run_id"}
    engine.dispose()
