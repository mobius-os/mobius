"""Terminal-completion state-machine tests (design 2026-06-01, §F).

Every test here drives the REAL `run_chat` wrapper (never `_complete_turn`
directly) so it covers `run_chat`'s `finally` + the locked terminal
transition together — the layer where the marker-clear decision now lives.
Tests 3 and 4 use DETERMINISTIC SMALL-TIMEOUT SEAMS (monkeypatching the
module constants small) so the bound trips without waiting 65 real seconds.
Test 3 and the failure-half of test 8 use a reconcile-after-restart
simulation (the `reconcile_interrupted_chats` pure function, exactly as
`test_crash_recovery.py` does) to prove the marker left set is recoverable.

The conftest `fresh_db` fixture starts a real writer actor per test bound to
the test DB, so `get_writer()` is the real path throughout.
"""

import asyncio
import os
import pathlib
import threading

import pytest

from app import chat as chat_mod
from app import chat_queue, chat_writer, models
from app.broadcast import ChatBroadcast, create_broadcast
from app.chat_writer import Barrier, get_writer
from app.database import SessionLocal


# -- shared harness ------------------------------------------------------
def _drain_actor():
  get_writer().submit(Barrier()).result(timeout=5)


def _seed_owner_and_creds():
  """Seed an Owner + a Claude creds file so `run_chat` reaches the SDK
  branch instead of bailing at the no-owner / auth-error guard."""
  from app import auth as auth_mod

  db = SessionLocal()
  try:
    if db.query(models.Owner).first() is None:
      db.add(models.Owner(
        username="o", hashed_password=auth_mod.hash_password("x"),
        provider="claude",
      ))
      db.commit()
  finally:
    db.close()
  creds = (
    pathlib.Path(os.environ["DATA_DIR"]) / "cli-auth" / "claude"
    / ".credentials.json"
  )
  creds.parent.mkdir(parents=True, exist_ok=True)
  creds.write_text("{}", encoding="utf-8")


def _seed_chat(chat_id, messages=None, pending=None, run_status=None,
               session_id="sess"):
  from datetime import UTC, datetime

  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id, title="t",
      messages=messages if messages is not None else [],
      pending_messages=pending if pending is not None else [],
      session_id=session_id, provider="claude",
    )
    if run_status is not None:
      chat.run_status = run_status
      chat.run_started_at = datetime.now(UTC)
    db.add(chat)
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


def _run_real_chat(chat_id, *, run_token, run_gen, provider_id="claude",
                   published=None):
  """Drive the REAL run_chat for `chat_id` (Claude branch) on a fresh
  broadcast, capturing published event types into `published` if given."""
  bc = create_broadcast(chat_id)
  if published is not None:
    orig = bc.publish
    bc.publish = lambda e: (published.append(e.get("type")), orig(e))[1]
  asyncio.run(
    chat_mod.run_chat(
      [chat_mod.schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat_id, session_id="sess", provider_id=provider_id,
      run_gen=run_gen, run_token=run_token,
    )
  )


def _patch_claude_runner(monkeypatch, *, text="partial answer"):
  """Make the Claude SDK runner stream one text block (so finalize() has
  blocks to commit) then return a clean result."""
  async def fake_runner(*, bc, **kwargs):
    if text is not None:
      bc.publish({"type": "text", "content": text})
    return {"session_id": "sess", "cost_usd": 0.0}

  import app.claude_sdk_runner as csr
  monkeypatch.setattr(csr, "run_claude_sdk_turn", fake_runner)


# -- 1. empty-queue final continuation CLEARS the marker -----------------
def test_empty_queue_terminal_clears_marker(monkeypatch):
  """A normal turn with an empty pending queue: the marker is cleared
  durably INSIDE the locked terminal transition, no queued_turn_starting is
  emitted, and the chat is forgotten only AFTER the clear (the generation
  counter is dropped via forget_chat once the marker is gone)."""
  _seed_owner_and_creds()
  _seed_chat(
    "t1", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], run_status="running",
  )
  _patch_claude_runner(monkeypatch)

  chat_mod.mark_starting("t1")
  gen = chat_mod.current_run_generation("t1")
  published = []
  _run_real_chat("t1", run_token="rt-1", run_gen=gen, published=published)
  _drain_actor()

  state = _load("t1")
  assert state["run_status"] is None, "empty-queue terminal must clear marker"
  assert "queued_turn_starting" not in published
  assert "done" in published
  # The chat was forgotten (generation dropped) after the clear.
  assert chat_mod.current_run_generation("t1") == 0
  assert not chat_mod.registry.is_alive("t1")


# -- 2. terminal write failure LEAVES the marker (+ reconcile repairs) ---
def test_terminal_finalize_failure_leaves_marker_then_reconcile_repairs(
  monkeypatch
):
  """A forced Finalize ack failure leaves the marker SET (running), drains
  NO queue, schedules NO continuation; a subsequent reconcile-after-restart
  clears the marker, drops the queued message, and appends an
  interrupted-turn note."""
  _seed_owner_and_creds()
  _seed_chat(
    "t2", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )
  _patch_claude_runner(monkeypatch)

  def _boom(db_, chat_id, blocks):
    from app.chat_writer import _PersistFailed
    raise _PersistFailed("forced finalize failure")

  monkeypatch.setattr(chat_writer, "finalize_response_outcome", _boom)

  chat_mod.mark_starting("t2")
  gen = chat_mod.current_run_generation("t2")
  published = []
  _run_real_chat("t2", run_token="rt-2", run_gen=gen, published=published)
  _drain_actor()

  state = _load("t2")
  assert "error" in published
  assert "queued_turn_starting" not in published
  assert state["run_status"] == "running", "failed terminal must LEAVE marker"
  assert len(state["pending_messages"]) == 1, "queue not consumed"

  # Reconcile-after-restart: the registry is empty (the run finished), so
  # reconcile sees the stranded marker and repairs it.
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "t2" in reconciled
  state = _load("t2")
  assert state["run_status"] is None, "reconcile must clear the marker"
  assert state["pending_messages"] == [], "reconcile drops the stranded queue"
  err = [b for b in state["messages"][-1]["blocks"] if b["type"] == "error"]
  assert err and "interrupted" in err[0]["message"].lower()


# -- 3. await_ack boundary trips mid-promote (small-timeout seam) --------
def test_promote_ack_timeout_leaves_marker_then_reconcile_resolves(
  monkeypatch
):
  """Latch the actor inside `_promote_pending` AFTER dispatch so its commit
  blocks past a deterministically-small ACK_TIMEOUT_SECS. The drain's
  await_ack times out → FAILED_LEAVE_MARKER: no continuation, marker left.
  Then the latched commit is released (it lands behind the caller's back —
  the accept-and-document outcome), and a reconcile-after-restart resolves
  the promoted-but-unscheduled turn."""
  _seed_owner_and_creds()
  _seed_chat(
    "t3",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )
  # No streamed text → finalize() is a no-op, so the turn reaches the drain
  # (the promote) which is what we're latching.
  _patch_claude_runner(monkeypatch, text=None)

  # Deterministic small ACK bound: the promote commit is latched longer than
  # this, so await_ack trips its asyncio.wait_for timeout in the failure
  # branch BEFORE we release the latch.
  monkeypatch.setattr(chat_writer, "ACK_TIMEOUT_SECS", 0.2)

  writer = get_writer()
  release = threading.Event()
  orig_promote = writer._promote_pending

  def latched_promote(db, cmd):
    release.wait(timeout=10)  # block the actor inside the commit
    return orig_promote(db, cmd)

  writer._promote_pending = latched_promote

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t3")
  gen = chat_mod.current_run_generation("t3")
  published = []
  try:
    _run_real_chat("t3", run_token="rt-3", run_gen=gen, published=published)
  finally:
    # The ack already timed out; release the latch so the commit lands behind
    # the caller's back (the accept-and-document landed-after-timeout case),
    # then restore.
    release.set()
    _drain_actor()
    writer._promote_pending = orig_promote
    chat_mod._schedule_continuation = orig_sched

  # The caller saw the timeout: no continuation scheduled, transport error
  # surfaced, marker LEFT set.
  assert scheduled == [], "a timed-out promote must NOT schedule a continuation"
  assert "queued_turn_starting" not in published
  assert "error" in published
  state = _load("t3")
  assert state["run_status"] == "running", "timed-out promote must LEAVE marker"

  # Reconcile-after-restart resolves whichever outcome the latched commit
  # produced (here it landed after the timeout: the head moved into messages
  # + the marker stayed set). Reconcile clears the marker + appends the
  # interrupted-turn note; the chat converges to a non-spinning state.
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "t3" in reconciled
  state = _load("t3")
  assert state["run_status"] is None
  assert state["pending_messages"] == []
  assert state["messages"][-1]["role"] == "assistant"
  assert any(b["type"] == "error" for b in state["messages"][-1]["blocks"])


# -- 4. drain-lock bound exceeded (small TERMINAL_LOCK_TIMEOUT seam) -----
def test_drain_lock_bound_exceeded_leaves_marker_then_reconcile_clears(
  monkeypatch
):
  """Hold the per-chat queue lock from another task longer than a
  deterministically-small TERMINAL_LOCK_TIMEOUT_SECS. The drain's bounded
  lock acquisition trips → FAILED_LEAVE_MARKER: bounded return, no
  continuation, marker left. A reconcile-after-restart then clears it."""
  _seed_owner_and_creds()
  _seed_chat(
    "t4",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )
  _patch_claude_runner(monkeypatch, text=None)
  # Small terminal-lock bound so the held lock trips it deterministically.
  monkeypatch.setattr(chat_queue, "TERMINAL_LOCK_TIMEOUT_SECS", 0.2)

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t4")
  gen = chat_mod.current_run_generation("t4")
  published = []

  async def drive():
    # Hold the lock for longer than the bound so the terminal drain's
    # acquisition times out.
    lock = chat_queue.get_lock("t4")
    await lock.acquire()
    try:
      await chat_mod.run_chat(
        [chat_mod.schemas.ChatMessage(role="user", content="hi")],
        chat_id="t4", session_id="sess", provider_id="claude",
        run_gen=gen, run_token="rt-4",
      )
    finally:
      lock.release()

  bc = create_broadcast("t4")
  orig = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig(e))[1]
  try:
    asyncio.run(drive())
  finally:
    chat_mod._schedule_continuation = orig_sched
  _drain_actor()

  assert scheduled == [], "a lock-bound timeout must NOT schedule a continuation"
  assert "queued_turn_starting" not in published
  assert "error" in published
  state = _load("t4")
  assert state["run_status"] == "running", "lock-bound timeout must LEAVE marker"
  assert len(state["pending_messages"]) == 1, "queue not consumed on timeout"

  # Reconcile-after-restart clears it.
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "t4" in reconciled
  assert _load("t4")["run_status"] is None


# -- 5a. appended scrub: orphan removed by identity, neighbours survive --
def test_failed_question_commit_appended_scrub_by_identity(monkeypatch):
  """A failed QuestionCommit where the question was APPENDED: only that
  block is removed (by identity), the prior text block and a block appended
  DURING the ACK wait (a concurrent same-loop append) both survive, and no
  orphan card is persisted by a later Finalize.

  The concurrent append is injected WHILE publish_question is awaiting the
  QuestionCommit ack (the window the wrong tail-slice scrub would have
  clobbered): we latch the actor's commit handler, run publish_question as a
  task, append the text block while the handler is blocked, then release the
  latch so the commit fails — exercising the exact interleaving the
  identity-based scrub guards against."""
  _seed_chat("t5a", messages=[{"role": "user", "content": "hi", "ts": 1}])
  bc = ChatBroadcast("t5a")
  sink = chat_mod._ChatEventSink(bc, "t5a", run_token="rt-5a")
  sink.publish({"type": "text", "content": "thinking"})

  # Latch the actor's QuestionCommit handler so it blocks INSIDE the commit
  # (after dispatch, while publish_question awaits the ack), then fails. While
  # it is blocked, a concurrent same-loop append lands.
  in_commit = threading.Event()
  release = threading.Event()
  real_apply = chat_writer._apply_last_assistant_message

  def latched_boom(db_, chat_id, snapshot):
    in_commit.set()
    release.wait(timeout=10)
    from app.chat_writer import _PersistFailed
    raise _PersistFailed("forced question-commit failure")

  monkeypatch.setattr(chat_writer, "_apply_last_assistant_message", latched_boom)

  published = []
  orig = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig(e))[1]

  async def go():
    task = asyncio.create_task(sink.publish_question(
      {"type": "question", "question_id": "q1",
       "questions": [{"id": "q1", "question": "Color?"}]}
    ))
    # Wait until the actor is blocked INSIDE the commit (the ack is pending),
    # then inject the concurrent append DURING the ACK wait.
    while not in_commit.is_set():
      await asyncio.sleep(0.005)
    sink.publish({"type": "text", "content": " more"})
    # Release the latch so the commit fails and the scrub undo runs.
    release.set()
    with pytest.raises(Exception):
      await task

  asyncio.run(go())

  # The orphan question block is gone; neither neighbour (the prior "thinking"
  # text, nor the " more" text appended DURING the ACK wait) was collaterally
  # deleted.
  assert all(b.get("type") != "question" for b in sink.assistant_blocks), (
    sink.assistant_blocks
  )
  texts = [b.get("content") for b in sink.assistant_blocks
           if b.get("type") == "text"]
  joined = "".join(texts)
  assert "thinking" in joined, "prior text block must survive the scrub"
  assert "more" in joined, (
    "a text block appended DURING the ACK wait must survive the identity scrub"
  )
  assert "question" not in published, "the card must NOT be broadcast"

  # A subsequent Finalize persists NO question card (the scrub held).
  monkeypatch.setattr(chat_writer, "_apply_last_assistant_message", real_apply)
  asyncio.run(sink.finalize())
  _drain_actor()
  blocks = _load("t5a")["messages"][-1].get("blocks") or []
  assert all(b.get("type") != "question" for b in blocks), blocks


# -- 5b. coalesced scrub: prior payload restored, nothing else deleted ---
def test_failed_question_commit_coalesced_scrub_restores_fields(monkeypatch):
  """A failed QuestionCommit where the question COALESCED into a
  pre-existing block (same identity, changed payload): the existing block
  stays present, its prior payload is restored, and nothing else is
  deleted."""
  _seed_chat("t5b", messages=[{"role": "user", "content": "hi", "ts": 1}])
  bc = ChatBroadcast("t5b")
  sink = chat_mod._ChatEventSink(bc, "t5b", run_token="rt-5b")

  # First, a SUCCESSFUL question commit so a question block with identity
  # "q1" already exists in assistant_blocks with its original payload.
  async def first():
    await sink.publish_question(
      {"type": "question", "question_id": "q1",
       "questions": [{"id": "q1", "question": "Color?"}]}
    )

  asyncio.run(first())
  _drain_actor()
  existing = [b for b in sink.assistant_blocks if b.get("type") == "question"]
  assert len(existing) == 1
  original_questions = list(existing[0]["questions"])

  # Now force the NEXT commit (a coalescing update to the SAME identity with
  # a changed payload) to fail.
  fail = {"on": True}
  real_apply = chat_writer._apply_last_assistant_message

  def maybe_boom(db_, chat_id, snapshot):
    if fail["on"]:
      from app.chat_writer import _PersistFailed
      raise _PersistFailed("forced coalesced-commit failure")
    return real_apply(db_, chat_id, snapshot)

  monkeypatch.setattr(chat_writer, "_apply_last_assistant_message", maybe_boom)

  async def second():
    with pytest.raises(Exception):
      await sink.publish_question(
        {"type": "question", "question_id": "q1",
         "questions": [{"id": "q1", "question": "Color?",
                        "options": ["red", "blue"]}]}
      )

  asyncio.run(second())

  qs = [b for b in sink.assistant_blocks if b.get("type") == "question"]
  assert len(qs) == 1, "the pre-existing block must NOT be deleted"
  assert qs[0]["questions"] == original_questions, (
    "the coalesced scrub must restore the prior payload, not the failed one"
  )
  assert qs[0].get("question_id") == "q1"


# -- 6. Stop handoff: marker clears only after terminal persistence ------
def test_stop_handoff_clears_only_immediate_successor_marker(monkeypatch):
  """A Stop-bumped run reaches the terminal transition with we_own_gen=False
  (STALE_NO_ACTION — no promotion). `run_chat`'s finally clears the marker
  for the immediate successor generation it still owns; a NEWER run's marker
  is never cleared."""
  _seed_owner_and_creds()
  _seed_chat(
    "t6", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], run_status="running",
  )
  _patch_claude_runner(monkeypatch)

  chat_mod.mark_starting("t6")
  gen = chat_mod.current_run_generation("t6")
  # Simulate Stop exactly as stop_chat_for does for an active handle: register
  # the stopped-generation handoff, bump the generation, AND release _starting
  # (stop_chat_for calls registry.discard_starting at the end). Releasing
  # _starting is load-bearing for FIX B's clear gate — a leftover claim would
  # read as "a newer owner reclaimed the chat" and (correctly) suppress the
  # clear, which is why a faithful Stop simulation must drop it here.
  chat_mod._clear_after_terminal_generation["t6"] = gen
  successor_gen = chat_mod.bump_run_generation("t6")
  assert successor_gen == gen + 1
  chat_mod.discard_starting("t6")

  published = []
  _run_real_chat("t6", run_token="rt-6", run_gen=gen, published=published)
  _drain_actor()

  # The dying run did NOT promote a queue / schedule a continuation, and the
  # Stop-handoff clear ran for the immediate successor generation.
  assert "queued_turn_starting" not in published
  assert _load("t6")["run_status"] is None, (
    "Stop handoff must clear the marker after terminal persistence"
  )

  # A newer run's marker must NEVER be cleared by a stale Stop-bumped run.
  _seed_chat("t6b", run_status="running")
  chat_mod._clear_after_terminal_generation["t6b"] = 0  # stopped gen 0
  chat_mod.bump_run_generation("t6b")  # successor gen 1 (the immediate one)
  chat_mod.bump_run_generation("t6b")  # a NEWER run claimed gen 2
  published_b = []
  # This run owns gen 0 but the current gen is now 2 (not 0+1=1), so it is a
  # stale run and must touch nothing.
  bc = create_broadcast("t6b")
  orig = bc.publish
  bc.publish = lambda e: (published_b.append(e.get("type")), orig(e))[1]
  asyncio.run(
    chat_mod.run_chat(
      [chat_mod.schemas.ChatMessage(role="user", content="hi")],
      chat_id="t6b", session_id="sess", provider_id="claude",
      run_gen=0, run_token="rt-6b",
    )
  )
  _drain_actor()
  assert _load("t6b")["run_status"] == "running", (
    "a stale Stop-bumped run must NOT clear a newer run's marker"
  )


# -- 7. unsupported runtime cleanup: marker cleared, pending dropped -----
def test_unsupported_provider_cleanup_clears_marker_and_pending(monkeypatch):
  """An unsupported provider hits the setup-error terminal cleanup: the
  pending queue is cleared durably, the marker is cleared before the
  registry release, and no continuation is scheduled."""
  _seed_owner_and_creds()
  _seed_chat(
    "t7", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )

  # Force an unsupported provider: stub get_provider to return a provider
  # whose name matches neither SDK branch and whose check_auth passes.
  class _Unsupported:
    name = "Bogus"

    def check_auth(self, data_dir):
      return None

    def build_env(self, **kwargs):
      return {}

  monkeypatch.setattr(chat_mod, "get_provider", lambda pid: _Unsupported())

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t7")
  gen = chat_mod.current_run_generation("t7")
  published = []
  try:
    _run_real_chat("t7", run_token="rt-7", run_gen=gen, published=published)
  finally:
    chat_mod._schedule_continuation = orig_sched
  _drain_actor()

  assert "error" in published
  assert "queued_turn_starting" not in published
  assert scheduled == []
  state = _load("t7")
  assert state["run_status"] is None, "unsupported cleanup must clear marker"
  assert state["pending_messages"] == [], "pending must be cleared durably"
  assert not chat_mod.registry.is_alive("t7"), "registry released"


# -- 8. actor fatal: bounded failure, marker left, reconcile repairs -----
def test_actor_fatal_leaves_marker_then_reconcile_repairs(monkeypatch):
  """When the writer actor is fatal, every terminal ack fails fast (bounded,
  not a hang): the turn surfaces a transport error, schedules no
  continuation, and LEAVES the marker set. A reconcile-after-restart then
  repairs it."""
  _seed_owner_and_creds()
  _seed_chat(
    "t8", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )
  _patch_claude_runner(monkeypatch)

  # Drive the actor fatal so its Finalize ack fails immediately (the bounded
  # failure path — no 65s wait).
  get_writer()._go_fatal()

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t8")
  gen = chat_mod.current_run_generation("t8")
  published = []
  try:
    _run_real_chat("t8", run_token="rt-8", run_gen=gen, published=published)
  finally:
    chat_mod._schedule_continuation = orig_sched

  assert "error" in published
  assert "queued_turn_starting" not in published
  assert scheduled == []
  state = _load("t8")
  assert state["run_status"] == "running", "actor-fatal terminal must LEAVE marker"
  assert len(state["pending_messages"]) == 1, "queue not consumed"

  # Reconcile-after-restart (the fatal actor is restarted by the fixture's
  # teardown; here we just run the pure recovery against the DB).
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "t8" in reconciled
  state = _load("t8")
  assert state["run_status"] is None
  assert state["pending_messages"] == []


# -- 9 (FIX A). post-promote continuation scheduling failure LEAVES marker -
def test_continuation_schedule_failure_after_promote_leaves_marker(monkeypatch):
  """The promote landed (the queued head moved into `messages` and the
  continuation's run marker was set by PromotePending), but spawning the
  continuation task then raises. The turn is now promoted-but-unscheduled, so
  the marker MUST be left set (NOT cleared) — clearing it would strand the
  promoted turn with no recovery handle. A transport error + done is surfaced
  on the continuation's broadcast, and a reconcile-after-restart recovers the
  promoted-but-unscheduled turn (appends an interrupted-turn note + clears)."""
  _seed_owner_and_creds()
  _seed_chat(
    "t9",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )
  _patch_claude_runner(monkeypatch)

  # Force the continuation scheduling to raise AFTER the promote lands.
  # `_schedule_continuation` builds its broadcast via chat.py's module-level
  # `create_broadcast`; making that raise simulates a scheduling failure that
  # occurs once the promote (in drain_and_release) has already committed.
  boom_calls = {"n": 0}
  real_create = chat_mod.create_broadcast

  def boom_create(chat_id_):
    boom_calls["n"] += 1
    raise RuntimeError("forced continuation scheduling failure")

  monkeypatch.setattr(chat_mod, "create_broadcast", boom_create)

  chat_mod.mark_starting("t9")
  gen = chat_mod.current_run_generation("t9")
  published = []
  _run_real_chat("t9", run_token="rt-9", run_gen=gen, published=published)
  _drain_actor()

  assert boom_calls["n"] == 1, "the continuation scheduler must have been hit"
  # The drain promoted the head (queued_turn_starting was emitted before the
  # continuation spawn), so the promote DID land before the failure.
  assert "queued_turn_starting" in published
  state = _load("t9")
  assert state["run_status"] == "running", (
    "a promoted-but-unscheduled turn MUST leave the marker set"
  )
  # The promote moved the head into messages — the user message is now last.
  assert state["pending_messages"] == [], "the head was promoted out of pending"
  assert state["messages"][-1]["content"] == "queued"

  # Reconcile-after-restart recovers the promoted-but-unscheduled turn.
  monkeypatch.setattr(chat_mod, "create_broadcast", real_create)
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "t9" in reconciled
  state = _load("t9")
  assert state["run_status"] is None, "reconcile must clear the marker"
  assert state["messages"][-1]["role"] == "assistant"
  assert any(b["type"] == "error" for b in state["messages"][-1]["blocks"])


# -- 10 (FIX B). Stop-handoff clear must NOT erase a racing fresh marker ----
def test_stop_handoff_clear_does_not_erase_racing_fresh_start_turn_marker(
  monkeypatch
):
  """The critical race the brief flags: a dying Stop-bumped run is about to
  clear the marker for the immediate successor generation it owns, but a FRESH
  StartTurn (a new send) raced in first via `mark_starting`. Because
  `mark_starting` does NOT bump the generation, the dying run's gen-only check
  (`current == run_gen + 1`) STILL passes even though a new run now owns the
  chat — so the old (gen-only, outside-the-lock) clear would wipe the new run's
  marker. The fix re-checks ownership UNDER the bounded queue lock and requires
  that no newer owner has reclaimed the chat (`not is_chat_running`); the fresh
  send's mark_starting makes the chat alive again, so the dying run leaves the
  marker. The new run's marker SURVIVES."""
  _seed_owner_and_creds()
  _seed_chat("t10", messages=[{"role": "user", "content": "hi", "ts": 1}],
             pending=[], run_status="running")
  _patch_claude_runner(monkeypatch)

  # Set up the dying run as a Stop handoff: it owns `gen`; Stop bumped to the
  # immediate successor (gen + 1) and registered the stopped-generation handoff
  # exactly as stop_chat_for does for an active handle. (stop_chat_for releases
  # _starting at the end, so the chat is idle and a fresh send can claim it.)
  chat_mod.mark_starting("t10")
  gen = chat_mod.current_run_generation("t10")
  chat_mod._clear_after_terminal_generation["t10"] = gen
  chat_mod.bump_run_generation("t10")  # Stop's immediate successor: gen + 1
  chat_mod.discard_starting("t10")  # Stop released _starting; chat now idle

  # A FRESH send claims the now-idle chat: mark_starting succeeds (NO gen bump,
  # by design) and a StartTurn re-sets the durable marker under the new run's
  # token. The new run is in flight (mark_starting kept _starting set), so the
  # chat is alive again at gen + 1 — exactly the state where the dying run's
  # gen-only check is ambiguous.
  assert chat_mod.mark_starting("t10") is True
  assert chat_mod.current_run_generation("t10") == gen + 1, (
    "mark_starting must NOT bump the generation (the race precondition)"
  )
  from app.chat_writer import StartTurn, await_ack, get_writer

  async def _set_fresh_marker():
    ack = get_writer().submit(StartTurn(
      chat_id="t10", run_token="rt-10-fresh",
      user_msg={"role": "user", "content": "fresh", "ts": 9},
    ))
    await await_ack(ack)

  asyncio.run(_set_fresh_marker())
  assert _load("t10")["run_status"] == "running", "fresh StartTurn set marker"

  # Now the dying Stop-bumped run reaches its finally. Its gen-only check
  # (current == run_gen + 1) still passes, but the lock-gated is_chat_running
  # re-check must observe the fresh owner and SKIP the clear.
  published = []
  _run_real_chat("t10", run_token="rt-10", run_gen=gen, published=published)
  _drain_actor()

  state = _load("t10")
  assert state["run_status"] == "running", (
    "the dying Stop-bumped run must NOT clear the racing fresh run's marker"
  )
  # The fresh owner is still alive (its run never ran here — we only set its
  # marker), so the chat remains claimed.
  assert chat_mod.is_chat_running("t10")


# -- 11 (FIX D). no-owner setup cleanup: marker cleared, pending dropped ----
def test_no_owner_cleanup_clears_marker_before_registry_release(monkeypatch):
  """The no-owner setup-error early return routes through the bounded
  terminal cleanup: the pending queue is cleared durably, the marker is
  cleared, the registry is released, and no continuation is scheduled."""
  # Seed creds but NO owner so run_chat bails at the no-owner guard (which
  # is checked before the auth guard).
  creds = (
    pathlib.Path(os.environ["DATA_DIR"]) / "cli-auth" / "claude"
    / ".credentials.json"
  )
  creds.parent.mkdir(parents=True, exist_ok=True)
  creds.write_text("{}", encoding="utf-8")
  _seed_chat(
    "t11", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t11")
  gen = chat_mod.current_run_generation("t11")
  published = []
  try:
    _run_real_chat("t11", run_token="rt-11", run_gen=gen, published=published)
  finally:
    chat_mod._schedule_continuation = orig_sched
  _drain_actor()

  assert "error" in published
  assert "queued_turn_starting" not in published
  assert scheduled == []
  state = _load("t11")
  assert state["run_status"] is None, "no-owner cleanup must clear the marker"
  assert state["pending_messages"] == [], "pending must be cleared durably"
  assert not chat_mod.registry.is_alive("t11"), "registry released"


# -- 12 (FIX D). auth-error setup cleanup: marker cleared, pending dropped --
def test_auth_error_cleanup_clears_marker_before_registry_release(monkeypatch):
  """The auth-error setup early return routes through the same bounded
  terminal cleanup: pending cleared, marker cleared, registry released, no
  continuation."""
  # Seed an owner but NO creds file → Claude check_auth returns an error.
  # The DATA_DIR tmpdir is shared across tests and conftest does not sweep
  # the creds file, so a prior _seed_owner_and_creds may have written one —
  # remove it explicitly to guarantee the auth-error precondition.
  from app import auth as auth_mod

  creds = (
    pathlib.Path(os.environ["DATA_DIR"]) / "cli-auth" / "claude"
    / ".credentials.json"
  )
  if creds.exists():
    creds.unlink()
  dbx = SessionLocal()
  try:
    dbx.add(models.Owner(
      username="o", hashed_password=auth_mod.hash_password("x"),
      provider="claude",
    ))
    dbx.commit()
  finally:
    dbx.close()
  _seed_chat(
    "t12", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t12")
  gen = chat_mod.current_run_generation("t12")
  published = []
  try:
    _run_real_chat("t12", run_token="rt-12", run_gen=gen, published=published)
  finally:
    chat_mod._schedule_continuation = orig_sched
  _drain_actor()

  assert "error" in published
  assert "queued_turn_starting" not in published
  assert scheduled == []
  state = _load("t12")
  assert state["run_status"] is None, "auth-error cleanup must clear the marker"
  assert state["pending_messages"] == [], "pending must be cleared durably"
  assert not chat_mod.registry.is_alive("t12"), "registry released"


# -- 13 (FIX D). strict marker-clear ACK timeout → FAILED_LEAVE_MARKER ------
def test_empty_queue_marker_clear_ack_timeout_leaves_marker(monkeypatch):
  """The empty-queue terminal path issues a STRICT ClearRunStatus. Latch the
  actor inside `_clear_run_status` AFTER dispatch so its commit blocks past a
  deterministically-small ACK_TIMEOUT_SECS. The strict clear's await_ack
  times out → drain_and_release raises → _complete_turn maps it to
  FAILED_LEAVE_MARKER: marker LEFT set (NOT cleared), no continuation,
  transport error surfaced. A reconcile-after-restart then clears it."""
  _seed_owner_and_creds()
  _seed_chat(
    "t13", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], run_status="running",
  )
  # No streamed text → finalize() is a no-op, so the turn reaches the
  # empty-queue drain (which issues the strict ClearRunStatus we latch).
  _patch_claude_runner(monkeypatch, text=None)
  monkeypatch.setattr(chat_writer, "ACK_TIMEOUT_SECS", 0.2)

  writer = get_writer()
  release = threading.Event()
  orig_clear = writer._clear_run_status

  def latched_clear(db, cmd):
    release.wait(timeout=10)  # block the actor inside the clear commit
    return orig_clear(db, cmd)

  writer._clear_run_status = latched_clear

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("t13")
  gen = chat_mod.current_run_generation("t13")
  published = []
  try:
    _run_real_chat("t13", run_token="rt-13", run_gen=gen, published=published)

    # The caller's await_ack timed out and returned FAILED_LEAVE_MARKER. The
    # latched clear has NOT committed yet (release is still unset), so the
    # marker is observably LEFT SET — the caller did not wipe it on the lost
    # ack, which is the whole point of the strict variant.
    assert scheduled == [], (
      "a timed-out marker clear must NOT schedule a continuation"
    )
    assert "queued_turn_starting" not in published
    assert "error" in published
    assert _load("t13")["run_status"] == "running", (
      "a timed-out strict marker clear must LEAVE the marker set"
    )
  finally:
    # Release the latch — the clear now lands behind the caller's back (the
    # accept-and-document late-landing outcome; it converges the marker to
    # cleared, the same state a restart reconcile would reach).
    release.set()
    _drain_actor()
    writer._clear_run_status = orig_clear
    chat_mod._schedule_continuation = orig_sched

  # Late-landing clear converged the marker (the documented accept-and-document
  # outcome). Had it NOT landed (commit dropped rather than delayed), the
  # marker would stay set and a restart reconcile would clear it — both
  # outcomes are covered by reconcile's mid-commit-timeout contract.
  assert _load("t13")["run_status"] is None


# -- 14 (final-review Bug B). malformed queue head LEAVES the marker --------
def test_malformed_pending_head_leaves_marker_then_reconcile_repairs(monkeypatch):
  """A MALFORMED pending head (content that can't build a schemas.ChatMessage)
  makes `_promote_pending` RAISE rather than return promoted=None. The drain
  maps that to FAILED_LEAVE_MARKER: the run marker is LEFT set and the
  malformed message stays queued — NOT EMPTY_TERMINAL_CLEARED, which would
  clear the marker + forget the chat while work still remains (the bug). A
  reconcile-after-restart then repairs it.

  Regression: before the fix, the except branch returned promoted=None,
  indistinguishable from an empty queue, so drain_and_release cleared the
  marker and forgot the chat with the malformed message still in the queue.
  """
  _seed_owner_and_creds()
  _seed_chat(
    "tb",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    # A truthy non-string content survives the `... or ""` guard and makes
    # schemas.ChatMessage(content=[...]) raise inside _promote_pending.
    pending=[{"role": "user", "content": ["malformed"], "ts": 3}],
    run_status="running",
  )
  # No streamed text → finalize() is a no-op, so the turn reaches the drain
  # (the promote) which is what raises on the malformed head.
  _patch_claude_runner(monkeypatch, text=None)

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  chat_mod.mark_starting("tb")
  gen = chat_mod.current_run_generation("tb")
  published = []
  try:
    _run_real_chat("tb", run_token="rt-b", run_gen=gen, published=published)
  finally:
    chat_mod._schedule_continuation = orig_sched
  _drain_actor()

  assert scheduled == [], "a malformed promote must NOT schedule a continuation"
  assert "queued_turn_starting" not in published
  assert "error" in published
  state = _load("tb")
  assert state["run_status"] == "running", (
    "a malformed queue head must LEAVE the marker set, not clear it"
  )
  assert len(state["pending_messages"]) == 1, "the malformed message stays queued"

  # Reconcile-after-restart repairs it: clears the marker, drops the stranded
  # queue, appends an interrupted-turn note.
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "tb" in reconciled
  state = _load("tb")
  assert state["run_status"] is None
  assert state["pending_messages"] == []


# -- 15 (final-review Area 6). reconcile scrubs an orphan question card -----
def test_reconcile_scrubs_unanswered_question_card():
  """A crash after a QuestionCommit (the unanswered question block is durable)
  but before the next turn leaves an orphan card: the in-memory pending future
  died with the process, so the card 410s on submit yet renders interactive
  (questionAnswerable = hasQuestion && isLastMsg && !sending — none of which
  reconcile changes). Reconcile must DROP the unanswered question block while
  KEEPING an already-answered one + other blocks, then append the
  interruption note.

  Regression: before the fix, reconcile left the unanswered question block in
  place, so the reloaded UI showed an interactive card that dead-ended on
  submit.
  """
  _seed_chat(
    "tq",
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant", "ts": 2, "blocks": [
        {"type": "text", "content": "thinking"},
        {"type": "question", "question_id": "q-answered",
         "questions": [{"id": "q-answered", "question": "Old?"}],
         "answers": {"Old?": "yes"}},
        {"type": "question", "question_id": "q-open",
         "questions": [{"id": "q-open", "question": "Color?"}]},
      ]},
    ],
    run_status="running",
  )
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "tq" in reconciled
  state = _load("tq")
  assert state["run_status"] is None
  blocks = state["messages"][-1]["blocks"]
  qids = [b.get("question_id") for b in blocks if b.get("type") == "question"]
  assert "q-open" not in qids, "the unanswered orphan card must be scrubbed"
  assert "q-answered" in qids, "an already-answered question is real transcript"
  assert any(
    b.get("type") == "text" and b.get("content") == "thinking" for b in blocks
  ), "non-question blocks must survive the scrub"
  assert any(
    b.get("type") == "error" and "interrupted" in b["message"].lower()
    for b in blocks
  ), "the interruption note must be appended"


# -- 16 (final-review Bug A). Stop during the StartTurn commit: no spawn ----
#
# These two drive send_message DIRECTLY (no TestClient) for determinism: the
# fix's observable is that the broadcast + the run_chat spawn are created
# together in the same branch, so `get_broadcast(cid)` is the bulletproof
# signal (independent of whether a run_chat monkeypatch intercepts). Creds are
# seeded + the SDK runner patched so a stray real run_chat is harmless rather
# than erroring on a missing CLI.
def _direct_send(cid):
  """Call the real send_message coroutine for `cid` with content 'hello' on a
  fresh session; returns the JSONResponse."""
  from app.routes import chats_stream
  body = chat_mod.schemas.SendMessage(content="hello")
  db = SessionLocal()
  try:
    return asyncio.run(chats_stream.send_message(body, cid, None, db))
  finally:
    db.close()


def test_stop_during_starting_does_not_spawn_superseded_turn(monkeypatch):
  """A Stop that lands DURING the initial StartTurn commit (it bumps the
  generation, clears the marker, and releases _starting while no SDK handle is
  registered yet) must NOT let send_message spawn the now-superseded turn.
  send_message captures the generation BEFORE the StartTurn await and
  revalidates after the ack; a bump means 'a Stop raced' → do not spawn (no
  broadcast, no run_chat).

  Regression: before the fix, the generation was read AFTER the ack, adopting
  Stop's bumped value, so the route spawned the already-stopped turn (with no
  durable marker).
  """
  from app.routes import chats_stream
  from app.broadcast import get_broadcast

  _seed_owner_and_creds()
  _patch_claude_runner(monkeypatch, text=None)  # harmless if run_chat runs
  cid = "bugA"
  _seed_chat(cid, messages=[], pending=[])

  spawned = []

  async def _noop(*a, **k):
    return None

  def fake_run_chat(*a, **k):
    spawned.append(k)
    return _noop()

  monkeypatch.setattr(chats_stream, "run_chat", fake_run_chat)

  # Bump the generation right after the StartTurn ack resolves (exactly what a
  # racing stop_chat_for does), inside send_message's capture→ack→revalidate
  # window. The bump is synchronous within the coroutine, so it is observed at
  # the revalidation.
  real_await_ack = chats_stream.await_ack

  async def bumping_await_ack(ack):
    result = await real_await_ack(ack)
    chat_mod.bump_run_generation(cid)
    return result

  monkeypatch.setattr(chats_stream, "await_ack", bumping_await_ack)

  resp = _direct_send(cid)
  _drain_actor()

  assert resp.status_code == 202
  assert spawned == [], (
    "a Stop that raced the StartTurn commit must not spawn the superseded turn"
  )
  assert get_broadcast(cid) is None, (
    "no broadcast may be created when a Stop raced the start (broadcast + "
    "spawn are gated together in the same branch)"
  )
  # The user message is durable (StartTurn committed it before the bump).
  state = _load(cid)
  assert state is not None and len(state["messages"]) == 1


def test_normal_send_spawns_turn(monkeypatch):
  """Control for the Bug A guard: with NO racing Stop (generation unchanged
  across the StartTurn await), send_message creates the broadcast and spawns
  the turn. Proves the guard gates only the raced case, not every send."""
  from app.routes import chats_stream
  from app.broadcast import get_broadcast

  _seed_owner_and_creds()
  _patch_claude_runner(monkeypatch, text=None)
  cid = "okA"
  _seed_chat(cid, messages=[], pending=[])

  spawned = []

  async def _noop(*a, **k):
    return None

  def fake_run_chat(*a, **k):
    spawned.append(k)
    return _noop()

  monkeypatch.setattr(chats_stream, "run_chat", fake_run_chat)

  resp = _direct_send(cid)
  _drain_actor()

  assert resp.status_code == 202
  assert len(spawned) == 1, "a normal send must spawn exactly one turn"
  assert get_broadcast(cid) is not None, "a normal send creates the broadcast"
