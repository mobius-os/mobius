"""Contention + real-DB dispatch tests for the chat-writer actor (C1).

These drive the actor's command dispatch against a REAL `SessionLocal`
(the test DB wired by conftest), proving the dispatch logic in isolation
while the actor is still DORMANT on the production path — no route,
runner, or sink routes through it yet. The unit-level mechanics (FIFO,
coalescing, fencing, failure propagation) live in `test_chat_writer.py`
against a DB-free recording stub; this module instead asserts that the
real JSON-blob read-modify-writes the actor will own at C2 are correct
and serialize without lost updates.

Latches are deterministic (`threading.Event` + the actor's
`pause_for_test`/`resume_for_test`/`_on_snapshot_ready_for_test` hooks) —
no sleeps. The concurrency cases (`test_concurrent_*`) are run repeatedly
in CI via the `-k` selector; locally `pytest ... --count` style reruns
are done by hand (see the milestone report).
"""

import threading
from concurrent.futures import Future

import pytest

from app import models, schemas
from app.chat_writer import (
  AnswerQuestion,
  AppendPending,
  Barrier,
  CancelPending,
  ChatWriterActor,
  ClearPending,
  ClearRunStatus,
  Finalize,
  PersistError,
  PersistTranscript,
  PromotePending,
  QuestionCommit,
  ReplaceTranscript,
  StartTurn,
)
from app.database import SessionLocal


# -- fixtures + helpers ---------------------------------------------------
def _seed_chat(chat_id="c1", messages=None, pending=None, session_id="sess-1"):
  """Insert a Chat row and return its id, committed via a throwaway session."""
  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id,
      title="Test chat",
      messages=messages if messages is not None else [],
      pending_messages=pending if pending is not None else [],
      session_id=session_id,
      provider="claude",
    )
    db.add(chat)
    db.commit()
  finally:
    db.close()
  return chat_id


def _seed_app(app_id=42):
  db = SessionLocal()
  try:
    app = models.App(
      id=app_id,
      name=f"App {app_id}",
      description="",
      jsx_source="export default function App() { return null }",
      compiled_path="",
    )
    db.add(app)
    db.commit()
  finally:
    db.close()
  return app_id


def _load_chat(chat_id="c1"):
  """Read a fresh copy of the Chat row through a separate session.

  The actor owns its own session; assertions read through a distinct one
  to prove the commit is visible cross-session (the lost-update guarantee
  is meaningless if read through the actor's own identity map).
  """
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if chat is None:
      return None
    # Detach plain copies so the caller can inspect after the session closes.
    return {
      "messages": list(chat.messages or []),
      "pending_messages": list(chat.pending_messages or []),
      "session_id": chat.session_id,
      "provider": chat.provider,
      "title": chat.title,
      "run_status": chat.run_status,
      "run_started_at": chat.run_started_at,
    }
  finally:
    db.close()


def _load_run(run_token):
  db = SessionLocal()
  try:
    run = db.query(models.ChatRun).filter(models.ChatRun.id == run_token).first()
    if run is None:
      return None
    return {
      "id": run.id,
      "chat_id": run.chat_id,
      "status": run.status,
      "initiated_by_app_id": run.initiated_by_app_id,
    }
  finally:
    db.close()


def _assistant_msg(blocks, content=""):
  """Build an assistant-message snapshot the persist helpers accept."""
  return {"role": "assistant", "content": content, "blocks": list(blocks)}


def _question_msg(question_id, content=""):
  """An assistant message carrying a single AskUserQuestion block."""
  return _assistant_msg(
    [
      {
        "type": "question",
        "question_id": question_id,
        "questions": [{"id": question_id, "question": "Which color?"}],
      }
    ],
    content=content,
  )


@pytest.fixture
def actor():
  """A started actor backed by the real test SessionLocal.

  Stopped (drained + joined) on teardown so no writer thread leaks into
  the next test.
  """
  a = ChatWriterActor(session_factory=SessionLocal)
  a.start()
  try:
    yield a
  finally:
    a.stop(timeout=5)


def _await(fut, timeout=5):
  return fut.result(timeout=timeout)


# -- 1. snapshot-then-answer survives -------------------------------------
def test_snapshot_then_answer_survives(actor):
  """An AnswerQuestion after a transcript snapshot keeps both the streamed
  blocks AND the answer — the lost-update race Option C exists to close."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  # Stream the question card as a coalescible snapshot.
  _await(
    actor.submit(
      PersistTranscript(
        chat_id="c1", run_token="rt1", snapshot=_question_msg("q1")
      )
    )
  )
  # Drain the coalesced write before answering.
  _await(actor.submit(Barrier()))
  # Answer it (fences any pending snapshot, then merges + commits).
  _await(
    actor.submit(
      AnswerQuestion(
        chat_id="c1",
        run_token="rt1",
        question_id="q1",
        answers={"q1": "Red"},
      )
    )
  )
  chat = _load_chat()
  last = chat["messages"][-1]
  assert last["role"] == "assistant"
  block = last["blocks"][0]
  assert block["question_id"] == "q1"
  assert block["answers"] == {"q1": "Red"}


# -- 2. question commits before ack ---------------------------------------
def test_question_commit_commits_before_ack(actor):
  """QuestionCommit's ack resolves only after the block is durably persisted
  — so a runner that broadcasts the card on ack never shows an unpersisted
  question."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  fut = actor.submit(
    QuestionCommit(chat_id="c1", run_token="rt1", snapshot=_question_msg("q1"))
  )
  assert _await(fut) is True
  # The ack has resolved; the row MUST already carry the question block.
  chat = _load_chat()
  assert chat["messages"][-1]["blocks"][0]["question_id"] == "q1"


# -- 3. question not broadcast on QuestionCommit fail ----------------------
def test_question_commit_failure_raises_so_card_is_not_broadcast():
  """A QuestionCommit whose write does not land must RAISE its ack, so the
  caller declines to broadcast the card (no save-before-broadcast violation,
  no direct-write fallback)."""
  # No assistant message to update -> update_last_assistant_message returns
  # True (no-op) when there are no messages, so to force a *failure* we make
  # the commit drop. A session whose commit always raises OperationalError is
  # swallowed by _commit_or_rollback (returns False), which QuestionCommit
  # turns into a raised ack.
  from sqlalchemy.exc import OperationalError

  class _DropCommitSession:
    """Real-ish session: query returns a chat with an assistant message, but
    commit always drops (OperationalError), so the persist helper returns
    False."""

    def __init__(self):
      self._db = SessionLocal()

    def expire_all(self):
      self._db.expire_all()

    def execute(self, *a, **k):
      return self._db.execute(*a, **k)
    def query(self, *a, **k):
      return self._db.query(*a, **k)

    def commit(self):
      raise OperationalError("stmt", {}, Exception("database is locked"))

    def rollback(self):
      self._db.rollback()

    def close(self):
      self._db.close()

  _seed_chat(
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      _assistant_msg([{"type": "text", "content": "thinking"}]),
    ]
  )
  a = ChatWriterActor(session_factory=_DropCommitSession)
  a.start()
  try:
    fut = a.submit(
      QuestionCommit(
        chat_id="c1", run_token="rt1", snapshot=_question_msg("q1")
      )
    )
    with pytest.raises(Exception):
      _await(fut)
  finally:
    a.stop(timeout=5)


# -- 4. Finalize ack only after commit ------------------------------------
def test_finalize_ack_only_after_commit(actor):
  """Finalize resolves True only once the terminal message is persisted; the
  row reflects it the instant the ack lands."""
  _seed_chat(
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      _assistant_msg([{"type": "text", "content": "partial"}]),
    ]
  )
  fut = actor.submit(
    Finalize(
      chat_id="c1",
      run_token="rt1",
      snapshot=_assistant_msg(
        [
          {"type": "text", "content": "done"},
          {"type": "tool", "status": "running", "name": "x"},
        ]
      ),
    )
  )
  assert _await(fut) is True
  chat = _load_chat()
  blocks = chat["messages"][-1]["blocks"]
  # finalize_blocks force-completed the running tool block.
  tool = next(b for b in blocks if b.get("type") == "tool")
  assert tool["status"] != "running"


# -- BLOCKING 2: must-persist commands fail (not falsely ack) on a no-op ---
# update_last_assistant_message returns True (lenient) when the chat row is
# absent OR has no messages — the streaming-path early return. For the
# MUST-PERSIST commands that means the ack would fire success while NOTHING
# was persisted (silent loss; for QuestionCommit the card would broadcast
# despite no durable write). These assert the dispatch RAISES on a no-op.
def test_question_commit_missing_chat_raises(actor):
  """QuestionCommit against a chat row that does not exist must RAISE, not
  falsely ack success — otherwise the runner broadcasts a card whose
  question_id was never persisted."""
  # No _seed_chat: the row is absent.
  fut = actor.submit(
    QuestionCommit(
      chat_id="ghost", run_token="rt1", snapshot=_question_msg("q1")
    )
  )
  with pytest.raises(Exception):
    _await(fut)


def test_question_commit_empty_transcript_raises(actor):
  """QuestionCommit against a chat with no messages must RAISE: there is no
  assistant message to write the question into, so the question card has no
  durable home. The lenient streaming early-return would falsely ack True."""
  _seed_chat(messages=[])
  fut = actor.submit(
    QuestionCommit(chat_id="c1", run_token="rt1", snapshot=_question_msg("q1"))
  )
  with pytest.raises(Exception):
    _await(fut)


def test_finalize_missing_chat_raises(actor):
  """Finalize against a missing chat row must RAISE so the caller doesn't
  promote the queue / schedule a continuation on a write that never landed."""
  fut = actor.submit(
    Finalize(
      chat_id="ghost",
      run_token="rt1",
      snapshot=_assistant_msg([{"type": "text", "content": "done"}]),
    )
  )
  with pytest.raises(Exception):
    _await(fut)


def test_finalize_empty_transcript_on_existing_chat_is_benign(actor):
  """Finalize against an EXISTING chat whose transcript is empty (a concurrent
  ReplaceTranscript wiped it mid-turn) is BENIGN: there is nothing to finalize
  onto, but the chat still exists, so this is a no-op rather than a persistence
  failure. The ack resolves (NOOP promoted to APPLIED) instead of raising a
  spurious "could not be saved". A finalize against a MISSING/soft-deleted chat
  still raises — covered in test_terminal_completion."""
  _seed_chat(messages=[])
  fut = actor.submit(
    Finalize(
      chat_id="c1",
      run_token="rt1",
      snapshot=_assistant_msg([{"type": "text", "content": "done"}]),
    )
  )
  _await(fut)  # must not raise
  assert _load_chat()["messages"] == [], "a benign NOOP writes nothing"


def test_question_commit_happy_path_acks_and_persists(actor):
  """The happy path is unchanged: an assistant message to write into ->
  QuestionCommit acks True and the block is durably persisted."""
  _seed_chat(
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      _assistant_msg([{"type": "text", "content": "thinking"}]),
    ]
  )
  fut = actor.submit(
    QuestionCommit(chat_id="c1", run_token="rt1", snapshot=_question_msg("q1"))
  )
  assert _await(fut) is True
  chat = _load_chat()
  assert chat["messages"][-1]["blocks"][0]["question_id"] == "q1"


# -- 5. ReplaceTranscript serializes with snapshots -----------------------
def test_replace_transcript_serializes_with_snapshots(actor):
  """A ReplaceTranscript and a PersistTranscript for the same chat run in FIFO
  order on the single actor thread — the later command's state is what
  persists, never a half-merge."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  actor.pause_for_test()
  # Snapshot first, then a full replace — both queued while paused.
  actor.submit(
    PersistTranscript(
      chat_id="c1",
      run_token="rt1",
      snapshot=_assistant_msg([{"type": "text", "content": "stream"}]),
    )
  )
  replaced = [
    {"role": "user", "content": "hi", "ts": 1},
    _assistant_msg([{"type": "text", "content": "replaced"}]),
  ]
  fut = actor.submit(
    ReplaceTranscript(chat_id="c1", run_token="rt1", messages=replaced)
  )
  actor.resume_for_test()
  assert _await(fut) is True
  _await(actor.submit(Barrier()))
  chat = _load_chat()
  # The replace ran AFTER the snapshot (FIFO), so the replaced transcript wins.
  assert chat["messages"][-1]["blocks"][0]["content"] == "replaced"


# -- FIX 4: ReplaceTranscript broad-fences snapshots under ANY run_token --
def test_replace_transcript_broad_fences_other_token_snapshot(actor):
  """ReplaceTranscript replaces the WHOLE transcript, so ANY in-flight
  snapshot for the chat — under ANY run_token — must be fenced or it could
  overwrite the replacement. The exact-key fence only reaches the replace's
  own (chat_id, run_token); a snapshot under a DIFFERENT token would survive
  and clobber. Assert the broad-by-chat fence catches the other-token
  snapshot: its ack resolves None and the replacement wins."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  actor.pause_for_test()
  # A snapshot under the streaming token 'rt-stream'.
  stale = actor.submit(
    PersistTranscript(
      chat_id="c1",
      run_token="rt-stream",
      snapshot=_assistant_msg([{"type": "text", "content": "stream-stale"}]),
    )
  )
  replaced = [
    {"role": "user", "content": "hi", "ts": 1},
    _assistant_msg([{"type": "text", "content": "replaced"}]),
  ]
  # A ReplaceTranscript under a DIFFERENT token ('rt-edit').
  fut = actor.submit(
    ReplaceTranscript(chat_id="c1", run_token="rt-edit", messages=replaced)
  )
  actor.resume_for_test()
  assert _await(fut) is True
  # The other-token snapshot was broad-fenced on the replace's submit.
  assert _await(stale) is None
  _await(actor.submit(Barrier()))
  chat = _load_chat()
  assert chat["messages"][-1]["blocks"][0]["content"] == "replaced"


# -- 6. concurrent append/cancel/promote preserve order -------------------
def test_concurrent_append_cancel_promote_preserve_order(actor):
  """Concurrent AppendPending / CancelPending / PromotePending never lose a
  queue entry — the single actor thread serializes every RMW on
  pending_messages, the race the asyncio queue lock guards today."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  # Append several queued messages concurrently.
  futs = []
  for i in range(6):
    futs.append(
      actor.submit(
        AppendPending(
          chat_id="c1",
          run_token="rt1",
          user_msg={"role": "user", "content": f"m{i}", "ts": 1000 + i},
        )
      )
    )
  results = [_await(f) for f in futs]
  stored_ts = [r["stored"]["ts"] for r in results]
  # Every append got a unique ts (the actor bumps colliders).
  assert len(set(stored_ts)) == len(stored_ts)
  chat = _load_chat()
  assert len(chat["pending_messages"]) == 6
  # Cancel one and promote the queued follow-ups concurrently — neither loses
  # the cancellation nor a queued send.
  cancel_ts = stored_ts[3]
  cf = actor.submit(CancelPending(chat_id="c1", run_token="rt1", ts=cancel_ts))
  pf = actor.submit(PromotePending(chat_id="c1", run_token="rt1"))
  _await(cf)
  promoted = _await(pf)
  assert promoted["promoted"] is not None
  chat = _load_chat()
  remaining_ts = [m["ts"] for m in chat["pending_messages"]]
  assert cancel_ts not in remaining_ts
  assert chat["pending_messages"] == []
  assert "m0" in promoted["promoted"]["content"]
  assert "m3" not in promoted["promoted"]["content"]


# -- 7. StartTurn atomic --------------------------------------------------
def test_start_turn_is_atomic(actor):
  """StartTurn appends the user message, sets the title + provider on the
  first message, and marks the run — one commit, all-or-nothing."""
  _seed_chat(messages=[], session_id="sess-x")
  fut = actor.submit(
    StartTurn(
      chat_id="c1",
      run_token="rt1",
      user_msg={"role": "user", "content": "build me a todo app", "ts": 5},
      title_source="build me a todo app",
      default_provider="codex",
    )
  )
  result = _await(fut)
  assert result["session_id"] == "sess-x"
  assert result["provider"] == "codex"
  # History entries are schemas.ChatMessage, exactly as the production
  # initial-send path builds them — run_chat consumes `.content`, so a raw
  # dict (no `.content` attribute) would break attribute access.
  assert isinstance(result["history"][-1], schemas.ChatMessage)
  assert result["history"][-1].content == "build me a todo app"
  chat = _load_chat()
  assert chat["messages"][-1]["content"] == "build me a todo app"
  assert chat["title"] == "build me a todo app"
  assert chat["provider"] == "codex"
  assert chat["run_status"] == "running"
  assert chat["run_started_at"] is not None


# -- 8. PromotePending collapses queued follow-ups ------------------------
def test_promote_pending_collapses_all_followups(actor):
  """PromotePending combines queued follow-ups into one transcript turn."""
  _seed_chat(
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[
      {"role": "user", "content": "first", "ts": 10},
      {"role": "user", "content": "second", "ts": 11},
    ],
  )
  result = _await(actor.submit(PromotePending(chat_id="c1", run_token="rt1")))
  assert result["promoted"]["content"] == "first\nsecond"
  assert result["promoted"]["ts"] == 10
  chat = _load_chat()
  assert chat["messages"][-1]["content"] == "first\nsecond"
  assert chat["pending_messages"] == []
  assert chat["run_status"] == "running"


def test_promote_pending_uses_first_queued_actor_for_run_attribution(actor):
  """Collapsed queued turns inherit the first queued message's actor.

  An owner follow-up in an app-owned chat carries no app id; an app follow-up
  carries its app id.  The metadata is consumed into ChatRun and must not leak
  into the promoted transcript message.
  """
  app_id = _seed_app()
  _seed_chat(
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[
      {
        "role": "user",
        "content": "from app",
        "ts": 10,
        "_initiated_by_app_id": app_id,
      },
      {"role": "user", "content": "also queued", "ts": 11},
    ],
  )
  result = _await(actor.submit(PromotePending(chat_id="c1", run_token="rt1")))
  assert result["promoted"]["content"] == "from app\nalso queued"
  assert result["promoted"]["_consumed_ts"] == [10, 11]
  assert "_initiated_by_app_id" not in result["promoted"]
  chat = _load_chat()
  assert "_initiated_by_app_id" not in chat["messages"][-1]
  assert "_consumed_ts" not in chat["messages"][-1]
  run = _load_run("rt1")
  assert run["initiated_by_app_id"] == app_id


def test_promote_pending_empty_queue_is_noop(actor):
  """Promoting an empty queue returns promoted=None and leaves the row."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}], pending=[])
  result = _await(actor.submit(PromotePending(chat_id="c1", run_token="rt1")))
  assert result["promoted"] is None
  chat = _load_chat()
  assert chat["messages"] == [{"role": "user", "content": "hi", "ts": 1}]


# -- BLOCKING 1: history entries are schemas.ChatMessage ------------------
def test_promote_pending_history_entries_are_chat_messages(actor):
  """PromotePending's returned history is built from schemas.ChatMessage,
  exactly like chat_queue.promote_pending_messages_locked. run_chat consumes
  `messages[-1].content` (attribute access); a raw dict has no `.content`
  attribute, so the history MUST carry ChatMessage objects."""
  _seed_chat(
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[{"role": "user", "content": "next turn", "ts": 10}],
  )
  result = _await(actor.submit(PromotePending(chat_id="c1", run_token="rt1")))
  assert result["promoted"]["content"] == "next turn"
  history = result["history"]
  assert history, "promoted turn must carry a non-empty history"
  assert all(isinstance(m, schemas.ChatMessage) for m in history)
  # The promoted message is the last history entry the runner sends.
  assert history[-1].role == "user"
  assert history[-1].content == "next turn"


def test_promote_pending_malformed_entry_raises_and_leaves_queue_intact(actor):
  """A transcript entry whose `content` is not a string must NOT silently
  consume the pending turn. Built as schemas.ChatMessage it raises
  ValidationError inside `_promote_pending`, which now RAISES `_PersistFailed`
  (it formerly returned promoted=None) so the turn-end drain maps it to
  FAILED_LEAVE_MARKER — the run marker is LEFT set and the queue stays intact
  for reconciliation / next-POST self-heal. Returning promoted=None was
  indistinguishable from an EMPTY queue, so drain_and_release cleared the
  marker + forgot the chat while the malformed message remained (claiming
  EMPTY_TERMINAL_CLEARED while work remained). The marker-left half is proven
  end-to-end in test_terminal_completion.py
  ::test_malformed_pending_head_leaves_marker_then_reconcile_repairs.

  The CORE invariant this test has always protected is UNCHANGED: the pending
  message is never consumed by a malformed promote.
  """
  from app.chat_writer import _PersistFailed

  # `content` is a dict, not a string: a real dict (so `.get` works) but
  # ChatMessage(content={...}) raises ValidationError. The defensive
  # `or ""` does NOT coerce it because the dict is truthy.
  _seed_chat(
    messages=[{"role": "user", "content": {"nested": "not a string"}, "ts": 1}],
    pending=[{"role": "user", "content": "should survive", "ts": 10}],
  )
  with pytest.raises(_PersistFailed):
    _await(actor.submit(PromotePending(chat_id="c1", run_token="rt1")))
  # The malformed head was NOT consumed: the queue is intact for retry.
  chat = _load_chat()
  assert [m.get("content") for m in chat["pending_messages"]] == [
    "should survive"
  ]


def test_start_turn_history_entries_are_chat_messages(actor):
  """StartTurn's returned history is schemas.ChatMessage objects, so the
  runner's `messages[-1].content` works on the initial send too."""
  _seed_chat(messages=[{"role": "user", "content": "earlier", "ts": 1}])
  result = _await(
    actor.submit(
      StartTurn(
        chat_id="c1",
        run_token="rt1",
        user_msg={"role": "user", "content": "new send", "ts": 2},
        title_source="new send",
      )
    )
  )
  history = result["history"]
  assert all(isinstance(m, schemas.ChatMessage) for m in history)
  assert history[0].content == "earlier"
  assert history[-1].content == "new send"


# -- 9. DB-error session-recreate -----------------------------------------
def test_db_error_recreates_session_and_keeps_serving():
  """A non-transient SQLAlchemyError that poisons the session (NOT a transient
  OperationalError, which the persist helpers swallow as a dropped write)
  fails that ack, recreates the session, and the actor keeps serving."""
  from sqlalchemy.exc import InvalidRequestError

  class _FlakySession:
    """One real session that raises a session-poisoning SQLAlchemyError on the
    first commit, then delegates to a fresh real session on recreate."""

    instances = []

    def __init__(self):
      self._db = SessionLocal()
      self._fail_next_commit = len(_FlakySession.instances) == 0
      _FlakySession.instances.append(self)

    def expire_all(self):
      self._db.expire_all()

    def execute(self, *a, **k):
      return self._db.execute(*a, **k)
    def query(self, *a, **k):
      return self._db.query(*a, **k)

    def commit(self):
      if self._fail_next_commit:
        self._fail_next_commit = False
        raise InvalidRequestError("session is in a broken state")
      self._db.commit()

    def rollback(self):
      self._db.rollback()

    def close(self):
      self._db.close()

  _FlakySession.instances = []
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}], pending=[])
  a = ChatWriterActor(session_factory=_FlakySession)
  a.start()
  try:
    # First command's commit raises SQLAlchemyError -> ack fails, recreate.
    bad = a.submit(
      AppendPending(
        chat_id="c1",
        run_token="rt1",
        user_msg={"role": "user", "content": "boom", "ts": 100},
      )
    )
    with pytest.raises(Exception):
      _await(bad)
    # The actor recreated its session; the next command commits cleanly.
    good = a.submit(
      AppendPending(
        chat_id="c1",
        run_token="rt1",
        user_msg={"role": "user", "content": "ok", "ts": 101},
      )
    )
    res = _await(good)
    assert res["stored"]["content"] == "ok"
    assert len(_FlakySession.instances) == 2  # recreated exactly once
  finally:
    a.stop(timeout=5)


# -- 10. fatal fails callers ----------------------------------------------
def test_fatal_actor_fails_callers():
  """When session recreation itself fails, the actor goes fatal and every
  outstanding + future ack raises (never hangs)."""
  from sqlalchemy.exc import InvalidRequestError

  class _SessionThenBoom:
    """First construction yields a session whose commit raises a session-
    poisoning SQLAlchemyError; the recreate construction raises, taking the
    actor fatal."""

    calls = 0

    def __init__(self):
      _SessionThenBoom.calls += 1
      if _SessionThenBoom.calls >= 2:
        raise RuntimeError("cannot reopen session")
      self._db = SessionLocal()

    def expire_all(self):
      self._db.expire_all()

    def execute(self, *a, **k):
      return self._db.execute(*a, **k)
    def query(self, *a, **k):
      return self._db.query(*a, **k)

    def commit(self):
      raise InvalidRequestError("session is in a broken state")

    def rollback(self):
      self._db.rollback()

    def close(self):
      self._db.close()

  _SessionThenBoom.calls = 0
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}], pending=[])
  a = ChatWriterActor(session_factory=_SessionThenBoom)
  a.start()
  try:
    bad = a.submit(
      AppendPending(
        chat_id="c1",
        run_token="rt1",
        user_msg={"role": "user", "content": "x", "ts": 1},
      )
    )
    with pytest.raises(Exception):
      _await(bad)
    # The actor is fatal now: any later submit fails fast.
    after = a.submit(Barrier())
    with pytest.raises(RuntimeError):
      _await(after)
  finally:
    a.stop(timeout=5)


# -- 11. legacy answer broad-fence ----------------------------------------
def test_legacy_answer_without_question_id_broad_fences_other_tokens(actor):
  """A legacy /question-answers AnswerQuestion has NO live run_token, so the
  exact-key fence cannot reach a snapshot pending under the streaming token.
  The tokenless answer must broad-fence by chat_id — invalidating EVERY
  pending snapshot for the chat across all run_tokens — or a stale snapshot
  under the streaming token clobbers the answer after it commits.

  Distinct keys are the point: the snapshot is under the streaming token
  'rt-stream'; the legacy answer carries no token. The exact-key fence would
  miss 'rt-stream'; the broad fence catches it."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  # Persist a question card under the streaming token.
  _await(
    actor.submit(
      QuestionCommit(
        chat_id="c1",
        run_token="rt-stream",
        snapshot=_assistant_msg(
          [{"type": "question", "questions": [{"question": "Color?"}]}]
        ),
      )
    )
  )
  # Queue a stale snapshot under the STREAMING token (a different key from the
  # tokenless answer), then submit the legacy answer with no run_token.
  actor.pause_for_test()
  stale = actor.submit(
    PersistTranscript(
      chat_id="c1",
      run_token="rt-stream",
      snapshot=_assistant_msg(
        # No answers — this is the snapshot that would WIPE the answer.
        [{"type": "question", "questions": [{"question": "Color?"}]}]
      ),
    )
  )
  fut = actor.submit(
    AnswerQuestion(
      chat_id="c1", run_token="", question_id=None, answers={"a": "Blue"}
    )
  )
  actor.resume_for_test()
  assert _await(fut) is True
  # The stale snapshot under rt-stream was broad-fenced on the answer's
  # submit: its ack resolves to None (accepted, then dropped), it never
  # commits.
  assert _await(stale) is None
  _await(actor.submit(Barrier()))
  chat = _load_chat()
  block = chat["messages"][-1]["blocks"][0]
  # The answer survived: the fenced stale snapshot did not wipe it.
  assert block["answers"] == {"a": "Blue"}


def test_answer_question_no_block_raises(actor):
  """AnswerQuestion against a transcript with no question block raises (so the
  route returns 503 and keeps the pending question for retry)."""
  _seed_chat(
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      _assistant_msg([{"type": "text", "content": "no question here"}]),
    ]
  )
  fut = actor.submit(
    AnswerQuestion(
      chat_id="c1", run_token="rt1", question_id="missing", answers={"a": "b"}
    )
  )
  with pytest.raises(Exception):
    _await(fut)


# -- 12. stop-races-answer never resolves a cancelled future --------------
def test_stop_races_answer_never_resolves_cancelled_future(actor):
  """If the caller's ack future is cancelled before the answer commits (the
  Stop-races-answer window), the actor must not crash on the dead future and
  must still leave the DB consistent — the caller, seeing a cancelled future,
  declines to resolve the PendingQuestion."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  _await(
    actor.submit(
      QuestionCommit(
        chat_id="c1", run_token="rt1", snapshot=_question_msg("q1")
      )
    )
  )
  # Pause, submit the answer, cancel its ack before the consumer resolves it.
  actor.pause_for_test()
  ack = Future()
  cmd = AnswerQuestion(
    ack=ack,
    chat_id="c1",
    run_token="rt1",
    question_id="q1",
    answers={"q1": "Green"},
  )
  actor.submit(cmd)
  assert ack.cancel()  # caller (Stop) abandons the wait
  actor.resume_for_test()
  # The actor survives the cancelled ack (a no-op set), keeps serving.
  _await(actor.submit(Barrier()))
  # The answer still committed (the DB write is independent of the dead ack).
  chat = _load_chat()
  assert chat["messages"][-1]["blocks"][0]["answers"] == {"q1": "Green"}


# -- 13. session_id direct update survives a later transcript commit -------
def test_session_id_direct_update_survives_later_transcript_commit(actor):
  """session_id is an ALLOWED direct writer (it dirties only that column).
  The actor's expire_all before each command re-reads the row, so a later
  transcript commit through the actor does NOT clobber the directly-written
  session_id."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}], session_id="old")
  # Simulate the loop-side direct session_id write (commits on its own session).
  side = SessionLocal()
  try:
    chat = side.query(models.Chat).filter(models.Chat.id == "c1").first()
    chat.session_id = "fresh-session"
    side.commit()
  finally:
    side.close()
  # Now the actor commits a transcript snapshot for the same chat.
  _await(
    actor.submit(
      Finalize(
        chat_id="c1",
        run_token="rt1",
        snapshot=_assistant_msg([{"type": "text", "content": "answer"}]),
      )
    )
  )
  chat = _load_chat()
  # The directly-written session_id is intact (expire_all re-read it).
  assert chat["session_id"] == "fresh-session"
  assert chat["messages"][-1]["blocks"][0]["content"] == "answer"


# -- 14. reconciliation works pre-startup ---------------------------------
def test_reconciliation_works_independent_of_actor():
  """reconcile_interrupted_chats runs BEFORE the actor exists (recovery must
  work when persistence is degraded). It must not need the writer and must
  resolve a stranded 'running' chat directly."""
  from datetime import UTC, datetime

  from app.chat import reconcile_interrupted_chats

  # A chat stranded mid-turn: run_status='running', a partial assistant block.
  db = SessionLocal()
  try:
    chat = models.Chat(
      id="stranded",
      title="t",
      messages=[
        {"role": "user", "content": "go", "ts": 1},
        _assistant_msg(
          [{"type": "tool", "status": "running", "name": "x"}]
        ),
      ],
      pending_messages=[{"role": "user", "content": "queued", "ts": 2}],
      run_status="running",
      run_started_at=datetime.now(UTC),
    )
    db.add(chat)
    db.commit()
    reconciled = reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "stranded" in reconciled
  chat = _load_chat("stranded")
  assert chat["run_status"] is None
  assert chat["pending_messages"] == []  # stranded queue cleared
  # The running tool block was force-completed and an error note appended.
  blocks = chat["messages"][-1]["blocks"]
  assert any(b.get("type") == "error" for b in blocks)
  assert all(
    b.get("status") != "running" for b in blocks if b.get("type") == "tool"
  )


# -- TEST-GAP cleanup: real-dispatch coverage -----------------------------
# These commands were imported but never actually dispatched against the real
# DB; assert each mutates the row (or no-ops correctly) as the production
# helper it replicates would.
def test_clear_pending_empties_queue_and_returns_count(actor):
  """ClearPending empties pending_messages and returns the count removed —
  the Stop / terminal-setup-error path."""
  _seed_chat(
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[
      {"role": "user", "content": "q1", "ts": 10},
      {"role": "user", "content": "q2", "ts": 11},
    ],
  )
  result = _await(actor.submit(ClearPending(chat_id="c1", run_token="rt1")))
  assert result == {"cleared": 2}
  chat = _load_chat()
  assert chat["pending_messages"] == []


def test_clear_pending_empty_queue_is_noop(actor):
  """ClearPending on an already-empty queue returns cleared=0 and commits
  nothing (the production helper skips the commit when nothing changed)."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}], pending=[])
  result = _await(actor.submit(ClearPending(chat_id="c1", run_token="rt1")))
  assert result == {"cleared": 0}
  chat = _load_chat()
  assert chat["pending_messages"] == []


def test_clear_run_status_clears_durable_marker(actor):
  """ClearRunStatus clears run_status + run_started_at once a turn has ended."""
  from datetime import UTC, datetime

  cid = _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  # Mark the run via a throwaway session (the marker the command clears).
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == cid).first()
    chat.run_status = "running"
    chat.run_started_at = datetime.now(UTC)
    db.commit()
  finally:
    db.close()
  result = _await(actor.submit(ClearRunStatus(chat_id="c1", run_token="rt1")))
  assert result is None
  chat = _load_chat()
  assert chat["run_status"] is None
  assert chat["run_started_at"] is None


def test_append_pending_bumps_colliding_ts(actor):
  """Two messages submitted with the SAME ts must not collide: AppendPending
  bumps the second so it is strictly greater (unique React keys client-side
  + unambiguous DELETE-by-ts). The actor serializes the RMW, so the bump is
  deterministic even under concurrency."""
  _seed_chat(messages=[{"role": "user", "content": "hi", "ts": 1}])
  first = _await(
    actor.submit(
      AppendPending(
        chat_id="c1",
        run_token="rt1",
        user_msg={"role": "user", "content": "a", "ts": 500},
      )
    )
  )
  second = _await(
    actor.submit(
      AppendPending(
        chat_id="c1",
        run_token="rt1",
        user_msg={"role": "user", "content": "b", "ts": 500},
      )
    )
  )
  assert first["stored"]["ts"] == 500
  # The colliding ts was bumped strictly above the existing max.
  assert second["stored"]["ts"] > first["stored"]["ts"]
  chat = _load_chat()
  ts_values = [m["ts"] for m in chat["pending_messages"]]
  assert len(set(ts_values)) == len(ts_values)  # all unique


def test_persist_error_writes_error_snapshot_to_row(actor):
  """PersistError commits the error-state snapshot as the chat's last
  assistant message — a real DB mutation, fire-and-forget (acks without
  raising)."""
  _seed_chat(
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      _assistant_msg([{"type": "text", "content": "partial"}]),
    ]
  )
  fut = actor.submit(
    PersistError(
      chat_id="c1",
      run_token="rt1",
      snapshot=_assistant_msg(
        [
          {"type": "text", "content": "partial"},
          {"type": "error", "content": "provider error"},
        ]
      ),
    )
  )
  assert _await(fut) is True
  chat = _load_chat()
  blocks = chat["messages"][-1]["blocks"]
  assert any(
    b.get("type") == "error" and b.get("content") == "provider error"
    for b in blocks
  )
