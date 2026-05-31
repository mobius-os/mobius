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
  run OFF the event loop. `publish()` defers non-question saves to an
  async `flush()` that commits via `asyncio.to_thread`; the question
  save stays inline (the save-before-broadcast invariant above). A
  slow commit on one chat must not stall the loop and starve other
  chats' SSE.
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
  (which happens via `_update_last_assistant_message` on the same
  thread) lands BEFORE the publish for question events.
  """

  def __init__(self, chat_id):
    super().__init__(chat_id)
    self.timeline = []

  def publish(self, event):
    self.timeline.append(("publish", event.get("type")))
    super().publish(event)


def test_question_event_is_saved_before_broadcast(db, chat):
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


def test_non_question_save_is_deferred_to_flush_off_loop(db, chat):
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
    # publish() broadcast but did NOT save: no save in the timeline,
    # and a snapshot is parked for flush().
    assert ("publish", "tool_start") in bc.timeline
    assert not any(kind == "save" for kind, _ in bc.timeline), (
      f"publish() must defer the commit, not run it inline. "
      f"timeline={bc.timeline}"
    )
    assert sink._pending_save is not None

    # flush() performs the deferred commit (off-loop via to_thread).
    ok = asyncio.run(sink.flush())
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


def test_flush_runs_commit_off_the_event_loop_thread(db, chat):
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

  try:
    asyncio.run(_scenario())
  finally:
    chat_mod._update_last_assistant_message = original

  assert "thread" in seen, "flush() never ran the commit"
  assert seen["thread"] != seen["loop_thread"], (
    "the streaming commit ran on the event loop thread — it must be "
    "offloaded via asyncio.to_thread so a SQLite lock can't stall the "
    "loop and starve other chats' SSE"
  )


def test_question_commit_stays_inline_and_blocks_the_loop_thread(db, chat):
  """The question save MUST stay synchronous (save-before-broadcast
  invariant), i.e. run inline on the calling thread inside publish()
  — NOT deferred. This is the deliberate exception to the offload."""
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
  try:
    seen["caller_thread"] = threading.get_ident()
    sink.publish({
      "type": "question",
      "questions": [{
        "id": "q1",
        "question": "Color?",
        "options": [{"label": "Red"}, {"label": "Blue"}],
      }],
    })
  finally:
    chat_mod._update_last_assistant_message = original

  assert seen.get("thread") == seen["caller_thread"], (
    "question commit must run inline on the calling thread"
  )
  assert sink._pending_save is None


def test_flush_surfaces_commit_failure_via_return(db, chat, monkeypatch):
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
  flush_ok = asyncio.run(sink.flush())

  # publish() reports True (it didn't commit inline); flush() exposes
  # the False from the failed commit.
  assert publish_ok is True
  assert flush_ok is False


def test_slow_flush_does_not_block_a_concurrent_coroutine(db, chat, monkeypatch):
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
    return ticks_at_flush_return

  ticks_at_flush_return = asyncio.run(_scenario())

  # During the ~0.3s commit the other coroutine should have ticked
  # several times (0.3s / 0.01s ≈ 30, minus scheduling slack). If the
  # commit blocked the loop, this would be ~0.
  assert ticks_at_flush_return >= 5, (
    f"concurrent coroutine only ticked {ticks_at_flush_return} times "
    "during the commit — the loop was blocked (commit not offloaded)"
  )


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
