"""Tests for the load-bearing event-bus contracts:

- Bug 3 (now Candidate B): a `question` event MUST be persisted to the
  DB BEFORE its card is broadcast. The frontend renders the card on
  broadcast; a user Submit that races the save would find no question
  block in `chat.messages` and the answer would be silently dropped. C2
  enforces this via `_ChatEventSink.publish_question`, which submits a
  `QuestionCommit` to the writer actor, AWAITS its ack, and only then
  broadcasts — and `publish()` REJECTS question events so no runner can
  bypass the barrier.

- Bug 4: System events (theme_updated, app_updated, shell_*) MUST
  reach the `SystemBroadcast` so a Shell subscriber sees them
  regardless of whether a per-chat broadcast is currently active.

- Commit offload (C2): the streaming save's blocking `db.commit()` runs
  OFF the event loop on the writer-actor thread. `publish()` submits a
  fire-and-forget `PersistTranscript` (or `PersistError`). A slow commit
  on one chat can't stall the loop and starve other chats' SSE because it
  never runs on the loop.
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
from app.chat_writer import Barrier, get_writer
from app.routes.notify import NotifyBody


# --- Bug 3 / Candidate B: question save-before-broadcast --------------


class _OrderedBroadcast(ChatBroadcast):
  """ChatBroadcast that records each publish so a test can inspect what
  reached the wire (and in what order relative to the actor commit)."""

  def __init__(self, chat_id):
    super().__init__(chat_id)
    self.timeline = []
    # At the instant a `question` event is broadcast, was the question
    # block ALREADY durable? A fresh-session re-read taken inside publish()
    # is the only way to ENFORCE the ordering. Two independent post-hoc
    # checks — the block is persisted after publish_question returns AND
    # the event reached the timeline — both pass even for a regression
    # that broadcasts BEFORE awaiting the QuestionCommit ack, because by
    # the time the test re-reads the DB the (out-of-order) commit has
    # already landed. Capturing durability AT broadcast time is what
    # closes that gap.
    self.question_block_persisted_at_publish = None

  def publish(self, event):
    if event.get("type") == "question":
      from app.database import SessionLocal
      s = SessionLocal()
      try:
        row = (
          s.query(models.Chat)
          .filter(models.Chat.id == self.chat_id)
          .one_or_none()
        )
        blocks = (
          row.messages[-1].get("blocks") if row and row.messages else []
        ) or []
        self.question_block_persisted_at_publish = any(
          b.get("type") == "question" for b in blocks
        )
      finally:
        s.close()
    self.timeline.append(("publish", event.get("type")))
    super().publish(event)


def _drain_actor():
  """Block until the writer actor has processed everything queued so far.

  publish() submits fire-and-forget commands; a Barrier ack resolves only
  after every prior command has been dispatched, so awaiting it from a
  test gives a deterministic "the commit has landed" point."""
  get_writer().submit(Barrier()).result(timeout=5)


def test_question_event_is_saved_before_broadcast(db, chat):
  """User-Submit-races-save regression: an AskUserQuestion card must land
  in chat.messages BEFORE the broadcast reaches a frontend that might
  POST an answer. C2 enforces this in publish_question via QuestionCommit
  (commit-before-ack); the card is broadcast only after the ack."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, run_token="rt-q")

  questions = [{
    "id": "q1",
    "question": "Color?",
    "options": [{"label": "Red"}, {"label": "Blue"}],
  }]

  async def go():
    # When publish_question returns, the QuestionCommit ack has resolved
    # (the block is durable) AND the card has been broadcast — in that
    # order, by construction.
    await sink.publish_question(
      {"type": "question", "question_id": "q1", "questions": questions}
    )
    # The block is already persisted at this point: re-read on a fresh
    # session proves it landed before the broadcast that just happened.
    from app.database import SessionLocal
    s = SessionLocal()
    try:
      row = s.query(models.Chat).filter(models.Chat.id == chat.id).one()
      blocks = row.messages[-1].get("blocks") or []
      assert any(b.get("type") == "question" for b in blocks), (
        "question block must be persisted before the broadcast"
      )
    finally:
      s.close()

  asyncio.run(go())
  # The card reached the wire.
  assert ("publish", "question") in bc.timeline
  # The ORDERING guarantee: the question block was already durable AT the
  # moment its card was broadcast. This is what makes the test catch a
  # regression that reorders publish() ahead of the QuestionCommit ack —
  # the two checks above would still pass for such a regression.
  assert bc.question_block_persisted_at_publish is True, (
    "save-before-broadcast: the question block MUST be durable at the moment "
    "its card is broadcast — broadcasting before the QuestionCommit ack would "
    "let a racing user Submit find no question block to attach the answer to"
  )


def test_publish_rejects_question_events(db, chat):
  """publish() must REJECT question events so a runner can't bypass the
  save-before-broadcast barrier — they MUST go through publish_question."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, run_token="rt-q")
  with pytest.raises(AssertionError):
    sink.publish({"type": "question", "questions": [{"id": "q1"}]})


def test_non_question_save_routes_to_actor_off_loop(db, chat):
  """Non-question events broadcast immediately and route their DB commit
  to the writer actor (off the event loop). publish() submits a
  fire-and-forget PersistTranscript; the block is durable once the actor
  drains."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, run_token="rt-t")

  sink._last_save = 0.0
  ok = sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
  assert ok is True
  # Broadcast happened synchronously on the calling thread.
  assert ("publish", "tool_start") in bc.timeline
  # The actor commits off-loop; drain it, then the tool block is durable.
  _drain_actor()
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert row.messages and row.messages[-1].get("role") == "assistant"
  assert any(b.get("type") == "tool" for b in row.messages[-1]["blocks"])


def test_streaming_commit_runs_off_the_event_loop_thread(db, chat):
  """The streaming commit must execute on the writer-actor thread, not
  the loop thread — that is the whole point of the offload (a slow SQLite
  commit can't stall the loop). The actor owns a dedicated thread named
  'chat-writer'; the publish call on the loop only enqueues."""
  import threading

  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, run_token="rt-t")

  seen: dict = {}
  from app import chat_writer as chat_writer_mod
  original = chat_writer_mod.update_last_assistant_message

  def spy(db_, chat_id, message):
    seen["commit_thread"] = threading.get_ident()
    return original(db_, chat_id, message)

  chat_writer_mod.update_last_assistant_message = spy
  try:
    seen["loop_thread"] = threading.get_ident()
    sink._last_save = 0.0
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    _drain_actor()
  finally:
    chat_writer_mod.update_last_assistant_message = original

  assert "commit_thread" in seen, "the actor never ran the commit"
  assert seen["commit_thread"] != seen["loop_thread"], (
    "the streaming commit ran on the event loop thread — it must run on "
    "the writer-actor thread so a SQLite lock can't stall the loop"
  )


def test_finalize_awaits_actor_and_persists(db, chat):
  """finalize() submits a Finalize and AWAITS its ack: the terminal
  message is durable the instant finalize() returns (commit-before-ack)."""
  chat.messages = [{"role": "user", "content": "hi", "ts": 1}]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, run_token="rt-f")
  sink.assistant_blocks = [{"type": "text", "content": "done"}]

  asyncio.run(sink.finalize())

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert row.messages[-1].get("role") == "assistant"
  assert row.messages[-1]["blocks"][-1]["content"] == "done"


def test_error_event_routes_to_persist_error(db, chat):
  """An `error` event routes to a PersistError (non-coalescing) so it
  can't be collapsed away by a later text snapshot; it lands durably."""
  chat.messages = [
    {"role": "user", "content": "hi", "ts": 1},
    {"role": "assistant", "content": "", "ts": 2,
     "blocks": [{"type": "text", "content": "partial"}]},
  ]
  db.commit()
  bc = _OrderedBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(bc, chat.id, run_token="rt-e")
  sink.assistant_blocks = [{"type": "text", "content": "partial"}]

  sink._last_save = 0.0
  sink.publish({"type": "error", "message": "boom"})
  _drain_actor()

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  blocks = row.messages[-1]["blocks"]
  assert any(
    b.get("type") == "error" and b.get("message") == "boom" for b in blocks
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
