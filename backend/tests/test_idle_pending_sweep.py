"""Recovery of old pending queues with no durable or in-memory run owner."""

import asyncio
import time
from datetime import UTC, datetime

from app import chat as chat_mod
from app import models
from app.chat_writer import cid_of
from app.database import SessionLocal, engine
from app.runner_registry import registry
from sqlalchemy import event


def _seed_pending(
  chat_id: str,
  *,
  age_secs: float,
  running: bool = False,
  pending_question_id: str | None = None,
  messages: list[dict] | None = None,
) -> None:
  now_ms = int(time.time() * 1000)
  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id,
      title="pending",
      provider="claude",
      messages=(
        messages
        if messages is not None
        else [{"role": "user", "content": "first", "ts": 1}]
      ),
      pending_messages=[{
        "role": "user",
        "content": "recover me",
        "ts": now_ms - int(age_secs * 1000),
        "cid": f"cid-{chat_id}",
      }],
      pending_question_id=pending_question_id,
    )
    db.add(chat)
    db.flush()
    if running:
      db.add(models.ChatRun(
        id=f"rt-{chat_id}",
        chat_id=chat_id,
        status="running",
        provider="claude",
        started_at=datetime.now(UTC),
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
    from app.run_state import latest_run
    run = latest_run(db, chat_id)
    return (
      (
        run.status
        if run is not None and run.status in models.NONTERMINAL_RUN_STATUSES
        else None
      ),
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
  _seed_pending(chat_id, age_secs=180, running=True)

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


def test_idle_pending_sweep_never_crosses_owner_question(monkeypatch):
  """An aged product wake stays queued until the owner answers.

  This is the restart failure shape from the reported chat: boot has closed
  the interrupted run, the open question is durable, and a queued wake is old
  enough for generic idle recovery. Age is not owner authorization.
  """
  chat_id = "question-blocked-old-pending"
  question = {
    "role": "assistant",
    "content": "Choose how to proceed.",
    "ts": 2,
    "blocks": [{
      "type": "question",
      "question_id": "owner-decision",
      "questions": [{
        "id": "push_requeue_787",
        "question": "Push and requeue PR #787?",
      }],
    }],
  }
  _seed_pending(
    chat_id,
    age_secs=180,
    pending_question_id="owner-decision",
    messages=[
      {"role": "user", "content": "Review PR #787", "ts": 1},
      question,
    ],
  )

  swept, scheduled = _sweep(monkeypatch)

  assert swept == []
  assert scheduled == []
  status, messages, pending = _read(chat_id)
  assert status is None
  assert messages[-1] == question
  assert [cid_of(row) for row in pending] == [f"cid-{chat_id}"]
  db = SessionLocal()
  try:
    assert db.get(models.Chat, chat_id).pending_question_id == "owner-decision"
  finally:
    db.close()
  assert not registry.is_alive(chat_id)


def test_idle_pending_sweep_candidate_query_does_not_load_transcripts(
  monkeypatch,
):
  chat_id = "idle-empty-large-transcript"
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id,
      title="large but idle",
      provider="codex",
      messages=[{"role": "assistant", "content": "x" * 1_000_000}],
      pending_messages=[],
    ))
    db.commit()
  finally:
    db.close()

  statements = []

  def capture(_conn, _cursor, statement, _parameters, _context, _many):
    if "FROM chats" in statement and "chats.pending_messages" in statement:
      statements.append(statement)

  event.listen(engine, "before_cursor_execute", capture)
  try:
    swept, scheduled = _sweep(monkeypatch)
  finally:
    event.remove(engine, "before_cursor_execute", capture)

  assert swept == []
  assert scheduled == []
  assert len(statements) == 1
  projection = statements[0].split("FROM chats", 1)[0]
  assert "chats.id" in projection
  assert "chats.pending_messages" in projection
  assert "chats.messages" not in projection


def _park_latest_run(chat_id: str, *, minutes_out: float) -> None:
  from datetime import timedelta
  db = SessionLocal()
  try:
    db.add(models.ChatRun(
      id=f"rt-park-{chat_id}",
      chat_id=chat_id,
      status="parked",
      started_at=datetime.now(UTC).replace(tzinfo=None),
      parked_until=(
        datetime.now(UTC).replace(tzinfo=None)
        + timedelta(minutes=minutes_out)
      ),
      park_reason="provider_limit",
    ))
    db.commit()
  finally:
    db.close()


def test_idle_pending_sweep_leaves_limit_parked_queue_alone(monkeypatch):
  """LIMIT_PARKED preserves pending so it is NOT refired into the limit.

  A parked chat has no running row. Draining it would
  consume the owner's queued work into a doomed turn and bypass the
  auto-resume opt-in that sweep_reset_parks enforces.
  """
  chat_id = "idle-limit-parked"
  _seed_pending(chat_id, age_secs=600)
  _park_latest_run(chat_id, minutes_out=30)

  swept, scheduled = _sweep(monkeypatch)

  assert swept == []
  assert scheduled == []
  active_status, _messages, pending = _read(chat_id)
  assert active_status == "parked"
  assert [m["content"] for m in pending] == ["recover me"]


def test_idle_pending_sweep_skips_past_park_until_user_or_optin(monkeypatch):
  """Even an EXPIRED park is not the idle sweep's to drain.

  After the reset, resumption belongs to sweep_reset_parks (when the
  owner opted in) or the user's own next send — never this sweep.
  """
  chat_id = "idle-expired-park"
  _seed_pending(chat_id, age_secs=600)
  _park_latest_run(chat_id, minutes_out=-5)

  swept, scheduled = _sweep(monkeypatch)

  assert swept == []
  assert scheduled == []
  _active_status, _messages, pending = _read(chat_id)
  assert [m["content"] for m in pending] == ["recover me"]


def test_idle_pending_sweep_stands_down_while_draining(monkeypatch):
  """The restart drain gate owns the queue during a drain window."""
  chat_id = "idle-during-drain"
  _seed_pending(chat_id, age_secs=600)
  monkeypatch.setattr(chat_mod, "draining", True)

  swept, scheduled = _sweep(monkeypatch)

  assert swept == []
  assert scheduled == []
  _active_status, _messages, pending = _read(chat_id)
  assert [m["content"] for m in pending] == ["recover me"]
