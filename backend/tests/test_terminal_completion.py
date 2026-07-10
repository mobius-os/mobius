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
from app.deps import Principal


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
  clears the marker, PRESERVES the queued message (it self-heals on the next
  send, bug #2), and appends an interrupted-turn note."""
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
  assert [m["content"] for m in state["pending_messages"]] == ["queued"], (
    "reconcile PRESERVES the queue (bug #2); it drains on the next send"
  )
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
  # _starting is load-bearing for the Stop-handoff clear gate — a leftover claim would
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
  assert [m["content"] for m in state["pending_messages"]] == ["queued"], (
    "reconcile PRESERVES the queue (bug #2); it drains on the next send"
  )


# -- 9. post-promote continuation scheduling failure LEAVES marker ---------
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


# -- 10. Stop-handoff clear must NOT erase a racing fresh marker ------------
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


# -- 10b. stale finalize must NOT append after a fresh turn's user msg -------
def test_stale_dying_run_does_not_finalize_after_fresh_turn_claimed(monkeypatch):
  """The bug the corrected fix closes: a Stop-superseded dying run reaches
  `_complete_turn` WITH accumulated assistant_blocks while a FRESH turn has
  already claimed the chat (registry alive via `mark_starting`, a new user
  message is the last persisted row). Without the stale-finalize guard, the
  dying run's `finalize()` appends its stale assistant content AFTER the fresh
  user message (`_apply_last_assistant_message`'s else-branch append), so the
  transcript reads user-fresh / assistant-stale and the fresh turn's response
  later lands out of order. The guard makes `_complete_turn` SKIP finalize and
  return STALE_NO_ACTION, leaving the fresh user message as the last row.

  This is the case-6 (legit Stop handoff finalizes) / case-10 (Stop-handoff
  clear must not erase a racing marker) discriminator turned on the FINALIZE
  decision: the dying run shares the Stopped generation `run_gen + 1` with a
  clean Stop handoff, so generation alone can't tell them apart — the
  `registry.is_alive` re-check (the fresh `mark_starting`) is what does."""
  _seed_owner_and_creds()
  _seed_chat("t10c", messages=[{"role": "user", "content": "hi", "ts": 1}],
             pending=[], run_status="running")

  chat_mod.mark_starting("t10c")
  gen = chat_mod.current_run_generation("t10c")

  from app.chat_writer import StartTurn, await_ack, get_writer

  # The dying run's gen is CURRENT at `_run_chat_impl`'s entry guard (so it gets
  # past it and into the runner). Stop + the fresh reclaim happen DURING the
  # stream — modelled by doing them inside the fake runner, after it has
  # accumulated assistant content into the sink but before `_complete_turn`.
  # This is the real ordering: a turn already streaming when Stop+resend lands.
  async def fake_runner(*, bc, **kwargs):
    bc.publish({"type": "text", "content": "stale dying-run answer"})
    # Stop the dying run exactly as stop_chat_for does for an active handle:
    # register the stopped-generation handoff, bump to the immediate successor,
    # and release _starting so a fresh send can claim the now-idle chat.
    chat_mod._clear_after_terminal_generation["t10c"] = gen
    chat_mod.bump_run_generation("t10c")  # Stop's immediate successor: gen + 1
    chat_mod.discard_starting("t10c")
    # A FRESH send claims the chat: a registry _starting claim (NO gen bump, by
    # design) makes the registry alive again at gen + 1, and a StartTurn re-sets
    # the marker AND appends the fresh user message — so the last persisted row
    # is now a USER message, the exact precondition for the else-branch stale
    # append. We use the registry primitive directly (not the chat_mod
    # mark_starting wrapper) because the dying run's broadcast is still running
    # mid-stream, so the wrapper's is_chat_running gate would refuse — but the
    # discriminator deliberately keys on registry.is_alive, not is_chat_running,
    # exactly so this fresh _starting claim is observed regardless of the dying
    # broadcast's lifecycle.
    assert chat_mod.registry.mark_starting("t10c") is True
    assert chat_mod.current_run_generation("t10c") == gen + 1
    assert chat_mod.registry.is_alive("t10c")
    ack = get_writer().submit(StartTurn(
      chat_id="t10c", run_token="rt-10c-fresh",
      user_msg={"role": "user", "content": "fresh question", "ts": 9},
    ))
    await await_ack(ack)
    return {"session_id": "sess", "cost_usd": 0.0}

  import app.claude_sdk_runner as csr
  monkeypatch.setattr(csr, "run_claude_sdk_turn", fake_runner)

  # Capture the disposition the dying run's _complete_turn returns.
  dispositions = []
  real_complete_turn = chat_mod._complete_turn

  async def _capturing_complete_turn(**kwargs):
    result = await real_complete_turn(**kwargs)
    dispositions.append(result)
    return result

  monkeypatch.setattr(chat_mod, "_complete_turn", _capturing_complete_turn)

  # The dying Stop-bumped run reaches its terminal transition. It owns `gen`,
  # the current generation is gen + 1 (so we_own_gen is False), and the fresh
  # mark_starting makes the registry alive (so stop_handoff_successor is also
  # False) — the guard must SKIP finalize.
  published = []
  _run_real_chat("t10c", run_token="rt-10c", run_gen=gen, published=published)
  _drain_actor()

  assert dispositions == [chat_queue.TerminalDisposition.STALE_NO_ACTION], (
    "a dying run whose chat was reclaimed by a fresh turn must return "
    "STALE_NO_ACTION (skip finalize), not finalize stale content"
  )
  state = _load("t10c")
  # The bug: finalize would APPEND the dying run's stale assistant content
  # after the fresh user message (the else-branch append, because msgs[-1] is
  # now a user row). The fix skips finalize, so the fresh user message stays
  # the last row.
  assert state["messages"][-1] == {
    "role": "user", "content": "fresh question", "ts": 9
  }, (
    "the dying run must NOT append its stale assistant content after the "
    "fresh turn's user message"
  )
  # No assistant row may follow the fresh user message (the precise orphan the
  # finalize append would create). A pre-race streaming partial — written while
  # msgs[-1] was still the ORIGINAL user row — is a separate concern and lands
  # before the fresh user message, so we check ordering, not mere presence.
  fresh_idx = next(
    i for i, m in enumerate(state["messages"])
    if m.get("role") == "user" and m.get("content") == "fresh question"
  )
  assert not any(
    m.get("role") == "assistant" for m in state["messages"][fresh_idx + 1:]
  ), "no assistant row may follow the fresh turn's user message"
  # The fresh turn's marker survives untouched.
  assert state["run_status"] == "running", (
    "the fresh turn's run marker must survive the dying run's bow-out"
  )
  assert "done" in published


# -- 11. no-owner setup cleanup: marker cleared, pending dropped ------------
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


# -- 12. auth-error setup cleanup: marker cleared, pending dropped ----------
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


# -- 12b. setup-error cleanup ownership gate (the forget-wedge guard) -------
def test_setup_error_cleanup_stale_gen_does_not_clobber_successor():
  """The setup-error forget-wedge guard.

  A setup-erroring run can be superseded between `_run_chat_impl` entry and
  the cleanup by a Stop (bumps the generation) plus a fresh POST (claims the
  chat at the new generation). The cleanup keyed on the OLD run_gen must then
  touch NOTHING: clearing pending would wipe the successor's queued send and
  the unconditional forget would reset the successor's generation, stranding
  its fresh marker `running` until restart reconciliation. The gate returns
  STALE_NO_ACTION instead."""
  # Seed the SUCCESSOR's durable state: a fresh turn that set the marker, with
  # a send queued behind it.
  _seed_chat(
    "se-stale",
    messages=[{"role": "user", "content": "fresh", "ts": 1}],
    pending=[{"role": "user", "content": "queued-behind-fresh", "ts": 2}],
    run_status="running",
  )
  chat_mod.registry.bump_generation("se-stale")  # → 1, the setup-erroring run
  chat_mod.registry.bump_generation("se-stale")  # → 2, a Stop bumped it
  chat_mod.registry.mark_starting("se-stale")    # successor holds the slot

  disposition = asyncio.run(
    chat_mod._terminal_setup_error_cleanup("se-stale", "rt-stale", 1)
  )
  _drain_actor()

  assert disposition is chat_queue.TerminalDisposition.STALE_NO_ACTION
  state = _load("se-stale")
  assert state["run_status"] == "running", "successor's marker must survive"
  assert state["pending_messages"] == [
    {"role": "user", "content": "queued-behind-fresh", "ts": 2}
  ], "successor's queued send must survive"
  assert chat_mod.registry.current_generation("se-stale") == 2, (
    "successor's generation must survive (not reset to reusable 0)"
  )
  assert chat_mod.registry.is_alive("se-stale"), "successor's slot kept"


def test_setup_error_cleanup_owned_gen_clears_and_forgets():
  """When this run still owns the generation, the cleanup clears the pending
  queue + the marker and forgets the chat (EMPTY_TERMINAL_CLEARED) — the gate
  is transparent on the common, uncontended path."""
  _seed_chat(
    "se-own",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 2}],
    run_status="running",
  )
  gen = chat_mod.registry.bump_generation("se-own")  # → 1, this run owns it
  chat_mod.registry.mark_starting("se-own")

  disposition = asyncio.run(
    chat_mod._terminal_setup_error_cleanup("se-own", "rt-own", gen)
  )
  _drain_actor()

  assert disposition is chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED
  state = _load("se-own")
  assert state["run_status"] is None, "owned cleanup clears the marker"
  assert state["pending_messages"] == [], "owned cleanup clears pending"
  assert chat_mod.registry.current_generation("se-own") == 0, "forgotten"
  assert not chat_mod.registry.is_alive("se-own"), "starting slot released"


# -- 13. strict marker-clear ACK timeout → FAILED_LEAVE_MARKER --------------
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


# -- 14. malformed queue head LEAVES the marker -----------------------------------------------
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

  # Reconcile-after-restart repairs it: clears the marker, PRESERVES the
  # queue (bug #2 — even a malformed entry is kept reversibly; the user can
  # cancel it from the tray, and the next-send drain's FAILED_LEAVE_MARKER
  # path already handles a malformed head), appends an interrupted-turn note.
  chat_mod.registry.reset_for_tests()
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "tb" in reconciled
  state = _load("tb")
  assert state["run_status"] is None
  assert len(state["pending_messages"]) == 1, (
    "reconcile PRESERVES the queue (bug #2), even a malformed head"
  )


# -- 15 (question wait contract). reconcile preserves an open question -------
def test_reconcile_preserves_tail_unanswered_question_card():
  """A crash after a QuestionCommit (the unanswered question block is durable)
  but before the answer no longer expires the prompt. The in-memory future died
  with the process, but the durable question remains the human handoff:
  reconcile keeps it as the tail block and inserts the interruption note before
  it so the later answer can restart a hidden continuation.
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
  assert "q-open" in qids, "the open question must remain answerable"
  assert "q-answered" in qids, "an already-answered question is real transcript"
  assert any(
    b.get("type") == "text" and b.get("content") == "thinking" for b in blocks
  ), "non-question blocks must survive recovery"
  assert blocks[-1].get("question_id") == "q-open", (
    "the open question must stay at the tail"
  )
  err_idx = next(
    i for i, b in enumerate(blocks)
    if b.get("type") == "error" and "interrupted" in b["message"].lower()
  )
  open_idx = next(
    i for i, b in enumerate(blocks)
    if b.get("question_id") == "q-open"
  )
  assert err_idx < open_idx, "the interruption note belongs before the prompt"


# -- 16. Stop during the StartTurn commit: no spawn -----------------------------------------------
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
    principal = Principal(owner=db.query(models.Owner).one(), app_id=None)
    return asyncio.run(chats_stream.send_message(body, cid, principal, db))
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
  """Control for the Stop-during-StartTurn guard: with NO racing Stop (generation unchanged
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


# -- 17. Stop during finalize → drain re-decides ownership UNDER its lock ----
def test_stop_during_finalize_makes_drain_bow_out_under_lock(monkeypatch):
  """The run OWNS its generation at the pre-finalize gate, but a Stop / fresh
  StartTurn lands DURING `await sink.finalize()` and bumps the generation. The
  drain no longer trusts a bool snapshotted before its lock — it re-decides
  ownership UNDER the lock from `run_gen` via the injected current-generation
  reader, so the in-finalize bump is observed and the superseded turn bows out
  (STALE_NO_ACTION): it promotes nothing, clears nothing.

  Regression: if the drain acted on the stale pre-finalize ownership (still
  True), it would promote the queued head (queued_turn_starting + a scheduled
  continuation) for a turn the in-finalize bump just superseded — double-firing
  the queue. It would also clear a marker the newer owner still needs.

  Seam: `finalize_response_outcome` is the actor-side handler the `Finalize`
  command dispatches to, so monkeypatching it to bump the generation lands the
  bump synchronously inside the `await sink.finalize()` ack window — exactly
  the "Stop/StartTurn races the finalize" interleaving. The observable is the
  OUTCOME (no promotion, no continuation, queue + marker left intact for the
  newer owner), not the internal ownership arg the drain no longer takes.
  """
  _seed_owner_and_creds()
  _seed_chat(
    "t17",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    # A pending head so a drain that WRONGLY acted as owner would promote it
    # (queued_turn_starting + a continuation) — making the bug observable.
    pending=[{"role": "user", "content": "queued", "ts": 3}],
    run_status="running",
  )
  # Stream text so finalize() has blocks to commit and the Finalize command is
  # actually submitted + awaited (the bump must land inside that await).
  _patch_claude_runner(monkeypatch, text="partial answer")

  chat_mod.mark_starting("t17")
  gen = chat_mod.current_run_generation("t17")

  # Bump the generation from INSIDE the finalize ack (the actor thread runs
  # finalize_response_outcome while the caller awaits sink.finalize()), then
  # delegate to the real handler so the terminal write still persists.
  real_finalize = chat_writer.finalize_response_outcome

  def bumping_finalize(db_, chat_id, blocks):
    chat_mod.bump_run_generation(chat_id)
    return real_finalize(db_, chat_id, blocks)

  monkeypatch.setattr(chat_writer, "finalize_response_outcome", bumping_finalize)

  scheduled = []
  orig_sched = chat_mod._schedule_continuation
  chat_mod._schedule_continuation = lambda **kw: scheduled.append(kw)

  published = []
  try:
    _run_real_chat("t17", run_token="rt-17", run_gen=gen, published=published)
  finally:
    chat_mod._schedule_continuation = orig_sched
  _drain_actor()

  # The drain re-decided ownership under its lock and saw the in-finalize bump:
  # it took STALE_NO_ACTION — no promotion, no continuation, queue left for the
  # newer owner.
  assert scheduled == [], (
    "a turn superseded mid-finalize must NOT schedule a continuation"
  )
  assert "queued_turn_starting" not in published, (
    "a turn superseded mid-finalize must NOT promote the queue"
  )
  assert _load("t17")["pending_messages"] == [
    {"role": "user", "content": "queued", "ts": 3}
  ], "the superseded turn must leave the queued message untouched"
  # The superseded run must NOT clear the marker — the newer owner still needs
  # it. The drain's STALE_NO_ACTION bow-out touches nothing durable.
  assert _load("t17")["run_status"] == "running", (
    "the superseded turn must NOT clear the marker the newer owner still holds"
  )


# -- 18. stale-reclaim bow-out: identity-keyed compare-and-clear -------------
def _arrange_stale_reclaim(chat_id):
  """Put `chat_id` in the pure stale-reclaim state and return its `gen`.

  `mark_starting` leaves the registry alive (so stop_handoff_successor's
  `not registry.is_alive` is False); bumping the generation without registering
  a Stop handoff makes we_own_gen False at the gate. With NO
  _clear_after_terminal_generation entry, stop_handoff_successor is False too —
  a fresh turn owns the chat (the pure stale-reclaim case)."""
  chat_mod.mark_starting(chat_id)
  gen = chat_mod.current_run_generation(chat_id)
  chat_mod.bump_run_generation(chat_id)  # a fresh StartTurn now owns the chat
  assert chat_mod.registry.is_alive(chat_id)
  return gen


def test_stale_reclaim_bow_out_preserves_fresh_owners_broadcast_and_browser(
  monkeypatch
):
  """FRESH OWNER PRESENT: a fresh turn already replaced the active-broadcast
  pointer with its OWN broadcast before this superseded run reaches its
  bow-out. The bow-out's identity-keyed `clear_active_broadcast_if(bc)` finds
  the pointer no longer points at this dying run's `bc`, so it PRESERVES the
  fresh owner's pointer (no clobber) and does NOT close the shared per-chat
  browser the live turn still holds.

  Regression: a blind `set_active_broadcast(None)` would erase the fresh
  turn's pointer, and an unconditional `_close_browser_session` would yank the
  shared browser out from under the live turn.

  Drives `_complete_turn` directly so the disposition return value is the
  observable, with the generation already bumped (we_own_gen False) and the
  registry still alive (stop_handoff_successor False) — the stale-reclaim path.
  """
  from app import broadcast as bc_mod

  _seed_owner_and_creds()
  _seed_chat(
    "t18a", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], run_status="running",
  )
  gen = _arrange_stale_reclaim("t18a")

  browser_closed = []

  async def spy_close(chat_id):
    browser_closed.append(chat_id)

  monkeypatch.setattr(chat_mod, "_close_browser_session", spy_close)

  bc = create_broadcast("t18a")
  published = []
  orig = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig(e))[1]
  sink = chat_mod._ChatEventSink(bc, "t18a", run_token="rt-18a")

  # The FRESH owner already holds the active-broadcast pointer with its OWN
  # broadcast (a different object than this dying run's `bc`).
  fresh_bc = ChatBroadcast("t18a")
  bc_mod.set_active_broadcast(fresh_bc)

  db = SessionLocal()
  try:
    disposition = asyncio.run(chat_mod._complete_turn(
      bc=bc, sink=sink, db=db, chat_id="t18a", run_gen=gen,
      provider_id="claude", cost_usd=0.0, close_browser=True,
    ))
    _drain_actor()

    assert disposition is chat_queue.TerminalDisposition.STALE_NO_ACTION
    assert "done" in published, "the bow-out still publishes its own done"
    assert bc_mod.get_active_broadcast() is fresh_bc, (
      "the bow-out must PRESERVE the fresh owner's broadcast pointer — its "
      "identity-keyed clear must not touch a pointer that isn't ours"
    )
    assert browser_closed == [], (
      "with a fresh owner present, the bow-out must NOT close the shared "
      "per-chat browser the live turn still holds"
    )
  finally:
    bc_mod.set_active_broadcast(None)


def test_stale_reclaim_bow_out_clears_pointer_but_leaves_browser_when_no_successor(
  monkeypatch
):
  """NO SUCCESSOR: the generation was bumped (e.g. by a Stop) AFTER the SDK
  runner already unregistered, so no fresh turn took over the active-broadcast
  pointer — it still points at THIS dying run's `bc`. The bow-out's
  identity-keyed `clear_active_broadcast_if(bc)` matches and releases the
  pointer (the durable, high-harm leak), rather than leaking it.

  It deliberately does NOT close the shared per-chat browser: the bow-out can't
  tell this no-successor case apart from a successor that is mid-handoff (has
  claimed the generation but not yet installed its pointer), and yanking a live
  browser is worse than a lingering Chrome in the rare no-successor case (the
  next turn / reconciliation reclaims it). Pointer freed, browser left.
  """
  from app import broadcast as bc_mod

  _seed_owner_and_creds()
  _seed_chat(
    "t18b", messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], run_status="running",
  )
  gen = _arrange_stale_reclaim("t18b")

  browser_closed = []

  async def spy_close(chat_id):
    browser_closed.append(chat_id)

  monkeypatch.setattr(chat_mod, "_close_browser_session", spy_close)

  bc = create_broadcast("t18b")
  published = []
  orig = bc.publish
  bc.publish = lambda e: (published.append(e.get("type")), orig(e))[1]
  sink = chat_mod._ChatEventSink(bc, "t18b", run_token="rt-18b")

  # NO fresh owner took over: the active-broadcast pointer is still THIS run's.
  bc_mod.set_active_broadcast(bc)

  db = SessionLocal()
  try:
    disposition = asyncio.run(chat_mod._complete_turn(
      bc=bc, sink=sink, db=db, chat_id="t18b", run_gen=gen,
      provider_id="claude", cost_usd=0.0, close_browser=True,
    ))
    _drain_actor()

    assert disposition is chat_queue.TerminalDisposition.STALE_NO_ACTION
    assert "done" in published, "the bow-out still publishes its own done"
    assert bc_mod.get_active_broadcast() is None, (
      "with no successor, the bow-out's identity-keyed clear must release the "
      "pointer that is still ours rather than leaking it"
    )
    assert browser_closed == [], (
      "the bow-out must NOT close the shared browser even with no successor — it "
      "can't distinguish that from a mid-handoff successor, and a lingering Chrome "
      "is cheaper than yanking a live one"
    )
  finally:
    bc_mod.set_active_broadcast(None)


# -- 19. ClearRunStatus is identity-keyed: a stale token can't wipe a marker --
def test_clear_run_status_is_identity_keyed_to_the_owning_run_token():
  """The markerless-run race. A fresh turn's StartTurn sets the marker and
  records itself (run_token) as the marker's owner. A dying run that races in
  during the window where the fresh turn has marked-starting but its handle
  isn't registered yet (so is_alive is briefly false) would otherwise clear
  the marker by chat_id, leaving the fresh turn markerless. ClearRunStatus is
  identity-keyed: a clear naming a DIFFERENT run_token is a no-op, so the fresh
  marker survives; the owning token still clears it.
  """
  from app.chat_writer import ClearRunStatus, StartTurn, await_ack, get_writer

  _seed_chat("t19", messages=[], pending=[])

  async def _fresh_then_stale_clear():
    a = get_writer().submit(StartTurn(
      chat_id="t19", run_token="rt-fresh",
      user_msg={"role": "user", "content": "hi", "ts": 1},
    ))
    await await_ack(a)
    # The dying run's clear names its OWN (older) token, not the fresh owner.
    a = get_writer().submit(
      ClearRunStatus(chat_id="t19", run_token="rt-dying")
    )
    await await_ack(a)

  asyncio.run(_fresh_then_stale_clear())
  assert _load("t19")["run_status"] == "running", (
    "a clear naming a non-owning run_token must NOT wipe the fresh marker"
  )

  async def _clear_matching():
    a = get_writer().submit(
      ClearRunStatus(chat_id="t19", run_token="rt-fresh")
    )
    await await_ack(a)

  asyncio.run(_clear_matching())
  assert _load("t19")["run_status"] is None, (
    "the run_token that owns the marker still clears it"
  )


# -- 20. a tokenless ClearRunStatus stays unconditional (reconcile/no-handoff)-
def test_tokenless_clear_run_status_is_unconditional():
  """A clear with run_token="" clears regardless of the recorded owner — the
  reconciliation and no-handoff paths that already know they own the marker
  must not be gated by the identity check.
  """
  from app.chat_writer import ClearRunStatus, StartTurn, await_ack, get_writer

  _seed_chat("t20", messages=[], pending=[])

  async def _start_then_tokenless_clear():
    a = get_writer().submit(StartTurn(
      chat_id="t20", run_token="rt-owner",
      user_msg={"role": "user", "content": "hi", "ts": 1},
    ))
    await await_ack(a)
    a = get_writer().submit(ClearRunStatus(chat_id="t20", run_token=""))
    await await_ack(a)

  asyncio.run(_start_then_tokenless_clear())
  assert _load("t20")["run_status"] is None, (
    "a tokenless clear clears unconditionally even with a recorded owner"
  )


# -- 21. finalize NOOP on a still-existing chat is benign (no spurious error) -
def test_finalize_noop_on_existing_chat_is_benign():
  """A concurrent ReplaceTranscript can wipe a chat's transcript mid-turn, so
  the terminal Finalize finds nothing to write and `_apply` returns NOOP — but
  the chat still EXISTS, so this is benign (nothing to save), not a persistence
  failure. The Finalize ack must resolve, not raise a spurious "could not be
  saved".
  """
  from app.chat_writer import Finalize, await_ack, get_writer

  _seed_chat("t21", messages=[], pending=[])  # exists, empty transcript

  async def _finalize():
    a = get_writer().submit(Finalize(
      chat_id="t21", run_token="rt-21",
      snapshot={"blocks": [{"type": "text", "content": "hi"}]},
    ))
    await await_ack(a)  # benign NOOP → APPLIED, so this must not raise

  asyncio.run(_finalize())


# -- 22. finalize NOOP on a missing/deleted chat stays a hard failure --------
def test_finalize_noop_on_missing_chat_raises():
  """A Finalize for a chat row that is gone (deleted mid-turn) must stay a hard
  NOOP: the dispatch raises so the turn maps to FAILED_LEAVE_MARKER and
  reconciliation handles the orphaned marker. A stray finalize must not
  silently succeed against a chat that delete already removed.
  """
  from app.chat_writer import (
    Finalize, _PersistFailed, await_ack, get_writer,
  )

  async def _finalize_missing():
    a = get_writer().submit(Finalize(
      chat_id="t22-missing", run_token="rt-22",
      snapshot={"blocks": [{"type": "text", "content": "hi"}]},
    ))
    await await_ack(a)

  with pytest.raises(_PersistFailed):
    asyncio.run(_finalize_missing())


# -- 23. finalize must NOT resurrect a soft-deleted chat ---------------------
def test_finalize_does_not_resurrect_soft_deleted_chat():
  """A Finalize enqueued just before a delete (and processed by the actor
  after deleted_at is set) must NOT append to the soft-deleted row — that would
  resurrect the deleted chat's transcript on recovery. The single core write
  helper filters deleted_at, so the finalize is a NOOP (→ hard, raises) and the
  row's transcript is left exactly as it was.
  """
  from datetime import UTC, datetime

  from app.chat_writer import (
    Finalize, _PersistFailed, await_ack, get_writer,
  )

  _seed_chat(
    "t23", messages=[{"role": "user", "content": "hi", "ts": 1}], pending=[],
  )
  # Soft-delete it (deleted_at set) while it still has a transcript — the
  # window where a pre-enqueued finalize would otherwise write to the dead row.
  db = SessionLocal()
  try:
    row = db.query(models.Chat).filter(models.Chat.id == "t23").first()
    row.deleted_at = datetime.now(UTC)
    db.commit()
  finally:
    db.close()

  async def _finalize_deleted():
    a = get_writer().submit(Finalize(
      chat_id="t23", run_token="rt-23",
      snapshot={"blocks": [{"type": "text", "content": "leaked"}]},
    ))
    await await_ack(a)

  with pytest.raises(_PersistFailed):
    asyncio.run(_finalize_deleted())

  state = _load("t23")  # _load reads by id, no deleted_at filter
  assert len(state["messages"]) == 1, "finalize must not append to a deleted chat"
  assert state["messages"][-1]["role"] == "user", "no assistant content resurrected"


# -- 24. NO chat-mutating command resurrects a soft-deleted chat -------------
def test_mutating_commands_do_not_resurrect_soft_deleted_chat():
  """A command queued before a delete and processed by the actor AFTER the
  soft-delete commits must NOT write to the dead row — it would resurrect the
  chat's transcript / set a run marker on a deleted chat (a wedged runner on a
  dead row). Every command that adds content or sets the marker now loads
  through `_active_chat` (filters deleted_at), so each raises (ack fails)
  instead. Covers the StartTurn / PromotePending / ReplaceTranscript class.
  """
  from datetime import UTC, datetime

  from app.chat_writer import (
    PromotePending, ReplaceTranscript, StartTurn, _PersistFailed,
    await_ack, get_writer,
  )

  _seed_chat(
    "t24",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "queued", "ts": 2}],
    run_status=None,
  )
  # Soft-delete while it still holds a transcript + a queued message.
  db = SessionLocal()
  try:
    row = db.query(models.Chat).filter(models.Chat.id == "t24").first()
    row.deleted_at = datetime.now(UTC)
    db.commit()
  finally:
    db.close()

  async def _submit(cmd):
    await await_ack(get_writer().submit(cmd))

  for cmd in (
    StartTurn(
      chat_id="t24", run_token="rt-24",
      user_msg={"role": "user", "content": "x", "ts": 3},
    ),
    PromotePending(chat_id="t24", run_token="rt-24"),
    ReplaceTranscript(
      chat_id="t24", messages=[{"role": "user", "content": "replaced", "ts": 9}],
    ),
  ):
    with pytest.raises(_PersistFailed):
      asyncio.run(_submit(cmd))

  # The soft-deleted row is untouched: original transcript + queue intact, no
  # marker set — nothing was resurrected.
  state = _load("t24")
  assert state["messages"] == [{"role": "user", "content": "hi", "ts": 1}], (
    "no command may mutate the transcript of a soft-deleted chat"
  )
  assert len(state["pending_messages"]) == 1, "the queue must not be promoted"
  assert state["run_status"] is None, "no run marker on a soft-deleted chat"
