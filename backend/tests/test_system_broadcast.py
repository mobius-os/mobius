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
import json

import pytest
from pydantic import ValidationError

from app import broadcast as bc_mod
from app import chat as chat_mod
from app import models
from app.broadcast import (
  ChatBroadcast,
  SystemBroadcast,
  clear_active_broadcast_if,
  get_active_broadcast,
  get_system_broadcast,
  set_active_broadcast,
)
from app.chat_writer import Barrier, get_writer
from app.chat_transcript import materialized_messages
from app.deps import Principal
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


def test_chat_broadcast_fans_out_to_phone_and_web_subscribers():
  """One chat turn can be watched by more than one client at once."""
  bc = ChatBroadcast("shared-chat")
  catch_up_a, phone = bc.subscribe()
  catch_up_b, web = bc.subscribe()
  event = {"type": "text", "content": "live on both"}

  assert catch_up_a == []
  assert catch_up_b == []
  bc.publish(event)

  assert phone.get_nowait() == event
  assert web.get_nowait() == event
  assert len(bc.subscribers) == 2


def test_task_progress_coalesces_by_task_id_in_log():
  """A run of task_progress ticks for one sub-task collapses to ONE log entry
  at the tail, while lifecycle events stay discrete and live push carries every
  tick verbatim (card 187)."""
  bc = ChatBroadcast("progress-chat")
  _, sub = bc.subscribe()

  bc.publish({"type": "task_start", "task_id": "t1", "tool_use_id": "u1"})
  bc.publish({
    "type": "task_progress", "task_id": "t1", "last_tool_name": "Read",
  })
  bc.publish({"type": "tool_start", "tool": "Bash", "tool_use_id": "u2"})
  bc.publish({
    "type": "task_progress", "task_id": "t1", "last_tool_name": "Bash",
  })

  # The invariant is that the newest cumulative state stays newest in replay
  # chronology, even when unrelated activity separated it from the older tick.
  assert [e.get("type") for e in bc.event_log] == [
    "task_start", "tool_start", "task_progress",
  ]
  assert bc.event_log[-1]["last_tool_name"] == "Bash"
  t1_progress = [
    e for e in bc.event_log
    if e.get("type") == "task_progress" and e.get("task_id") == "t1"
  ]
  assert len(t1_progress) == 1

  bc.publish({
    "type": "task_done", "task_id": "t1", "status": "done", "tool_use_id": "u1",
  })

  # The discrete start and done markers remain in their original chronology.
  types = [e.get("type") for e in bc.event_log]
  assert types == ["task_start", "tool_start", "task_progress", "task_done"]
  progress = next(e for e in bc.event_log if e["type"] == "task_progress")
  assert progress["last_tool_name"] == "Bash"

  # A second concurrent sub-task keeps its own coalesced entry.
  bc.publish({
    "type": "task_progress", "task_id": "t2", "last_tool_name": "Grep",
  })
  bc.publish({
    "type": "task_progress", "task_id": "t2", "last_tool_name": "Write",
  })
  t2 = [e for e in bc.event_log if e.get("task_id") == "t2"]
  assert len(t2) == 1 and t2[0]["last_tool_name"] == "Write"
  assert bc.event_log[-1] == t2[0]

  # The live wire is never coalesced: subscribers saw every raw event.
  live = [sub.get_nowait() for _ in range(7)]
  assert [e.get("type") for e in live] == [
    "task_start", "task_progress", "tool_start", "task_progress",
    "task_done", "task_progress", "task_progress",
  ]


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
  messages = materialized_messages(row)
  assert messages and messages[-1].get("role") == "assistant"
  assert any(b.get("type") == "tool" for b in messages[-1]["blocks"])


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
  original = chat_writer_mod.update_live_assistant

  def spy(db_, chat_id, message):
    seen["commit_thread"] = threading.get_ident()
    return original(db_, chat_id, message)

  chat_writer_mod.update_live_assistant = spy
  try:
    seen["loop_thread"] = threading.get_ident()
    sink._last_save = 0.0
    sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
    _drain_actor()
  finally:
    chat_writer_mod.update_live_assistant = original

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
  blocks = materialized_messages(row)[-1]["blocks"]
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


@pytest.mark.asyncio
async def test_notify_shell_rebuilt_reaches_system_broadcast(client, auth):
  """POST /api/notify {shell_rebuilt} must land on the SystemBroadcast.

  This is the exact channel deploy-prod.sh's post-deploy broadcast hits:
  the script mints a service-token-authed POST so already-open PWAs reload
  onto the freshly-rebuilt shell. Shell.jsx subscribes to /api/events/system
  (which streams the SystemBroadcast) and reloads on `shell_rebuilt`. Before
  the deploy script fired this event, an already-open PWA never learned a new
  bundle existed. This test guards the backend half of that wiring: the
  event type is accepted (not rejected by the validator) AND it reaches the
  Shell-level broadcast even with no chat active.
  """
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post("/api/notify", headers=auth, json={"type": "shell_rebuilt"})
    assert r.status_code == 204, r.text
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event["type"] == "shell_rebuilt"
  finally:
    sb.unsubscribe(q)


@pytest.mark.asyncio
async def test_notify_shell_apply_now_reaches_system_broadcast(client, auth):
  """POST /api/notify {shell_apply_now} must be accepted and broadcast.

  Agents use this system event after a burst of shell edits so the Shell can
  apply rebuilt frontend changes at a safe moment instead of mid-turn.
  """
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post(
      "/api/notify",
      headers=auth,
      json={"type": "shell_apply_now"},
    )
    assert r.status_code == 204, r.text
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event["type"] == "shell_apply_now"
  finally:
    sb.unsubscribe(q)


def test_notify_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/notify",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
    json={"type": "app_updated", "appId": "36"},
  )
  assert cross.status_code == 403


def test_notify_body_type_validator_rejects_unknown():
  """NotifyBody rejects unknown system-event types."""
  assert NotifyBody(type="app_build_failed").type == "app_build_failed"
  assert NotifyBody(type="app_update_stale").type == "app_update_stale"
  try:
    NotifyBody(type="bogus")
  except ValidationError:
    pass
  else:
    raise AssertionError("Expected ValidationError for bogus event type")


# --- Single-bus routing: catch-up-safe fan-out vs system-bus-only -------
#
# The built-app CTA remains derived from the apps query's chat_id + updated_at.
# First placement is additionally triggered by the system-bus-only app_created
# lifecycle event, while app_updated remains the catch-up-safe recompile signal.
# What matters here is which events fan out to per-chat broadcasts and which
# ride the system bus alone.


@pytest.mark.asyncio
async def test_notify_app_updated_fans_out_to_live_chat_broadcast(client, auth):
  """A catch-up-SAFE event (app_updated) fans out to a running per-chat
  broadcast AND the system broadcast, so a chat reconnect's replay is a real
  backstop for a dropped system stream."""
  chat = bc_mod.create_broadcast("live-chat")
  q_chat = chat.subscribe()[1]
  sb = get_system_broadcast()
  q_sys = sb.subscribe()
  try:
    r = client.post(
      "/api/notify", headers=auth,
      json={"type": "app_updated", "appId": "42"},
    )
    assert r.status_code == 204, r.text
    ev_sys = await asyncio.wait_for(q_sys.get(), timeout=1.0)
    assert ev_sys["type"] == "app_updated"
    ev_chat = await asyncio.wait_for(q_chat.get(), timeout=1.0)
    assert ev_chat["type"] == "app_updated"
  finally:
    sb.unsubscribe(q_sys)
    bc_mod.remove_broadcast("live-chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["shell_rebuilt", "app_update_stale"])
async def test_notify_catch_up_unsafe_event_is_system_bus_only(
  client, auth, event_type,
):
  """A catch-up-UNSAFE event rides the system broadcast ALONE
  — never a per-chat broadcast — so a chat reconnect can't replay a stale
  action from its event log. SystemBroadcast has no replay, so one delivery per
  client and no frontend dedup."""
  chat = bc_mod.create_broadcast("live-chat-2")
  q_chat = chat.subscribe()[1]
  sb = get_system_broadcast()
  q_sys = sb.subscribe()
  try:
    r = client.post(
      "/api/notify", headers=auth, json={"type": event_type},
    )
    assert r.status_code == 204, r.text
    ev_sys = await asyncio.wait_for(q_sys.get(), timeout=1.0)
    assert ev_sys["type"] == event_type
    # The per-chat broadcast must NOT have received it (no fan-out, no replay).
    assert all(
      e.get("type") != event_type for e in chat.event_log
    ), chat.event_log
    with pytest.raises(asyncio.TimeoutError):
      await asyncio.wait_for(q_chat.get(), timeout=0.2)
  finally:
    sb.unsubscribe(q_sys)
    bc_mod.remove_broadcast("live-chat-2")


# --- Chat-scoped build_phase milestone rail (feature 212) --------------


@pytest.mark.asyncio
async def test_notify_build_phase_routes_by_explicit_chat_id(client, auth):
  """POST /api/notify {build_phase, chatId, label} lands ONLY on the NAMED
  chat's broadcast — even when a different chat started more recently and
  owns the active-broadcast pointer (run_chat overwrites it on every turn
  start). Routing by that pointer was the wrong-chat bug: chat A's milestone
  rendered in chat B's rail. The event carries label + ts so a reconnect's
  catch-up replay can rebuild the rail."""
  building = bc_mod.create_broadcast("phase-builder")
  later = bc_mod.create_broadcast("phase-later-turn")
  # A second chat started after the builder: the pointer tracks the LATEST
  # turn, so pointer-routing would deliver onto `later` instead.
  set_active_broadcast(later)
  sb = get_system_broadcast()
  sq = sb.subscribe()
  q_build = building.subscribe()[1]
  try:
    r = client.post(
      "/api/notify",
      headers=auth,
      json={
        "type": "build_phase",
        "chatId": "phase-builder",
        "label": "Storage wired",
      },
    )
    assert r.status_code == 204, r.text

    ev = await asyncio.wait_for(q_build.get(), timeout=1.0)
    assert ev["type"] == "build_phase"
    assert ev["label"] == "Storage wired"
    assert isinstance(ev["ts"], int)

    # The wrong-chat regression: the most-recently-started chat must NOT
    # receive another chat's milestone, and there is no system fan-out.
    assert all(
      e.get("type") != "build_phase" for e in later.event_log
    ), later.event_log
    assert sq.empty(), "build_phase must not reach the system broadcast"
  finally:
    sb.unsubscribe(sq)
    set_active_broadcast(None)
    bc_mod.remove_broadcast("phase-builder")
    bc_mod.remove_broadcast("phase-later-turn")


@pytest.mark.asyncio
async def test_notify_build_phase_dropped_when_chat_not_running(client, auth):
  """A build_phase for a chat whose turn already ended (broadcast present but
  no longer running) is accepted (204) and dropped — nothing published, no
  raise. A late milestone has no rail to feed."""
  ended = bc_mod.create_broadcast("phase-ended")
  ended.running = False
  before = len(ended.event_log)
  try:
    r = client.post(
      "/api/notify",
      headers=auth,
      json={"type": "build_phase", "chatId": "phase-ended", "label": "Late"},
    )
    assert r.status_code == 204, r.text
    assert len(ended.event_log) == before, ended.event_log
  finally:
    bc_mod.remove_broadcast("phase-ended")


@pytest.mark.asyncio
async def test_notify_build_phase_dropped_without_target_chat(client, auth):
  """No chatId, or a chatId with no broadcast at all, means no rail to feed:
  the POST is accepted (204) but publishes nothing — and never raises."""
  sb = get_system_broadcast()
  sq = sb.subscribe()
  try:
    for body in (
      {"type": "build_phase", "label": "First layer openable"},
      {"type": "build_phase", "chatId": "no-such-chat", "label": "x"},
    ):
      r = client.post("/api/notify", headers=auth, json=body)
      assert r.status_code == 204, r.text
    assert sq.empty(), "build_phase must not touch the system broadcast"
  finally:
    sb.unsubscribe(sq)


def test_notify_build_phase_label_capped_at_80():
  """A build_phase label longer than the cap is truncated server-side so a
  single POST cannot flood the rail (or the polite live region)."""
  body = NotifyBody(type="build_phase", label="x" * 200)
  assert len(body.label) == 80
  assert body.label == "x" * 80


def test_notify_rejects_label_on_non_build_phase():
  """`label` is confined to build_phase: any other type carrying a label is a
  malformed request so the closed schema keeps its meaning."""
  with pytest.raises(ValidationError):
    NotifyBody(type="app_updated", appId="7", label="nope")


def test_notify_rejects_chat_id_on_non_build_phase():
  """`chatId` is confined to build_phase exactly like `label` — every other
  event type keeps the closed type/appId schema."""
  with pytest.raises(ValidationError):
    NotifyBody(type="app_updated", appId="7", chatId="c1")


def test_notify_endpoint_rejects_label_on_non_build_phase(client, auth):
  """The endpoint 422s a label on a non-build_phase type."""
  r = client.post(
    "/api/notify",
    headers=auth,
    json={"type": "app_updated", "appId": "7", "label": "nope"},
  )
  assert r.status_code == 422, r.text


def test_notify_endpoint_rejects_chat_id_on_non_build_phase(client, auth):
  """The endpoint 422s a chatId on a non-build_phase type."""
  r = client.post(
    "/api/notify",
    headers=auth,
    json={"type": "shell_rebuilt", "chatId": "c1"},
  )
  assert r.status_code == 422, r.text


# --- clear_active_broadcast_if: identity-keyed compare-and-clear -------


def test_clear_active_broadcast_if_clears_when_pointer_matches():
  """When the active-broadcast pointer still points at `bc`, the
  identity-keyed clear releases it and returns True — the no-successor case
  where the dying run must release the pointer rather than leak it."""
  bc = ChatBroadcast("c-match")
  set_active_broadcast(bc)
  try:
    assert clear_active_broadcast_if(bc) is True
    assert get_active_broadcast() is None, "the matching pointer must be cleared"
  finally:
    set_active_broadcast(None)


def test_clear_active_broadcast_if_leaves_when_different_active():
  """When a DIFFERENT broadcast is active (a fresh owner replaced the pointer),
  the clear must NOT touch it: it returns False and leaves the live owner's
  pointer intact — never clobbering a turn that isn't `bc`."""
  fresh = ChatBroadcast("c-fresh")
  superseded = ChatBroadcast("c-superseded")
  set_active_broadcast(fresh)
  try:
    assert clear_active_broadcast_if(superseded) is False
    assert get_active_broadcast() is fresh, (
      "a non-matching clear must leave the fresh owner's pointer untouched"
    )
  finally:
    set_active_broadcast(None)


def test_clear_active_broadcast_if_returns_false_when_already_none():
  """When the pointer is already None, the clear is a no-op: returns False and
  leaves it None."""
  set_active_broadcast(None)
  bc = ChatBroadcast("c-none")
  assert clear_active_broadcast_if(bc) is False
  assert get_active_broadcast() is None


# --- SSE subscription-leak: subscribe pairs with unsubscribe inside ----
# --- the GET /stream generator's lifecycle -----------------------------


class _NeverDisconnectedRequest:
  """Minimal stand-in for the Starlette Request the stream handler reads.

  The generator only calls `request.is_disconnected()`; a completed
  broadcast returns before that loop, so this is never invoked there,
  but the abandon-early case may, so it answers honestly (connected)."""

  async def is_disconnected(self):
    return False


@pytest.mark.asyncio
async def test_stream_subscribe_happens_inside_generator_not_at_endpoint(
  db, chat,
):
  """The leak fix: bc.subscribe() must run INSIDE the GET /stream
  generator (when iteration starts), NOT when the endpoint builds the
  StreamingResponse. Otherwise a client that disconnects before the
  generator's body ever runs leaves a queue in bc.subscribers whose
  pairing `finally: unsubscribe` never fires — a leaked subscriber that
  lingers until the broadcast completes."""
  from app.routes.chats_stream import stream_chat

  bc = ChatBroadcast(chat.id)
  bc.publish({"type": "text", "content": "hello"})
  bc_mod._broadcasts[bc.chat_id] = bc
  try:
    baseline = len(bc.subscribers)

    resp = await stream_chat(
      request=_NeverDisconnectedRequest(), chat_id=bc.chat_id,
      principal=Principal(owner=db.query(models.Owner).one(), app_id=None),
      db=db,
    )

    # Building the response must NOT have subscribed yet — the generator
    # body has not run. A subscriber here is exactly the leak.
    assert len(bc.subscribers) == baseline, (
      "subscribe ran at the endpoint, before the generator's try/finally — "
      "a pre-iteration client disconnect would leak this queue"
    )

    # Abandon the stream WITHOUT iterating (the client disconnected before
    # the first iteration), then close the generator. No subscriber must
    # have leaked.
    await resp.body_iterator.aclose()
    assert len(bc.subscribers) == baseline, (
      "abandoning the stream before iterating leaked a subscriber"
    )
  finally:
    bc_mod._broadcasts.pop(bc.chat_id, None)


@pytest.mark.asyncio
async def test_stream_generator_unsubscribes_after_finally_runs(db, chat):
  """Driving the GET /stream generator to completion must leave
  bc.subscribers back at baseline: subscribe (inside the generator) and
  the unsubscribe in its finally are paired across the generator's whole
  lifecycle, so a finished/abandoned stream never leaks a subscriber."""
  from app.routes.chats_stream import stream_chat

  bc = ChatBroadcast(chat.id)
  bc.publish({"type": "text", "content": "hi"})
  # A completed broadcast WITHOUT a done event in the catch-up: the
  # generator synthesises a done and returns, exercising the early-return
  # path that still must run its finally.
  bc.running = False
  bc_mod._broadcasts[bc.chat_id] = bc
  try:
    baseline = len(bc.subscribers)

    resp = await stream_chat(
      request=_NeverDisconnectedRequest(), chat_id=bc.chat_id,
      principal=Principal(owner=db.query(models.Owner).one(), app_id=None),
      db=db,
    )

    saw_subscriber_mid_stream = False
    chunks = []
    async for chunk in resp.body_iterator:
      chunks.append(chunk)
      # While the generator is live (mid-iteration), the subscriber IS
      # registered — proof subscribe happened inside generate().
      if len(bc.subscribers) > baseline:
        saw_subscriber_mid_stream = True

    assert saw_subscriber_mid_stream, (
      "subscribe never ran inside the generator — the catch-up burst was "
      "served without ever registering a live subscriber"
    )
    payloads = [
      json.loads(line.removeprefix("data: "))
      for chunk in chunks
      for line in chunk.splitlines()
      if line.startswith("data: ")
    ]
    marker = next(event for event in payloads if event.get("type") == "catch_up_done")
    assert isinstance(marker.get("ts"), int) and marker["ts"] > 0, (
      "catch_up_done must carry the server replay clock so a remounted "
      "thinking timer can recover time since its last delta"
    )
    # The finally ran on generator exhaustion → back to baseline.
    assert len(bc.subscribers) == baseline, (
      "the generator finished but its finally never unsubscribed — leak"
    )
  finally:
    bc_mod._broadcasts.pop(bc.chat_id, None)
