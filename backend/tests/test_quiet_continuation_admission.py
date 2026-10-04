"""Run starts commit their owner fence with their source-specific handoff."""

import pytest

from app import models, transcript_rows
from app import chat_writer
from app.agent_write_channel import WriteIntent
from app.chat_writer import (
  AdmitAgentWrites, AdmitProviderExecution, ClaimAgentWrite, PromotePending,
  SealAgentWrites, SettleAgentWrite, StartTurn, get_writer,
)


def submit(command):
  return get_writer().submit(command).result(timeout=5)


def failed_write(chat):
  owner = {"chat_id": chat.id, "run_token": "failed-source"}
  submit(StartTurn(**owner, user_msg={"role": "user", "content": "Original", "ts": 10},
                   title_source="Original", default_provider="claude"))
  submit(AdmitProviderExecution(**owner))
  submit(AdmitAgentWrites(**owner, item_id="item", fingerprint="a" * 64,
    writes=(WriteIntent(owner["run_token"], "checkpoint_chat",
                        {"summary": "private"}),)))
  submit(ClaimAgentWrite(**owner))
  submit(SettleAgentWrite(**owner, operation_id=owner["run_token"],
                          status="failed", reason="synthetic failure"))
  submit(SealAgentWrites(**owner))
  return owner


def test_pending_handoff_commits_queue_transcript_and_run_together(chat, db):
  owner = failed_write(chat)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  row.pending_messages = [
    {"role": "user", "content": "First follow-up", "cid": "first", "ts": 20},
  ]
  db.commit()
  result = submit(PromotePending(chat_id=chat.id, run_token="promoted",
                                 ending_run_token=owner["run_token"]))
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  run = db.get(models.ChatRun, result["promoted"]["_run_token"])
  assert result["history"][-1].content == "First follow-up"
  assert run.status == "running" and run.root_run_id == run.id
  assert list(transcript_rows.history(saved))[-1]["content"] == "First follow-up"
  assert saved.pending_messages == []
  assert saved.active_assistant_message_id == run.id
  assert saved.live_assistant["id"] == run.id
  assert db.get(models.ChatRun, owner["run_token"]).status == "completed"
  submit(AdmitProviderExecution(chat_id=chat.id, run_token=run.id))
  db.expire_all()
  assert db.get(models.ChatRun, run.id).provider_execution_admitted is True


def test_clean_recovery_commits_run_without_consuming_owner_queue(chat, db):
  owner = failed_write(chat)
  result = submit(PromotePending(chat_id=chat.id, run_token="unused",
                                 ending_run_token=owner["run_token"]))
  source = result["promoted"]
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  run = db.get(models.ChatRun, source["_run_token"])
  assert source["continuation_reason"] == "quiet_write_failure"
  assert result["history"][-1].content == source["content"]
  assert run.root_run_id == owner["run_token"]
  assert run.continuation_json["source_work_id"] == owner["run_token"]
  assert saved.pending_messages == []
  assert len(list(transcript_rows.history(saved))) == 1  # Recovery control is provider-only.
  assert saved.active_assistant_message_id == run.id
  assert saved.live_assistant["id"] == run.id
  submit(AdmitProviderExecution(chat_id=chat.id, run_token=run.id))
  db.expire_all()
  assert db.get(models.ChatRun, run.id).provider_execution_admitted is True


def test_owner_question_blocks_both_queue_and_recovery_admission(chat, db):
  owner = failed_write(chat)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  row.pending_question_id = "card"
  db.commit()
  blocked = submit(PromotePending(chat_id=chat.id, run_token="candidate",
                                  ending_run_token=owner["run_token"]))
  assert blocked.reason == "question"
  db.expire_all()
  assert db.get(models.Chat, chat.id).active_assistant_message_id == owner["run_token"]
  assert db.query(models.ChatRun).count() == 1


@pytest.mark.parametrize("pending", [False, True])
def test_failed_admission_commit_preserves_prior_owner_and_pending_work(chat, db, monkeypatch, pending):
  owner = failed_write(chat)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  queued = [{"role": "user", "content": "Follow-up", "cid": "pending", "ts": 20}] if pending else []
  row.pending_messages = queued
  db.commit()
  before_messages = list(transcript_rows.history(row))

  def reject_commit(session):
    session.rollback()
    return False

  with monkeypatch.context() as patch:
    patch.setattr(chat_writer, "_commit_or_rollback", reject_commit)
    with pytest.raises(chat_writer._PersistFailed):
      submit(PromotePending(chat_id=chat.id, run_token="candidate",
                           ending_run_token=owner["run_token"]))

  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == queued
  assert list(transcript_rows.history(saved)) == before_messages
  assert saved.active_assistant_message_id == owner["run_token"]
  assert db.get(models.ChatRun, owner["run_token"]).status == "running"
  assert db.query(models.ChatRun).count() == 1
  assert get_writer()._run_token_owner[chat.id] == owner["run_token"]
