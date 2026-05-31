"""Integration tests for the C2 atomic activation — the WIRED path.

The command-level mechanics (FIFO, coalescing, fencing, failure
propagation) live in `test_chat_writer.py`; the command-level real-DB
dispatch lives in `test_chat_writer_contention.py`. THIS module drives
the activated production seams end-to-end — the sink, the routes, and
the queue all routing through the actor — to prove the wiring closes the
lost-update race and honours the failure semantics the design specifies.

The conftest `fresh_db` fixture starts a real writer actor per test bound
to the test DB, so these exercise the actual `get_writer()` path.
"""

import asyncio
import threading
import time

import pytest

from app import chat as chat_mod
from app import models, questions
from app.broadcast import ChatBroadcast
from app.chat_writer import Barrier, PersistTranscript, get_writer
from app.database import SessionLocal
from app.pending_questions import PendingQuestion


def _seed_chat(chat_id, messages=None, pending=None, session_id="sess"):
  db = SessionLocal()
  try:
    db.add(
      models.Chat(
        id=chat_id,
        title="t",
        messages=messages if messages is not None else [],
        pending_messages=pending if pending is not None else [],
        session_id=session_id,
        provider="claude",
      )
    )
    db.commit()
  finally:
    db.close()
  return chat_id


def _load(chat_id):
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return None if chat is None else {
      "messages": list(chat.messages or []),
      "pending_messages": list(chat.pending_messages or []),
      "run_status": chat.run_status,
    }
  finally:
    db.close()


def _drain_actor():
  get_writer().submit(Barrier()).result(timeout=5)


# -- 1. sink streaming snapshot then question commit survives ------------
def test_sink_stream_then_question_survives_via_actor():
  """A streaming PersistTranscript followed by a save-before-broadcast
  question (publish_question → QuestionCommit) keeps both: the actor
  serializes them, and the QuestionCommit fences the stale snapshot."""
  _seed_chat("c-sq", messages=[{"role": "user", "content": "hi", "ts": 1}])
  bc = ChatBroadcast("c-sq")
  sink = chat_mod._ChatEventSink(bc, "c-sq", run_token="rt-sq")

  async def go():
    sink._last_save = 0.0
    sink.publish({"type": "text", "content": "thinking"})
    await sink.publish_question(
      {"type": "question", "question_id": "q1",
       "questions": [{"id": "q1", "question": "Color?"}]}
    )

  asyncio.run(go())
  _drain_actor()
  chat = _load("c-sq")
  blocks = chat["messages"][-1]["blocks"]
  assert any(b.get("question_id") == "q1" for b in blocks)


# -- 2. wired answer survives a concurrent streaming snapshot ------------
def test_wired_answer_survives_concurrent_stream_snapshot(client, auth, chat):
  """The lost-update race Option C closes, end-to-end through the route:
  a streaming snapshot (no answers) pending under the streaming token must
  NOT clobber the answer the route writes via AnswerQuestion. The fence
  happens on SUBMIT, so the stale snapshot is invalidated the instant the
  route submits AnswerQuestion (no need to pause the actor — submitting a
  fence is synchronous)."""
  db = SessionLocal()
  try:
    c = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
    c.messages = [
      {"role": "user", "content": "go", "ts": 1},
      {"role": "assistant", "content": "", "ts": 2, "blocks": [
        {"type": "question", "question_id": "qX",
         "questions": [{"id": "q1", "question": "Color?"}]},
      ]},
    ]
    db.commit()
  finally:
    db.close()

  fut = asyncio.new_event_loop().create_future()
  pending = PendingQuestion(
    question_id="qX",
    questions=[{"id": "q1", "question": "Color?"}],
    future=fut,
    run_token="rt-stream",
  )
  questions.register(chat.id, pending)

  # Deterministically interleave: hold the consumer at the stale
  # snapshot's marker (after dequeue, before _take_pending) until the
  # route's AnswerQuestion submit has FENCED it. The fence (generation
  # bump + pop pending) runs synchronously inside `submit()`, resolving
  # the dropped snapshot's ack to None right there — so the held marker is
  # released the moment `stale` is done, and the consumer then takes a
  # snapshot that's already been invalidated (no-op). Releasing on
  # `stale.done()` (set by submit) avoids a deadlock: the route's await on
  # its OWN ack still needs the actor to advance.
  release = threading.Event()

  def hold_until_fenced():
    release.wait(timeout=5)

  get_writer()._on_snapshot_ready_for_test = hold_until_fenced

  def watch():
    # The route's AnswerQuestion submit fences the pending snapshot,
    # resolving `stale` to None synchronously; release the consumer then.
    while not stale.done():
      time.sleep(0.005)
    release.set()

  try:
    stale = get_writer().submit(
      PersistTranscript(
        chat_id=chat.id, run_token="rt-stream",
        snapshot={"role": "assistant", "content": "", "blocks": [
          {"type": "question", "question_id": "qX",
           "questions": [{"id": "q1", "question": "Color?"}]},
        ]},
      )
    )
    watcher = threading.Thread(target=watch)
    watcher.start()
    res = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "answer", "hidden": True,
            "answers": {"Color?": "Red"}, "question_id": "qX"},
      headers=auth,
    )
    release.set()
    watcher.join(timeout=5)
  finally:
    get_writer()._on_snapshot_ready_for_test = None
    release.set()

  assert res.status_code == 202, res.text
  # The stale snapshot was fenced on the AnswerQuestion submit.
  assert stale.result(timeout=5) is None
  _drain_actor()
  chat_state = _load(chat.id)
  block = chat_state["messages"][-1]["blocks"][0]
  assert block.get("answers") == {"Color?": "Red"}


# -- 3. answer route returns 410 when no pending question ----------------
def test_answer_410_when_no_pending(client, auth, chat):
  """No pending question registered → the answer route returns 410 after
  the grace window without touching the transcript (stop-races-answer's
  'card no longer live' surface)."""
  res = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "answer", "hidden": True, "answers": {"Color?": "Red"}},
    headers=auth,
  )
  assert res.status_code == 410, res.text


# -- 4. concurrent queue appends through the route never lose a message --
def test_concurrent_route_appends_preserve_every_message(client, auth, chat):
  """Several POSTs racing while a turn is 'running' all land in the queue
  — the actor's AppendPending serializes the RMW so none is lost."""
  from app.runner_registry import RunnerKind, registry
  from dataclasses import dataclass

  @dataclass
  class _Running:
    chat_id: str
    kind: RunnerKind = RunnerKind.SUBPROCESS

    async def stop(self, timeout: float = 2.0):
      return True

  registry.register(_Running(chat_id=chat.id))
  try:
    results = []
    lock = threading.Lock()

    def send(i):
      r = client.post(
        f"/api/chats/{chat.id}/messages",
        json={"content": f"m{i}"},
        headers=auth,
      )
      with lock:
        results.append(r.status_code)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(8)]
    for t in threads:
      t.start()
    for t in threads:
      t.join(timeout=10)
  finally:
    registry.unregister(chat.id, RunnerKind.SUBPROCESS)

  assert all(s == 202 for s in results), results
  _drain_actor()
  chat_state = _load(chat.id)
  contents = {m["content"] for m in chat_state["pending_messages"]}
  assert contents == {f"m{i}" for i in range(8)}, contents
  # Every queued ts is unique (the actor bumps colliders).
  ts = [m["ts"] for m in chat_state["pending_messages"]]
  assert len(set(ts)) == len(ts)


# -- 5. PUT transcript replace serializes with a streaming snapshot ------
def test_put_replace_transcript_broad_fences_stream_snapshot(client, auth, chat):
  """PUT /api/chats/{id} with messages routes through ReplaceTranscript,
  which broad-fences EVERY in-flight streaming snapshot for the chat
  (under ANY run_token) on submit — so a concurrent stream save under a
  DIFFERENT token can't clobber the replacement."""
  db = SessionLocal()
  try:
    c = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
    c.messages = [{"role": "user", "content": "hi", "ts": 1}]
    db.commit()
  finally:
    db.close()

  # Hold the consumer at the stale snapshot's marker until the PUT's
  # ReplaceTranscript submit has broad-fenced it (synchronous in
  # `submit()`, resolving `stale` to None). The stale snapshot is under
  # the streaming token; ReplaceTranscript uses an EMPTY token but
  # broad-fences ALL of the chat's keys. Releasing on `stale.done()`
  # avoids deadlocking the PUT's own await.
  release = threading.Event()

  def hold_until_fenced():
    release.wait(timeout=5)

  get_writer()._on_snapshot_ready_for_test = hold_until_fenced

  def watch():
    while not stale.done():
      time.sleep(0.005)
    release.set()

  try:
    stale = get_writer().submit(
      PersistTranscript(
        chat_id=chat.id, run_token="rt-stream",
        snapshot={"role": "assistant", "content": "",
                  "blocks": [{"type": "text", "content": "stream-stale"}]},
      )
    )
    watcher = threading.Thread(target=watch)
    watcher.start()
    res = client.put(
      f"/api/chats/{chat.id}",
      json={"messages": [
        {"role": "user", "content": "hi", "ts": 1},
        {"role": "assistant", "content": "", "ts": 2,
         "blocks": [{"type": "text", "content": "replaced"}]},
      ]},
      headers=auth,
    )
    release.set()
    watcher.join(timeout=5)
  finally:
    get_writer()._on_snapshot_ready_for_test = None
    release.set()

  assert res.status_code == 200, res.text
  # The other-token streaming snapshot was broad-fenced on the PUT submit.
  assert stale.result(timeout=5) is None
  _drain_actor()
  chat_state = _load(chat.id)
  assert chat_state["messages"][-1]["blocks"][0]["content"] == "replaced"


# -- 6. finalize ack failure → no queue promote, marker left set ---------
def test_finalize_failure_leaves_marker_and_does_not_promote(monkeypatch):
  """If the terminal Finalize ack raises, _complete_turn must emit a
  transport error + done, NOT drain the queue, and leave the run marker
  set for reconciliation. No direct-write fallback."""
  _seed_chat(
    "c-fin",
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant", "content": "", "ts": 2,
       "blocks": [{"type": "text", "content": "partial"}]},
    ],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
  )
  # Mark the run (StartTurn would have done this in production).
  db = SessionLocal()
  try:
    c = db.query(models.Chat).filter(models.Chat.id == "c-fin").one()
    from datetime import UTC, datetime
    c.run_status = "running"
    c.run_started_at = datetime.now(UTC)
    db.commit()
  finally:
    db.close()

  bc = ChatBroadcast("c-fin")
  sink = chat_mod._ChatEventSink(bc, "c-fin", run_token="rt-fin")
  sink.assistant_blocks = [{"type": "text", "content": "done"}]

  # Force Finalize to fail by making the actor's finalize seam raise.
  from app import chat_writer as cw

  def _boom(db_, chat_id, blocks):
    from app.chat_writer import _PersistFailed
    raise _PersistFailed("forced finalize failure")

  monkeypatch.setattr(cw, "finalize_response_outcome", _boom)

  published = []
  orig_publish = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig_publish(e))[1]

  asyncio.run(
    chat_mod._complete_turn(
      bc=bc, sink=sink, db=SessionLocal(), chat_id="c-fin",
      run_gen=None, provider_id="claude", cost_usd=0, close_browser=False,
    )
  )

  # Transport error + done were emitted; queue NOT promoted; marker set.
  assert "error" in published
  assert "done" in published
  assert "queued_turn_starting" not in published
  chat_state = _load("c-fin")
  assert chat_state["run_status"] == "running"  # left for reconciliation
  assert len(chat_state["pending_messages"]) == 1  # not promoted


# -- 7. stop-races-answer: future not resolved when Stop wins the race ---
def test_stop_races_answer_route_returns_410(client, auth, chat, monkeypatch):
  """If a Stop removes the pending question WHILE the answer's
  AnswerQuestion ack is in flight, the route's re-claim-by-identity finds
  the entry gone and returns 410 WITHOUT resolving the (now cancelled)
  future — even though the answer itself committed durably.

  The race window is between the route's peek and its post-ack re-claim.
  We make it deterministic by simulating the concurrent Stop inside
  `questions.claim_if`: the first call (the route's re-claim) cancels the
  future and reports the entry gone, exactly as a real Stop interleaving
  the await would."""
  db = SessionLocal()
  try:
    c = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
    c.messages = [
      {"role": "user", "content": "go", "ts": 1},
      {"role": "assistant", "content": "", "ts": 2, "blocks": [
        {"type": "question", "question_id": "qS",
         "questions": [{"id": "q1", "question": "Color?"}]},
      ]},
    ]
    db.commit()
  finally:
    db.close()

  fut = asyncio.new_event_loop().create_future()
  pending = PendingQuestion(
    question_id="qS",
    questions=[{"id": "q1", "question": "Color?"}],
    future=fut,
    run_token="rt-S",
  )
  questions.register(chat.id, pending)

  # Simulate a concurrent Stop that cancelled this question while the
  # AnswerQuestion ack was in flight: when the route re-claims by
  # identity, the entry is gone (Stop popped it + cancelled the future).
  import app.routes.chats_stream as cs

  def stop_won(chat_id, expected):
    questions.cancel(chat_id)  # Stop's path: pop + cancel the future
    return False

  monkeypatch.setattr(cs.questions, "claim_if", stop_won)

  res = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "answer", "hidden": True,
          "answers": {"Color?": "Red"}, "question_id": "qS"},
    headers=auth,
  )
  # Re-claim failed (Stop took the entry) → 410; the cancelled future was
  # NEVER resolved with answers.
  assert res.status_code == 410, res.text
  assert fut.cancelled()
  # The answer DID commit durably — the write is independent of the dead
  # future (the actor applied it before the re-claim check).
  _drain_actor()
  chat_state = _load(chat.id)
  assert chat_state["messages"][-1]["blocks"][0].get("answers") == {
    "Color?": "Red"
  }
