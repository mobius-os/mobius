"""Terminal-completion state-machine tests (design 2026-06-01, §F).

Eight tests, all driving the REAL `run_chat` wrapper (never `_complete_turn`
directly) so they cover `run_chat`'s `finally` + the locked terminal
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
  AFTER the question (a concurrent same-loop append) both survive, and no
  orphan card is persisted by a later Finalize."""
  _seed_chat("t5a", messages=[{"role": "user", "content": "hi", "ts": 1}])
  bc = ChatBroadcast("t5a")
  sink = chat_mod._ChatEventSink(bc, "t5a", run_token="rt-5a")
  sink.publish({"type": "text", "content": "thinking"})

  fail = {"on": True}
  real_apply = chat_writer._apply_last_assistant_message

  def maybe_boom(db_, chat_id, snapshot):
    if fail["on"]:
      from app.chat_writer import _PersistFailed
      raise _PersistFailed("forced question-commit failure")
    return real_apply(db_, chat_id, snapshot)

  monkeypatch.setattr(chat_writer, "_apply_last_assistant_message", maybe_boom)

  published = []
  orig = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig(e))[1]

  async def go():
    with pytest.raises(Exception):
      await sink.publish_question(
        {"type": "question", "question_id": "q1",
         "questions": [{"id": "q1", "question": "Color?"}]}
      )
    # A later same-loop append (a text block) lands AFTER the failed question
    # block would have been appended — it must survive the scrub. We append
    # it here to model the concurrent-append hazard the tail-slice deleted.
    sink.publish({"type": "text", "content": " more"})

  asyncio.run(go())

  # The orphan question block is gone; neither neighbour was collaterally
  # deleted.
  assert all(b.get("type") != "question" for b in sink.assistant_blocks), (
    sink.assistant_blocks
  )
  texts = [b for b in sink.assistant_blocks if b.get("type") == "text"]
  assert texts, "prior text block must survive the scrub"
  assert "question" not in published, "the card must NOT be broadcast"

  # A subsequent Finalize persists NO question card (the scrub held).
  fail["on"] = False
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
  # Simulate Stop: bump the generation and register the stopped-generation
  # handoff exactly as stop_chat_for does for an active handle.
  chat_mod._clear_after_terminal_generation["t6"] = gen
  successor_gen = chat_mod.bump_run_generation("t6")
  assert successor_gen == gen + 1

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
