"""Live-turn liveness watchdog + debug health surface."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models, questions
from app.broadcast import create_broadcast, remove_broadcast
from app.chat_writer import Barrier, get_writer
from app.database import SessionLocal
from app.pending_questions import PendingQuestion
from app.runner_registry import RunnerKind, registry


class _Handle:
  def __init__(self, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self.stop_calls = 0

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self.stop_calls += 1
    registry.unregister(self.chat_id, self.kind)
    return True


def _drain_writer():
  get_writer().submit(Barrier()).result(timeout=5)


def _seed(chat_id: str, *, pending=None, age_secs=30):
  db = SessionLocal()
  try:
    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(
      seconds=age_secs
    )
    db.add(models.Chat(
      id=chat_id,
      title="t",
      messages=[{"role": "user", "content": "do work", "ts": 1}],
      pending_messages=pending or [],
      session_id="sess",
      provider="claude",
      run_status="running",
      run_started_at=started,
    ))
    db.add(models.ChatRun(
      id=f"rt-{chat_id}",
      chat_id=chat_id,
      status="running",
      provider="claude",
      started_at=started,
    ))
    db.commit()
  finally:
    db.close()


def _sweep():
  db = SessionLocal()
  try:
    return asyncio.run(chat_mod.sweep_stalled_live_runs(db))
  finally:
    db.close()


def _chat(chat_id: str):
  db = SessionLocal()
  try:
    row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return {
      "messages": list(row.messages or []),
      "pending": list(row.pending_messages or []),
      "run_status": row.run_status,
    }
  finally:
    db.close()


def _live_stale(chat_id: str, *, pending=None):
  _seed(chat_id, pending=pending)
  bc = create_broadcast(chat_id)
  bc.publish({"type": "text", "content": "partial"})
  bc.last_event_at = time.monotonic() - chat_mod.PROGRESS_TIMEOUT - 5
  sink = chat_mod._ChatEventSink(bc, chat_id, run_token=f"rt-{chat_id}")
  chat_mod.register_active_sink(chat_id, sink)
  handle = _Handle(chat_id)
  registry.register(handle)
  return bc, sink, handle


def test_broadcast_creation_starts_watchdog_clock():
  before = time.monotonic()
  bc = create_broadcast("clock-start-1")
  try:
    after = time.monotonic()
    assert before <= bc.last_event_at <= after
    assert bc.event_log == []
    assert chat_mod.last_event_age_secs(bc) is not None
  finally:
    remove_broadcast("clock-start-1")


def test_watchdog_interrupts_stale_client_preserves_queue_and_persists_error():
  _, _, handle = _live_stale(
    "stale-1",
    pending=[{"role": "user", "content": "queued", "ts": 2}],
  )

  swept = _sweep()
  _drain_writer()

  state = _chat("stale-1")
  assert swept == ["stale-1"]
  assert handle.stop_calls == 1
  assert state["pending"] == [{"role": "user", "content": "queued", "ts": 2}]
  blocks = state["messages"][-1]["blocks"]
  assert any(
    block.get("type") == "error"
    and block.get("message") == chat_mod.STALLED_TURN_MESSAGE
    for block in blocks
  )


def test_watchdog_exempts_registered_pending_question():
  _bc, _sink, handle = _live_stale("question-1")
  loop = asyncio.new_event_loop()
  try:
    questions.register(
      "question-1",
      PendingQuestion(
        question_id="q1",
        questions=[],
        future=loop.create_future(),
        run_token="rt-question-1",
      ),
    )

    swept = _sweep()
  finally:
    loop.close()

  assert swept == []
  assert handle.stop_calls == 0


def test_fresh_event_resets_watchdog_clock():
  bc, _sink, handle = _live_stale("fresh-1")
  bc.publish({"type": "tool_start", "name": "build"})

  swept = _sweep()

  assert swept == []
  assert handle.stop_calls == 0
  assert chat_mod.last_event_age_secs(bc) < 5


def test_debug_status_exposes_liveness_ages_and_stale_flag(client, auth):
  bc, _sink, _handle = _live_stale("debug-stale-1")
  catch_up, q = bc.subscribe()
  assert catch_up
  assert q is not None

  r = client.get("/api/debug/status", headers=auth)

  assert r.status_code == 200
  data = r.json()
  entry = next(
    item for item in data["active_sdk_clients"]
    if item["chat_id"] == "debug-stale-1"
  )
  assert entry["state"] == "stale"
  assert entry["last_event_age_secs"] > chat_mod.PROGRESS_TIMEOUT
  assert entry["run_age_secs"] is not None
  assert entry["subscriber_count"] == 1
  assert entry["stale"] is True

  broadcast = next(
    item for item in data["broadcasts"]
    if item["chat_id"] == "debug-stale-1"
  )
  assert broadcast["stale"] is True
  assert broadcast["subscriber_count"] == 1
