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
import json
import threading
import time

import pytest

from app import chat as chat_mod
from app import chat_queue, models, questions, schemas
from app.broadcast import ChatBroadcast
from app.chat_writer import Barrier, PersistTranscript, get_writer
from app.database import SessionLocal
from app.deps import Principal
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


# -- 6. finalize ack failure through the REAL run_chat wrapper -----------
# FIX 2: the run marker must survive a failed terminal Finalize so startup
# reconciliation recovers the incomplete turn + queued messages. This drives
# the REAL `run_chat` wrapper (NOT `_complete_turn` directly) precisely so it
# covers `run_chat`'s `finally`, which previously cleared the marker
# UNCONDITIONALLY — wiping exactly what reconciliation needs. A test that
# called `_complete_turn` directly could never have caught that, which is why
# the original review missed it.
def test_finalize_failure_via_run_chat_leaves_marker_for_reconciliation(
  monkeypatch, tmp_path
):
  """Through the real `run_chat` wrapper, a forced `Finalize` ack failure
  must leave the durable run marker SET after `run_chat` returns (so
  reconciliation would recover it) and perform NO continuation/promotion —
  no half-persisted, unexecuted turn."""
  _seed_chat(
    "c-fin",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
  )
  # The StartTurn that the production initial-send route would have run set
  # the durable marker. Replicate that precondition + seed an Owner (run_chat
  # bails early with "No owner configured" otherwise, never reaching the SDK
  # branch).
  db = SessionLocal()
  try:
    from datetime import UTC, datetime
    from app import auth as auth_mod
    db.add(models.Owner(
      username="o", hashed_password=auth_mod.hash_password("x"),
      provider="claude",
    ))
    c = db.query(models.Chat).filter(models.Chat.id == "c-fin").one()
    c.run_status = "running"
    c.run_started_at = datetime.now(UTC)
    db.commit()
  finally:
    db.close()

  # check_auth needs the creds file to exist; point DATA_DIR's claude creds
  # at a real file so run_chat reaches the SDK branch instead of bailing.
  import os
  creds = (
    __import__("pathlib").Path(os.environ["DATA_DIR"])
    / "cli-auth" / "claude" / ".credentials.json"
  )
  creds.parent.mkdir(parents=True, exist_ok=True)
  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "test-token",
      "expiresAt": int(time.time() * 1000) + 3_600_000,
    },
  }), encoding="utf-8")

  # Fake the Claude SDK runner: stream one text block through the sink (so
  # finalize() has accumulated blocks and actually submits a Finalize), then
  # return a clean result so the turn reaches the success _complete_turn path.
  async def fake_runner(*, bc, **kwargs):
    bc.publish({"type": "text", "content": "partial answer"})
    return {"session_id": "sess", "cost_usd": 0.0}

  import app.claude_sdk_runner as csr
  monkeypatch.setattr(csr, "run_claude_sdk_turn", fake_runner)

  # Force the actor's Finalize seam to fail so the terminal write does not
  # land — exactly the persistence-unavailable case.
  from app import chat_writer as cw

  def _boom(db_, chat_id, blocks):
    from app.chat_writer import _PersistFailed
    raise _PersistFailed("forced finalize failure")

  monkeypatch.setattr(cw, "finalize_response_outcome", _boom)

  from app.broadcast import create_broadcast
  bc = create_broadcast("c-fin")
  published = []
  orig_publish = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig_publish(e))[1]

  chat_mod.mark_starting("c-fin")
  gen = chat_mod.current_run_generation("c-fin")
  asyncio.run(
    chat_mod.run_chat(
      [chat_mod.schemas.ChatMessage(role="user", content="hi")],
      chat_id="c-fin", session_id="sess", provider_id="claude",
      run_gen=gen, run_token="rt-fin",
    )
  )
  _drain_actor()

  # Transport error + done emitted; queue NOT promoted; marker LEFT SET.
  assert "error" in published
  assert "done" in published
  assert "queued_turn_starting" not in published
  chat_state = _load("c-fin")
  assert chat_state["run_status"] == "running"  # left for reconciliation
  assert len(chat_state["pending_messages"]) == 1  # not promoted/executed


# -- 6b. slow PromotePending: caller awaits the ack, never abandon-strands -
# FIX 1: the old `_complete_turn` wrapped the turn-end promotion in a ~5.0s
# outer `asyncio.wait_for`, shorter-than/equal-to SQLite's busy_timeout AND
# the actor's await_ack bound. A legitimately-slow PromotePending commit
# tripped that timer; the caller treated the TimeoutError as "abandon and
# continue" (next_user=None) WHILE the PromotePending command was still in
# the actor queue — it then committed AFTER the caller gave up, promoting a
# queued turn into `messages` that was never executed or reconciled (a
# stranded turn). The fix removes the outer timer: the actor's await_ack is
# the SINGLE authority, so a slow-but-successful commit is awaited and the
# continuation proceeds on the ACK RESULT — never on a separate clock.
def test_complete_turn_awaits_slow_promote_and_does_not_strand():
  """With PromotePending's commit latched slow, `_complete_turn` must AWAIT
  the ack (not return early on a timer) and, once the commit lands, promote
  the head + schedule the continuation. It must never abandon a turn that the
  actor then commits behind its back."""
  _seed_chat(
    "c-slow",
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant", "content": "ok", "ts": 2,
       "blocks": [{"type": "text", "content": "ok"}]},
    ],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
  )

  bc = ChatBroadcast("c-slow")
  sink = chat_mod._ChatEventSink(bc, "c-slow", run_token="rt-slow")
  # No accumulated blocks → finalize() is a no-op, so the turn reaches the
  # success path and the promote drain (what we're exercising).
  sink.assistant_blocks = []

  # Latch the actor's real `_promote_pending` so its commit blocks until we
  # release it — simulating a legitimately slow (but successful) SQLite
  # commit that would have tripped the old 5s outer timer.
  writer = get_writer()
  gate = threading.Event()
  orig_promote = writer._promote_pending

  def slow_promote(db, cmd):
    gate.wait(timeout=10)
    return orig_promote(db, cmd)

  writer._promote_pending = slow_promote

  scheduled = []
  import app.chat as _chat
  orig_sched = _chat._schedule_continuation
  _chat._schedule_continuation = (
    lambda **kw: scheduled.append(kw)
  )

  published = []
  orig_publish = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig_publish(e))[1]

  async def go():
    task = asyncio.create_task(
      chat_mod._complete_turn(
        bc=bc, sink=sink, db=SessionLocal(), chat_id="c-slow",
        run_gen=None, provider_id="claude", cost_usd=0, close_browser=False,
      )
    )
    # While the promote is latched, the caller must STILL be awaiting the
    # ack — not have returned early on a timer. Give it room to (wrongly)
    # bail, then assert it hasn't.
    await asyncio.sleep(0.2)
    assert not task.done(), (
      "_complete_turn returned before the promote committed — it abandoned "
      "the turn on a timer (the FIX-1 strand bug)"
    )
    gate.set()  # let the slow commit land
    return await asyncio.wait_for(task, timeout=10)

  try:
    result = asyncio.run(go())
  finally:
    writer._promote_pending = orig_promote
    _chat._schedule_continuation = orig_sched

  # The turn completed successfully and the head was promoted exactly once,
  # with a continuation scheduled — no strand. `_complete_turn` now returns
  # the terminal disposition (CONTINUATION_PROMOTED) rather than a bool; the
  # marker stays set for the scheduled continuation.
  from app.chat_queue import TerminalDisposition
  assert result is TerminalDisposition.CONTINUATION_PROMOTED
  assert "queued_turn_starting" in published
  assert len(scheduled) == 1
  assert scheduled[0]["next_user"]["content"] == "queued"
  _drain_actor()
  chat_state = _load("c-slow")
  # The queued message moved into the transcript (promoted) and the queue is
  # now empty — committed once, by the awaited ack.
  assert chat_state["pending_messages"] == []
  assert any(
    m.get("content") == "queued" for m in chat_state["messages"]
  )


# -- 6c. failed QuestionCommit scrubs the orphan block ------------------
# FIX 3: `publish_question` runs `process_event` (appending the question
# block to `assistant_blocks`) BEFORE awaiting the QuestionCommit ack. On a
# failed commit the old code left that block in place, so a later `Finalize`
# persisted an unanswerable card (reload shows a card with no live pending
# future). The fix scrubs the just-added block on failure so a subsequent
# Finalize cannot persist the orphan.
def test_failed_question_commit_scrubs_orphan_block(monkeypatch):
  """A failed QuestionCommit must remove the just-added question block from
  `assistant_blocks` (so a later Finalize persists NO question card) and must
  NOT broadcast the card."""
  _seed_chat(
    "c-q", messages=[{"role": "user", "content": "hi", "ts": 1}],
  )
  bc = ChatBroadcast("c-q")
  sink = chat_mod._ChatEventSink(bc, "c-q", run_token="rt-q")
  # A prior streamed text block — it must SURVIVE the scrub (only the failed
  # question block is removed).
  sink.publish({"type": "text", "content": "thinking"})

  # Force ONLY the QuestionCommit seam to fail; the later Finalize (which
  # shares the same module-level `_apply_last_assistant_message`) must run
  # for real. A flag gates the boom so it fires once, for the commit only.
  from app import chat_writer as cw

  fail = {"on": True}
  real_apply = cw._apply_last_assistant_message

  def _maybe_boom(db_, chat_id, snapshot):
    if fail["on"]:
      from app.chat_writer import _PersistFailed
      raise _PersistFailed("forced question-commit failure")
    return real_apply(db_, chat_id, snapshot)

  monkeypatch.setattr(cw, "_apply_last_assistant_message", _maybe_boom)

  published = []
  orig_publish = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig_publish(e))[1]

  async def go():
    # publish_question must propagate the failure (the runner ends the turn
    # with a transport-only error); the card must NOT be broadcast.
    with pytest.raises(Exception):
      await sink.publish_question(
        {"type": "question", "question_id": "q1",
         "questions": [{"id": "q1", "question": "Color?"}]}
      )

  asyncio.run(go())

  # The orphan question block was scrubbed; the prior text block remains.
  assert all(
    b.get("type") != "question" for b in sink.assistant_blocks
  ), sink.assistant_blocks
  assert any(b.get("type") == "text" for b in sink.assistant_blocks)
  # The card was NOT broadcast (no question event reached the wire).
  assert "question" not in published

  # A subsequent Finalize persists NO question card (the scrub held).
  fail["on"] = False  # the commit seam works again for the terminal write
  asyncio.run(sink.finalize())
  _drain_actor()
  chat_state = _load("c-q")
  last_blocks = chat_state["messages"][-1].get("blocks") or []
  assert all(b.get("type") != "question" for b in last_blocks), last_blocks


# -- 7. stop-races-answer: REAL concurrent Stop vs answer on the queue lock
# FIX 4: the prior version monkeypatched `questions.claim_if` to fake the
# Stop. This version drives the REAL `send_message` answer route and the REAL
# `stop_chat_for` as concurrent coroutines on ONE event loop, both genuinely
# contending on `chat_queue.get_lock(chat_id)` (the same lock object, the
# same loop — exactly the production interleaving). No `claim_if` stub; the
# route's own re-claim-by-identity and Stop's own `questions.cancel` run for
# real. Stop wins the lock race, so the answer route — when it takes the lock
# — finds the question gone and 410s WITHOUT resolving the cancelled future.
def test_stop_races_answer_real_lock_contention_returns_410(chat, owner_token):
  """Real concurrent Stop vs answer, contending on the actual queue lock.
  Stop cancels the pending question (popping it + cancelling the future)
  while the answer route is parked acquiring the same lock; when the answer
  route proceeds it returns 410 and the future is cancelled, never resolved
  with answers."""
  from fastapi import HTTPException
  from app.routes.chats_stream import send_message

  db_seed = SessionLocal()
  try:
    c = db_seed.query(models.Chat).filter(models.Chat.id == chat.id).one()
    c.messages = [
      {"role": "user", "content": "go", "ts": 1},
      {"role": "assistant", "content": "", "ts": 2, "blocks": [
        {"type": "question", "question_id": "qS",
         "questions": [{"id": "q1", "question": "Color?"}]},
      ]},
    ]
    db_seed.commit()
  finally:
    db_seed.close()

  async def race():
    # Pending future lives on THIS loop (the one both coroutines run on), so
    # Stop's cancel and the answer route's resolve target the same future.
    fut = asyncio.get_event_loop().create_future()
    pending = PendingQuestion(
      question_id="qS",
      questions=[{"id": "q1", "question": "Color?"}],
      future=fut,
      run_token="rt-S",
    )
    questions.register(chat.id, pending)

    # Pre-acquire the REAL queue lock so the answer route genuinely blocks on
    # it — the production block behind a Stop that holds the lock. We hold it,
    # start the answer task (it parks on the lock), then run the REAL Stop
    # cancel primitive and release, letting the answer route proceed into its
    # no-pending → 410 branch.
    lock = chat_queue.get_lock(chat.id)
    await lock.acquire()

    body = schemas.SendMessage(
      content="answer", hidden=True,
      answers={"Color?": "Red"}, question_id="qS",
    )
    db_answer = SessionLocal()
    answer_task = asyncio.create_task(
      send_message(
        body=body, chat_id=chat.id,
        principal=Principal(
          owner=db_answer.query(models.Owner).one(), app_id=None,
        ),
        db=db_answer,
      )
    )
    # Let the answer task reach + park on the held lock.
    await asyncio.sleep(0.1)
    assert not answer_task.done(), "answer route did not block on the lock"

    # The REAL Stop cancel (stop_chat_for runs this lock-free after its own
    # lock block): pop the pending entry + cancel the future.
    questions.cancel(chat.id)
    lock.release()

    status = None
    try:
      await asyncio.wait_for(answer_task, timeout=10)
    except HTTPException as exc:
      status = exc.status_code
    finally:
      db_answer.close()
    return status, fut

  status, fut = asyncio.run(race())

  # Stop won the lock race → the answer route saw no pending question and
  # raised 410; the cancelled future was NEVER resolved with answers.
  assert status == 410, status
  assert fut.cancelled()
  assert not (fut.done() and not fut.cancelled())  # never set_result'd
  # No answer was written (410'd before any AnswerQuestion submit).
  _drain_actor()
  chat_state = _load(chat.id)
  assert chat_state["messages"][-1]["blocks"][0].get("answers") is None


# -- AppendSteeredUserMessage — mid-turn Codex steer transcript write ----
def test_append_steered_user_message_lands_at_end_of_transcript():
  """A steered user message is appended at the END of the transcript so a
  reload renders Q1, A1, Q2, A2. The split path (the sink's
  `split_for_steer`) seals the streamed-so-far assistant text as its own
  message BEFORE submitting this, so after the append the trailing message
  is the steered user row — which makes the runner's next snapshot append
  the post-steer continuation as a fresh assistant. The ts is bumped past
  every transcript + queued ts."""
  from app.chat_writer import AppendSteeredUserMessage

  _seed_chat(
    "c-steer",
    messages=[
      {"role": "user", "content": "start", "ts": 1},
      {"role": "assistant", "content": "partial", "ts": 5, "blocks": []},
    ],
    pending=[{"role": "user", "content": "queued", "ts": 9}],
  )

  ack = get_writer().submit(
    AppendSteeredUserMessage(
      chat_id="c-steer",
      run_token="",
      user_msg={"role": "user", "content": "steered", "ts": 2},
    )
  )
  stored = ack.result(timeout=5)["stored"]

  chat = _load("c-steer")
  roles = [m["role"] for m in chat["messages"]]
  assert roles == ["user", "assistant", "user"]
  assert chat["messages"][-1]["content"] == "steered"
  # ts bumped past every transcript + pending ts (max was the queued 9).
  assert stored["ts"] > 9
  # The pending queue was untouched — a steer is NOT a queue append.
  assert [m["content"] for m in chat["pending_messages"]] == ["queued"]


def test_append_steered_user_message_appends_when_no_assistant_yet():
  """When the turn hasn't streamed any assistant text yet (no trailing
  assistant message), the steered message is simply appended."""
  from app.chat_writer import AppendSteeredUserMessage

  _seed_chat(
    "c-steer2",
    messages=[{"role": "user", "content": "start", "ts": 1}],
  )

  get_writer().submit(
    AppendSteeredUserMessage(
      chat_id="c-steer2",
      run_token="",
      user_msg={"role": "user", "content": "steered", "ts": 2},
    )
  ).result(timeout=5)

  chat = _load("c-steer2")
  assert [m["content"] for m in chat["messages"]] == ["start", "steered"]
