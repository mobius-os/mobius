"""Agent-authored continuity persistence, migration, and handoff contracts."""

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

from app import auth as auth_module, models
from app.chat_continuity import (
  _prefix_hash, completed_prefix, verified_uncovered_messages,
)
from app.chat_writer import StartTurn, get_writer
from app.compaction import build_portable_source
from app.chat_retention import purge_expired_chat_tombstones
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc
from tests.goal_fixtures import goal_run as make_goal_run


def _start(chat, run_id="continuity-run"):
  result = get_writer().submit(StartTurn(
    chat_id=chat.id,
    run_token=run_id,
    user_msg={"role": "user", "content": "continue", "ts": 10},
    title_source="continue",
  )).result(timeout=5)
  assert isinstance(result, dict)
  token = auth_module.create_agent_token(
    chat.id, "test", 0, run_id=run_id,
  )
  return {"Authorization": f"Bearer {token}"}


def test_checkpoint_retry_platform_revision_and_manual_title(
  client, auth, chat, db,
):
  chat.title = "Owner title"
  chat.title_locked = True
  db.commit()
  agent = _start(chat)
  payload = {
    "checkpoint_id": "cp-1",
    "digest": "Implemented the first durable slice.",
    "summary": "Core work is underway.",
    "title": "Generated title",
  }
  first = client.post(
    "/api/chat/continuity/checkpoints", headers=agent, json=payload,
  )
  assert first.status_code == 200, first.text
  assert first.json() == {
    "status": "committed",
    "revision": 1,
    "coverage": {"message_count": 0, "prefix_hash": None},
    "title_applied": False,
  }
  retry = client.post(
    "/api/chat/continuity/checkpoints", headers=agent, json=payload,
  )
  assert retry.status_code == 200, retry.text
  assert retry.json()["status"] == "already_committed"
  assert retry.json()["entry_revision"] == 1
  assert retry.json()["revision"] == 1

  stale = client.post(
    "/api/chat/continuity/checkpoints", headers=agent,
    json={**payload, "checkpoint_id": "cp-2"},
  )
  assert stale.status_code == 200
  assert stale.json()["revision"] == 2
  db.expire_all()
  assert db.get(models.Chat, chat.id).title == "Owner title"


def test_first_checkpoint_imports_entire_legacy_note_once(client, auth, chat, db):
  legacy = """---
description: Old name
---

## Digest

Short old state.

## Summary

Complete old fact A.\n\n## Nested detail\nFact B.

## Facts & Intent

Related fact C.
"""
  path = (
    Path(os.environ["DATA_DIR"]) / "shared" / "memory" / "chats"
    / chat.id / "index.md"
  )
  path.parent.mkdir(parents=True)
  path.write_text(legacy, encoding="utf-8")
  agent = _start(chat, "legacy-run")
  saved = client.post(
    "/api/chat/continuity/checkpoints", headers=agent,
    json={
      "checkpoint_id": "after-legacy",
      "digest": "New work started.",
    },
  )
  assert saved.status_code == 200, saved.text
  assert saved.json()["revision"] == 2
  full = client.get(
    f"/api/chats/{chat.id}/continuity?full=true",
    headers=auth,
  )
  assert full.status_code == 200, full.text
  baseline = full.json()["entries"][0]
  assert baseline["legacy_baseline"] is True
  assert baseline["legacy_markdown"] == legacy
  assert full.json()["summary"] == "Short old state."
  page = client.get(
    f"/api/chats/{chat.id}/continuity?after_revision=0&limit=1&include_legacy=true",
    headers=auth,
  )
  assert page.status_code == 200, page.text
  assert page.json()["entries"][0]["legacy_markdown"] == legacy
  assert "summary" not in page.json()["entries"][0]


def test_legacy_note_without_a_database_baseline_is_read_losslessly(client, auth, chat):
  legacy = "---\ndescription: Old\n---\n\n## Summary\n\n" + "x" * 1200
  path = (
    Path(os.environ["DATA_DIR"]) / "shared" / "memory" / "chats"
    / chat.id / "index.md"
  )
  path.parent.mkdir(parents=True)
  path.write_text(legacy, encoding="utf-8")

  page = client.get(
    f"/api/chats/{chat.id}/continuity?after_revision=0&include_legacy=true",
    headers=auth,
  )

  assert page.status_code == 200, page.text
  assert page.json()["entries"][0]["legacy_markdown"] == legacy


def test_stale_running_row_cannot_checkpoint(client, chat, db):
  current = _start(chat, "current-run")
  db.add(models.ChatRun(id="stale-run", chat_id=chat.id, status="running"))
  db.commit()
  stale_token = auth_module.create_agent_token(
    chat.id, "test", 0, run_id="stale-run",
  )
  response = client.post(
    "/api/chat/continuity/checkpoints",
    headers={"Authorization": f"Bearer {stale_token}"},
    json={
      "checkpoint_id": "stale-cp",
      "digest": "Must not land.",
    },
  )
  assert response.status_code == 409
  assert response.json()["detail"]["status"] == "stale_run"
  assert current


def test_ordinary_run_checkpoints_despite_historical_goal(client, chat, db):
  db.add(make_goal_run(db,
    id="historical-goal", root_run_id="historical-goal",
    chat_id=chat.id, status="completed", provider="codex",
    goal_objective="Old work", goal_id="old-goal-id",
  ))
  db.commit()
  agent = _start(chat, "ordinary-question")

  response = client.post(
    "/api/chat/continuity/checkpoints", headers=agent,
    json={
      "checkpoint_id": "ordinary-checkpoint",
      "digest": "Discussion-only continuity is durable too.",
      "summary": "The ordinary turn remains current.",
    },
  )

  assert response.status_code == 200, response.text
  assert response.json()["status"] == "committed"
  entry = db.query(models.ChatContinuityEntry).filter_by(
    chat_id=chat.id, checkpoint_id="ordinary-checkpoint",
  ).one()
  assert entry.run_id == "ordinary-question"


def test_whole_prefix_proof_replays_transcript_after_edit():
  messages = [
    {"role": "user", "content": "old question", "cid": "q1"},
    {"role": "assistant", "content": "old answer", "id": "old-run"},
    {"role": "user", "content": "current question", "cid": "q2"},
    {"role": "assistant", "content": "partial", "id": "new-run"},
  ]
  count, digest = completed_prefix(messages, "new-run")
  assert count == 2 and digest
  suffix, verified = verified_uncovered_messages(
    messages, covered_count=count, covered_prefix_hash=digest,
  )
  assert verified is True
  assert suffix == messages[2:]
  messages[0]["content"] = "edited old question"
  replay, verified = verified_uncovered_messages(
    messages, covered_count=count, covered_prefix_hash=digest,
  )
  assert verified is False
  assert replay == messages


def test_streamed_prefix_hash_preserves_legacy_canonical_bytes():
  rows = [
    {"role": "user", "content": "café \"quoted\"\n😀", "nested": {"z": 1.25, "a": [None, True]}},
    {"role": "assistant", "id": "old", "content": "backslash \\ and €"},
  ]
  old = hashlib.sha256(json.dumps(
    rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")).hexdigest()
  assert _prefix_hash(rows, len(rows)) == old
  assert _prefix_hash(rows, 0) is None


def test_portable_source_uses_complete_journal_and_only_verified_suffix(chat, db):
  chat.messages = [
    {"role": "user", "content": "covered", "cid": "q1"},
    {"role": "assistant", "content": "covered answer", "id": "old-run"},
    {"role": "user", "content": "uncovered decision", "cid": "q2"},
  ]
  count, digest = completed_prefix(chat.messages, "new-run")
  db.add(models.ChatContinuity(
    chat_id=chat.id, revision=2, current_summary="Current state.",
    covered_message_count=count, covered_prefix_hash=digest,
  ))
  db.add_all([
    models.ChatContinuityEntry(
      chat_id=chat.id, revision=1, checkpoint_id="cp1", run_id="old-run",
      digest="First durable decision.", covered_message_count=0,
    ),
    models.ChatContinuityEntry(
      chat_id=chat.id, revision=2, checkpoint_id="cp2", run_id="old-run",
      digest="Second durable decision.", covered_message_count=count,
      covered_prefix_hash=digest,
    ),
  ])
  db.commit()
  source = build_portable_source(
    db, os.environ["DATA_DIR"], chat.id, chat.messages,
  )
  assert "First durable decision." in source
  assert "Second durable decision." in source
  assert "uncovered decision" in source
  assert "covered answer" not in source


def test_soft_delete_preserves_continuity_until_expired_purge(chat, db):
  chat_id = chat.id
  db.add(models.ChatContinuity(
    chat_id=chat.id, revision=1, current_summary="Recoverable.",
    covered_message_count=0,
  ))
  db.add(models.ChatContinuityEntry(
    chat_id=chat.id, revision=1, checkpoint_id="cp-retained",
    digest="Keep through recovery window.", covered_message_count=0,
  ))
  chat.deleted_at = now_naive_utc()
  db.commit()
  assert purge_expired_chat_tombstones(db) == []
  assert db.get(models.ChatContinuity, chat.id) is not None

  chat.deleted_at = now_naive_utc() - SOFT_DELETE_TTL - timedelta(seconds=1)
  db.commit()
  assert purge_expired_chat_tombstones(db) == [chat_id]
  assert db.get(models.ChatContinuity, chat_id) is None
  assert db.query(models.ChatContinuityEntry).filter_by(chat_id=chat_id).count() == 0
