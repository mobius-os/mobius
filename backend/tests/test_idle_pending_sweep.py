"""Recovery of old pending queues with no durable or in-memory run owner."""

import asyncio
import time
from datetime import UTC, datetime

from app import chat as chat_mod
from app import models
from app.chat_writer import cid_of
from app.database import SessionLocal
from app.runner_registry import registry


def _seed_pending(
  chat_id: str,
  *,
  age_secs: float,
  run_status: str | None = None,
) -> None:
  now_ms = int(time.time() * 1000)
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id,
      title="pending",
      provider="claude",
      messages=[{"role": "user", "content": "first", "ts": 1}],
      pending_messages=[{
        "role": "user",
        "content": "recover me",
        "ts": now_ms - int(age_secs * 1000),
        "cid": f"cid-{chat_id}",
      }],
      run_status=run_status,
      run_started_at=(datetime.now(UTC) if run_status else None),
    ))
    db.commit()
  finally:
    db.close()


def _sweep(monkeypatch):
  scheduled = []

  def _schedule(**kwargs):
    scheduled.append(kwargs)
    return True

  monkeypatch.setattr(chat_mod, "_schedule_continuation", _schedule)
  db = SessionLocal()
  try:
    swept = asyncio.run(chat_mod.sweep_idle_pending_chats(db))
  finally:
    db.close()
  return swept, scheduled


def _read(chat_id: str):
  db = SessionLocal()
  try:
    chat = db.get(models.Chat, chat_id)
    return (
      chat.run_status,
      list(chat.messages or []),
      list(chat.pending_messages or []),
    )
  finally:
    db.close()


def test_idle_pending_sweep_claims_and_starts_old_queue(monkeypatch):
  chat_id = "idle-old-pending"
  _seed_pending(chat_id, age_secs=180)
  claims = []
  original_mark_starting = chat_mod.mark_starting

  def _claim(candidate):
    claims.append(candidate)
    return original_mark_starting(candidate)

  monkeypatch.setattr(chat_mod, "mark_starting", _claim)
  swept, scheduled = _sweep(monkeypatch)

  assert swept == [chat_id]
  assert claims == [chat_id]
  assert len(scheduled) == 1
  status, messages, pending = _read(chat_id)
  assert status == "running"
  assert pending == []
  assert [cid_of(row) for row in messages if row.get("role") == "user"] == [
    "legacy-1",
    f"cid-{chat_id}",
  ]


def test_idle_pending_sweep_leaves_running_chat_untouched(monkeypatch):
  chat_id = "running-old-pending"
  _seed_pending(chat_id, age_secs=180, run_status="running")

  swept, scheduled = _sweep(monkeypatch)

  assert swept == []
  assert scheduled == []
  status, _messages, pending = _read(chat_id)
  assert status == "running"
  assert [cid_of(row) for row in pending] == [f"cid-{chat_id}"]


def test_idle_pending_sweep_respects_age_gate(monkeypatch):
  chat_id = "recent-idle-pending"
  _seed_pending(chat_id, age_secs=30)

  swept, scheduled = _sweep(monkeypatch)

  assert swept == []
  assert scheduled == []
  status, _messages, pending = _read(chat_id)
  assert status is None
  assert [cid_of(row) for row in pending] == [f"cid-{chat_id}"]
  assert not registry.is_alive(chat_id)
