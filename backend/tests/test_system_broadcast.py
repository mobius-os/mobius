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


def test_text_event_keeps_legacy_publish_then_save_order(db, chat):
  """Text events stay on the throttled save-after-publish path so
  per-token streaming latency isn't penalized by a DB write."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)
  original = chat_mod._update_last_assistant_message
  def spy(db_, chat_id, message):
    bc.timeline.append(("save", chat_id))
    original(db_, chat_id, message)
  chat_mod._update_last_assistant_message = spy
  try:
    # First text event: throttle has never fired, so no save (last_save=0
    # was set in __init__, and the elapsed check uses 1s interval). We
    # force the throttle to fire by aging _last_save.
    sink._last_save = 0.0
    # Take a deliberate slow path — emit a tool_start which is in
    # IMMEDIATE_SAVE_TYPES so we DO get a save event, and check that
    # for non-question types the save still lands AFTER publish.
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
  finally:
    chat_mod._update_last_assistant_message = original

  pub_idx = next(
    i for i, (kind, _) in enumerate(bc.timeline) if kind == "publish"
  )
  save_idx = next(
    (i for i, (kind, _) in enumerate(bc.timeline) if kind == "save"),
    None,
  )
  assert save_idx is not None, f"timeline={bc.timeline}"
  assert pub_idx < save_idx, (
    "Non-question events must keep the legacy publish-then-save "
    f"order (text streaming latency). timeline={bc.timeline}"
  )


def test_sink_publish_returns_false_on_safe_commit_failure(db, chat, monkeypatch):
  """publish() exposes DB write failure via its boolean return."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  db.refresh(chat)
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, db)

  monkeypatch.setattr(chat_mod, "_safe_commit", lambda _db: False)

  ok = sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})

  assert ok is False


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
