"""Run starts commit their owner fence with their source-specific handoff."""

import pytest

from app import models
from app import chat_writer
from app.chat_writer import (
  AdmitProviderExecution, PromotePending, StartTurn, get_writer,
)


def submit(command):
  return get_writer().submit(command).result(timeout=5)


def started_run(chat):
  owner = {"chat_id": chat.id, "run_token": "finished-source"}
  submit(StartTurn(**owner, user_msg={"role": "user", "content": "Original", "ts": 10},
                   title_source="Original", default_provider="claude"))
  submit(AdmitProviderExecution(**owner))
  return owner


def test_pending_handoff_commits_queue_transcript_and_run_together(chat, db):
  owner = started_run(chat)
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
  assert saved.messages[-1]["content"] == "First follow-up"
  assert saved.pending_messages == []
  assert saved.active_assistant_message_id == run.id
  assert saved.live_assistant["id"] == run.id
  assert db.get(models.ChatRun, owner["run_token"]).status == "completed"
  submit(AdmitProviderExecution(chat_id=chat.id, run_token=run.id))
  db.expire_all()
  assert db.get(models.ChatRun, run.id).provider_execution_admitted is True


def test_owner_question_blocks_queue_admission(chat, db):
  owner = started_run(chat)
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


def test_failed_admission_commit_preserves_prior_owner_and_pending_work(chat, db, monkeypatch):
  owner = started_run(chat)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  queued = [{"role": "user", "content": "Follow-up", "cid": "pending", "ts": 20}]
  row.pending_messages = queued
  db.commit()
  before_messages = list(row.messages)

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
  assert saved.messages == before_messages
  assert saved.active_assistant_message_id == owner["run_token"]
  assert db.get(models.ChatRun, owner["run_token"]).status == "running"
  assert db.query(models.ChatRun).count() == 1
  assert get_writer()._run_token_owner[chat.id] == owner["run_token"]
