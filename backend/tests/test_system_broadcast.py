"""Tests for the load-bearing event-bus contracts:

- Bug 3: `_ChatEventSink.publish` MUST persist a `question` event
  to the DB BEFORE broadcasting it. The frontend renders the
  question card on broadcast; a user Submit that races the save
  would find no question block in `chat.messages` and the answer
  would be silently dropped.

- Bug 4: System events (theme_updated, app_updated, shell_*) MUST
  reach the `SystemBroadcast` so a Shell subscriber sees them
  regardless of whether a per-chat broadcast is currently active.
  Without this the user gets a forever-spinner after the agent
  updates a mini-app because the iframe version never bumps.

- Commit offload: the streaming save's blocking `db.commit()` must
  run OFF the event loop. `publish()` only queues commands; the
  loop-owned consumer commits via `asyncio.to_thread`.

- Finalize call sites audited for async teardown:
  `chat.py` Codex exception, Codex completion, Claude exception, and
  Claude completion paths all use `await sink.finalize()`.
"""

import asyncio

import pytest
from pydantic import ValidationError

from app import broadcast as bc_mod
from app import chat as chat_mod
from app import models
from app.broadcast import (
  ChatBroadcast,
  SystemBroadcast,
  get_system_broadcast,
)
from app.routes.notify import NotifyBody


# --- Bug 3: question save-before-publish ------------------------------


class _OrderedBroadcast(ChatBroadcast):
  """ChatBroadcast that records the order of publish vs save calls.

  Tests inject one of these into a sink and assert the SAVE step
  (which happens via `_update_last_assistant_message` in a worker)
  lands BEFORE the publish for question events.
  """

  def __init__(self, chat_id):
    super().__init__(chat_id)
    self.timeline = []

  def publish(self, event):
    self.timeline.append(("publish", event.get("type")))
    super().publish(event)


@pytest.mark.asyncio
async def test_question_event_is_saved_before_broadcast(db, chat):
  """User-Submit-races-save regression: AskUserQuestion events must
  land in chat.messages BEFORE the broadcast reaches a frontend
  that might POST an answer.

  Seed the chat with a user message first — in real usage the
  runner only emits a question event AFTER the user sent something,
  so chat.messages is non-empty by then. _update_last_assistant_message
  early-returns when chat.messages is empty (otherwise it'd silently
  drop the first assistant write).
  """
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  # Spy: monkey-patch _update_last_assistant_message so we can
  # record when it ran relative to bc.publish().
  original = chat_mod._update_last_assistant_message
  def spy(db_, chat_id, message):
    bc.timeline.append(("save", chat_id))
    original(db_, chat_id, message)
  chat_mod._update_last_assistant_message = spy
  try:
    questions = [{
      "id": "q1",
      "question": "Color?",
      "options": [{"label": "Red"}, {"label": "Blue"}],
    }]
    sink.publish({"type": "question", "questions": questions})
    await sink.flush()
    await sink.finalize()
  finally:
    chat_mod._update_last_assistant_message = original

  # Save must precede publish in the timeline.
  save_idx = next(
    i for i, (kind, _) in enumerate(bc.timeline) if kind == "save"
  )
  pub_idx = next(
    i for i, (kind, _) in enumerate(bc.timeline) if kind == "publish"
  )
  assert save_idx < pub_idx, (
    f"save_idx={save_idx} pub_idx={pub_idx} timeline={bc.timeline}"
  )

  # And the question block is actually persisted in chat.messages.
  db.refresh(chat)
  msgs = list(chat.messages or [])
  assert msgs and msgs[-1].get("role") == "assistant"
  blocks = msgs[-1].get("blocks") or []
  assert any(b.get("type") == "question" for b in blocks)


@pytest.mark.asyncio
async def test_non_question_save_is_deferred_to_flush_off_loop(db, chat):
  """Non-question events broadcast immediately and DEFER their DB
  commit to `flush()` so the blocking `db.commit()` runs off the
  event loop (chat._ChatEventSink offload). `publish()` must NOT
  commit inline for these; it records the snapshot and the runner's
  later `await flush()` performs the write."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)
  original = chat_mod._update_last_assistant_message
  def spy(db_, chat_id, message):
    bc.timeline.append(("save", chat_id))
    return original(db_, chat_id, message)
  chat_mod._update_last_assistant_message = spy
  try:
    # tool_start is an IMMEDIATE_SAVE_TYPE, so a save is due — but it
    # must be deferred, not committed inside publish().
    sink._last_save = 0.0
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    # Let the consumer process the event. It broadcasts and parks the
    # snapshot, but does not save until the barrier reaches the queue.
    await asyncio.sleep(0)
    assert ("publish", "tool_start") in bc.timeline
    assert not any(kind == "save" for kind, _ in bc.timeline), (
      f"publish() must defer the commit, not run it inline. "
      f"timeline={bc.timeline}"
    )
    assert sink._pending_save is not None

    # flush() performs the deferred commit (off-loop via to_thread).
    ok = await sink.flush()
    await sink.finalize()
  finally:
    chat_mod._update_last_assistant_message = original

  assert ok is True
  assert any(kind == "save" for kind, _ in bc.timeline), (
    f"flush() must perform the deferred save. timeline={bc.timeline}"
  )
  assert sink._pending_save is None
  # And the tool block is actually persisted.
  db.refresh(chat)
  msgs = list(chat.messages or [])
  assert msgs and msgs[-1].get("role") == "assistant"


@pytest.mark.asyncio
async def test_flush_runs_commit_off_the_event_loop_thread(db, chat):
  """The deferred commit must execute on a worker thread, not the
  loop thread — that is the whole point of the offload (a slow
  SQLite commit can't stall the loop). Pin it by capturing the
  thread `_update_last_assistant_message` runs on and asserting it
  differs from the loop thread."""
  import threading

  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  seen: dict = {}
  original = chat_mod._update_last_assistant_message
  def spy(db_, chat_id, message):
    seen["thread"] = threading.get_ident()
    return original(db_, chat_id, message)
  chat_mod._update_last_assistant_message = spy

  async def _scenario():
    seen["loop_thread"] = threading.get_ident()
    sink._last_save = 0.0
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    await sink.flush()
    await sink.finalize()

  try:
    await _scenario()
  finally:
    chat_mod._update_last_assistant_message = original

  assert "thread" in seen, "flush() never ran the commit"
  assert seen["thread"] != seen["loop_thread"], (
    "the streaming commit ran on the event loop thread — it must be "
    "offloaded via asyncio.to_thread so a SQLite lock can't stall the "
    "loop and starve other chats' SSE"
  )


@pytest.mark.asyncio
async def test_flush_worker_uses_its_own_session(db, chat, monkeypatch):
  """The off-loop save must open a SessionLocal in the worker thread
  instead of sharing the sink's loop-owned session."""
  import threading

  from app import database as db_mod

  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  seen: dict = {"factory_threads": []}
  original_factory = db_mod.SessionLocal
  original_update = chat_mod._update_last_assistant_message

  def session_factory():
    seen["factory_threads"].append(threading.get_ident())
    return original_factory()

  def spy(worker_db, chat_id, message):
    seen["worker_db"] = worker_db
    return original_update(worker_db, chat_id, message)

  monkeypatch.setattr(db_mod, "SessionLocal", session_factory)
  monkeypatch.setattr(chat_mod, "_update_last_assistant_message", spy)

  async def _scenario():
    seen["loop_thread"] = threading.get_ident()
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    await sink.flush()
    await sink.finalize()

  await _scenario()

  assert len(seen["factory_threads"]) >= 1
  assert all(
    thread != seen["loop_thread"] for thread in seen["factory_threads"]
  )
  assert seen["worker_db"] is not db


@pytest.mark.asyncio
async def test_blocked_flush_then_question_preserves_save_before_broadcast(
  db, chat, monkeypatch,
):
  """A question queued behind a blocked flush survives and is saved
  before its broadcast reaches the frontend."""
  import threading
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  flush_entered = threading.Event()
  release_flush = threading.Event()
  original = chat_mod._update_last_assistant_message

  def hold_stale_flush(db_, chat_id, message):
    if threading.get_ident() != loop_thread and not flush_entered.is_set():
      flush_entered.set()
      assert release_flush.wait(timeout=2.0)
    bc.timeline.append((
      "save",
      any(
        block.get("type") == "question"
        for block in message.get("blocks") or []
      ),
    ))
    return original(db_, chat_id, message)

  monkeypatch.setattr(
    chat_mod, "_update_last_assistant_message", hold_stale_flush,
  )

  async def _scenario():
    nonlocal loop_thread
    loop_thread = threading.get_ident()
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    flush_task = asyncio.create_task(sink.flush())
    assert await asyncio.to_thread(flush_entered.wait, 1.0)

    sink.publish({
      "type": "question",
      "questions": [{
        "id": "q1",
        "question": "Color?",
        "options": [{"label": "Red"}, {"label": "Blue"}],
      }],
    })
    assert ("publish", "question") not in bc.timeline
    release_flush.set()
    await flush_task
    await sink.flush()
    await sink.finalize()

  loop_thread = 0
  await _scenario()

  db.expire_all()
  persisted = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  blocks = persisted.messages[-1].get("blocks") or []
  assert any(block.get("type") == "question" for block in blocks)
  pub_idx = next(
    i for i, item in enumerate(bc.timeline)
    if item == ("publish", "question")
  )
  assert any(
    item == ("save", True) for item in bc.timeline[:pub_idx]
  ), bc.timeline


@pytest.mark.asyncio
async def test_question_commit_runs_off_the_loop_thread(db, chat, monkeypatch):
  """Question persistence stays save-before-broadcast without blocking
  the event-loop thread."""
  import threading

  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  seen: dict = {}
  original = chat_mod._update_last_assistant_message
  def spy(db_, chat_id, message):
    seen["thread"] = threading.get_ident()
    return original(db_, chat_id, message)
  monkeypatch.setattr(chat_mod, "_update_last_assistant_message", spy)
  seen["caller_thread"] = threading.get_ident()
  sink.publish({
    "type": "question",
    "questions": [{
      "id": "q1",
      "question": "Color?",
      "options": [{"label": "Red"}, {"label": "Blue"}],
    }],
  })
  await sink.flush()
  await sink.finalize()

  assert seen.get("thread") != seen["caller_thread"]
  assert sink._pending_save is None


@pytest.mark.asyncio
async def test_flush_surfaces_commit_failure_via_return(db, chat, monkeypatch):
  """The deferred-save outcome is observable: flush() returns the
  commit result (False on a dropped/locked write)."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  monkeypatch.setattr(chat_mod, "_safe_commit", lambda _db: False)

  # publish records the pending save; flush attempts the commit.
  sink._last_save = 0.0
  publish_ok = sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
  flush_ok = await sink.flush()
  await sink.finalize()

  # publish() reports True (it didn't commit inline); flush() exposes
  # the False from the failed commit.
  assert publish_ok is True
  assert flush_ok is False


@pytest.mark.asyncio
async def test_slow_flush_does_not_block_a_concurrent_coroutine(
  db, chat, monkeypatch,
):
  """The headline bug: a slow streaming commit must NOT block the
  event loop. Simulate a commit that sleeps (a SQLite busy_timeout
  wait) and assert a concurrent coroutine keeps making progress while
  the flush is in-flight — which is only possible if the commit runs
  off the loop via asyncio.to_thread."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  import time as _time

  def slow_commit(_db_, _chat_id, _message):
    # Stand in for a lock-contended commit blocking on busy_timeout.
    _time.sleep(0.3)
    return True

  monkeypatch.setattr(chat_mod, "_update_last_assistant_message", slow_commit)

  ticks = {"count": 0}

  async def _other_chat_loop():
    # A different chat's coroutine; each tick is a loop turn. If the
    # slow commit were inline on the loop, these ticks would stall for
    # the whole 0.3s.
    for _ in range(30):
      ticks["count"] += 1
      await asyncio.sleep(0.01)

  async def _scenario():
    other = asyncio.create_task(_other_chat_loop())
    sink._last_save = 0.0
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    await sink.flush()  # blocks ~0.3s, but OFF the loop
    ticks_at_flush_return = ticks["count"]
    await other
    await sink.finalize()
    return ticks_at_flush_return

  ticks_at_flush_return = await _scenario()

  # During the ~0.3s commit the other coroutine should have ticked
  # several times (0.3s / 0.01s ≈ 30, minus scheduling slack). If the
  # commit blocked the loop, this would be ~0.
  assert ticks_at_flush_return >= 5, (
    f"concurrent coroutine only ticked {ticks_at_flush_return} times "
    "during the commit — the loop was blocked (commit not offloaded)"
  )


@pytest.mark.asyncio
async def test_blocked_flush_then_finalize_drains_cleanly(db, chat, monkeypatch):
  """Finalize waits behind a blocked barrier, drains, and terminates."""
  import threading

  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)
  entered = threading.Event()
  release = threading.Event()

  def blocked_commit(_db, _chat_id, _message):
    entered.set()
    assert release.wait(timeout=2.0)
    return True

  monkeypatch.setattr(
    chat_mod, "_update_last_assistant_message", blocked_commit,
  )
  sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
  flush_task = asyncio.create_task(sink.flush())
  assert await asyncio.to_thread(entered.wait, 1.0)
  finalize_task = asyncio.create_task(sink.finalize())
  assert not finalize_task.done()
  release.set()
  assert await flush_task is True
  await asyncio.wait_for(finalize_task, timeout=1.0)
  assert sink._consumer_task.done()


@pytest.mark.asyncio
async def test_consumer_exception_fails_flush_and_finalize(db, chat, monkeypatch):
  """A consumer failure is surfaced instead of orphaning barrier waits."""
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  def explode(_event, _blocks):
    raise RuntimeError("consumer exploded")

  monkeypatch.setattr(chat_mod, "process_event", explode)
  sink.publish({"type": "text", "content": "hi"})
  with pytest.raises(RuntimeError, match="consumer exploded"):
    await asyncio.wait_for(sink.flush(), timeout=1.0)
  with pytest.raises(RuntimeError, match="consumer exploded"):
    await asyncio.wait_for(sink.finalize(), timeout=1.0)
  assert sink._consumer_task.done()


def test_sink_constructs_without_running_loop(db, chat):
  """Lazy consumer startup permits synchronous sink construction."""
  sink = chat_mod._ChatEventSink(_OrderedBroadcast(chat.id), chat.id, db)
  assert sink._consumer_task is None


# --- Bug 4: SystemBroadcast end-to-end --------------------------------


@pytest.mark.asyncio
async def test_system_broadcast_delivers_to_subscriber():
  """A subscriber to SystemBroadcast receives events published
  while it's listening — independent of any per-chat broadcast."""
  sb = SystemBroadcast()
  q = sb.subscribe()
  try:
    sb.publish({"type": "app_updated", "appId": "36"})
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event == {"type": "app_updated", "appId": "36"}
  finally:
    sb.unsubscribe(q)


@pytest.mark.asyncio
async def test_notify_endpoint_reaches_system_broadcast(client, auth):
  """POST /api/notify must publish to the SystemBroadcast — that is
  the channel Shell.jsx subscribes to so app_updated reaches the
  iframe-version-bumper even when no chat is active.

  Regression for the "agent updates app → spinner stuck forever"
  bug: previously the publish loop only hit per-chat broadcasts,
  which close 30s after each turn, so any later notify had nowhere
  to land.
  """
  # Subscribe to the system broadcast BEFORE the POST so we can
  # observe the event landing in real time.
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post(
      "/api/notify",
      headers=auth,
      json={"type": "app_updated", "appId": "36"},
    )
    assert r.status_code == 204, r.text

    event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event["type"] == "app_updated"
    assert event["appId"] == "36"
  finally:
    sb.unsubscribe(q)


def test_notify_body_type_validator_rejects_unknown():
  """NotifyBody rejects unknown system-event types."""
  try:
    NotifyBody(type="bogus")
  except ValidationError:
    pass
  else:
    raise AssertionError("Expected ValidationError for bogus event type")
