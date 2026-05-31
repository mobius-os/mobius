"""Unit tests for the single-writer chat-persistence actor.

The actor (`app.chat_writer.ChatWriterActor`) owns one Session on a
dedicated thread and consumes a FIFO queue of domain commands. These
tests exercise the actor in isolation with a `_RecordingSession` stub
that records committed payloads instead of touching SQLite — no DB, no
asyncio, no broadcast. The production write path is wired in a later
milestone; here the actor is dormant.
"""

import threading
from concurrent.futures import Future

import pytest

from app.chat_writer import (
  AnswerQuestion,
  Barrier,
  ChatWriterActor,
  Finalize,
  PersistError,
  PersistTranscript,
)


class _RecordingSession:
  """Minimal Session stub for the actor's unit tests.

  `commit`/`close`/`rollback` are no-ops except that the Task-1 FIFO
  test routes a recorded payload through `commit_test`; later tests
  record full snapshots via `record_commit`. The actor never inspects
  the stub beyond these hooks.
  """

  def __init__(self, sink: list):
    self._sink = sink

  def commit_test(self, payload) -> None:
    self._sink.append(payload)

  def record_commit(self, snapshot) -> None:
    self._sink.append(snapshot)

  def commit(self) -> None:
    pass

  def rollback(self) -> None:
    pass

  def close(self) -> None:
    pass


def test_actor_processes_commands_in_fifo_order():
  seen: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(seen))
  actor.start()
  try:
    for i in range(5):
      actor.submit_test_persist(chat_id="c1", run_token="t1", payload=i)
    fut = actor.submit(Barrier())  # acked only after all prior processed
    fut.result(timeout=5)
    assert seen == [0, 1, 2, 3, 4]
  finally:
    actor.stop(timeout=5)


def test_persist_transcript_coalesces_per_run_token():
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  try:
    actor.pause_for_test()  # hold the consumer so the batch accumulates
    for i in range(10):
      actor.submit(
        PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": i})
      )
    actor.resume_for_test()
    actor.submit(Barrier()).result(timeout=5)
    # Only the LATEST snapshot for (c1,t1) must commit; earlier ones drop.
    assert commits == [{"n": 9}]
  finally:
    actor.stop(timeout=5)


def test_finalize_and_error_never_coalesce():
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  try:
    actor.pause_for_test()
    # Interleave coalescible snapshots with must-persist commands. Each
    # Finalize/PersistError must commit its own snapshot — never dropped,
    # never replaced by a neighbouring transcript snapshot.
    actor.submit(PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 0}))
    actor.submit(Finalize(chat_id="c1", run_token="t1", snapshot={"final": 1}))
    actor.submit(PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 2}))
    actor.submit(PersistError(chat_id="c1", run_token="t1", snapshot={"error": 3}))
    actor.resume_for_test()
    actor.submit(Barrier()).result(timeout=5)
    assert {"final": 1} in commits
    assert {"error": 3} in commits
  finally:
    actor.stop(timeout=5)


def test_failed_command_acks_with_exception_but_actor_survives():
  class _BoomOnFirst:
    """Raises on the first commit, then behaves normally."""

    def __init__(self, sink: list):
      self._sink = sink
      self._calls = 0

    def record_commit(self, snapshot):
      self._calls += 1
      if self._calls == 1:
        raise ValueError("boom")
      self._sink.append(snapshot)

    def commit(self):
      pass

    def rollback(self):
      pass

    def close(self):
      pass

  committed: list = []
  actor = ChatWriterActor(session_factory=lambda: _BoomOnFirst(committed))
  actor.start()
  try:
    bad = actor.submit(
      Finalize(chat_id="c1", run_token="t1", snapshot={"bad": True})
    )
    with pytest.raises(ValueError):
      bad.result(timeout=5)
    # The actor survives: the next command still commits.
    good = actor.submit(
      Finalize(chat_id="c1", run_token="t1", snapshot={"ok": True})
    )
    assert good.result(timeout=5) is True
    assert committed == [{"ok": True}]
  finally:
    actor.stop(timeout=5)


def test_thread_death_fails_pending_acks():
  class _DeadlySession:
    """Closing raises, but the real kill is commit raising a non-Exception.

    To force the thread-fatal path (distinct from a per-command failure),
    `record_commit` raises BaseException, which the per-command try/except
    (Exception) does not catch — it propagates to the outer handler that
    sets `_fatal` and fails every ack.
    """

    def record_commit(self, snapshot):
      raise KeyboardInterrupt("thread-fatal")

    def commit(self):
      pass

    def rollback(self):
      pass

    def close(self):
      pass

  actor = ChatWriterActor(session_factory=lambda: _DeadlySession())
  actor.start()
  try:
    # This command triggers the fatal path on the thread.
    killer = actor.submit(
      Finalize(chat_id="c1", run_token="t1", snapshot={"x": 1})
    )
    with pytest.raises(BaseException):
      killer.result(timeout=5)
    # A command submitted AFTER the thread died must still fail fast, not
    # hang forever.
    after = actor.submit(Barrier())
    with pytest.raises(RuntimeError):
      after.result(timeout=5)
  finally:
    actor.stop(timeout=5)


def _boom():
  raise RuntimeError("session factory unavailable")


def test_startup_failure_is_caught_and_writer_reports_unhealthy():
  from app import chat_writer

  # A session_factory that raises must NOT crash start_writer; get_writer()
  # returns a writer whose submit() acks with an exception (never hangs).
  chat_writer.start_writer(session_factory=_boom)
  try:
    w = chat_writer.get_writer()
    fut = w.submit(chat_writer.Barrier())
    with pytest.raises(RuntimeError):
      fut.result(timeout=5)
  finally:
    chat_writer.stop_writer(timeout=5)


# -- CRITICAL 1: coalesced dispatch exception must resolve the snapshot ack --
class _BoomOnNthCommit:
  """Records snapshots but raises the given exception on the Nth commit.

  Lets a test force a failure *inside* the coalesced `_SnapshotReady`
  dispatch (which commits the popped pending snapshot), where the
  originating `PersistTranscript`'s ack is the popped snapshot's ack, not
  the marker's (the marker carries no ack).
  """

  def __init__(self, sink: list, fail_on: int, exc: BaseException):
    self._sink = sink
    self._calls = 0
    self._fail_on = fail_on
    self._exc = exc

  def record_commit(self, snapshot):
    self._calls += 1
    if self._calls == self._fail_on:
      raise self._exc
    self._sink.append(snapshot)

  def commit(self):
    pass

  def rollback(self):
    pass

  def close(self):
    pass


def test_coalesced_commit_exception_resolves_originating_ack_and_survives():
  # The originating PersistTranscript's ack must RAISE (not hang) when the
  # coalesced commit fails with a normal Exception, and the actor survives
  # to serve the next command.
  committed: list = []
  actor = ChatWriterActor(
    session_factory=lambda: _BoomOnNthCommit(
      committed, fail_on=1, exc=ValueError("coalesced boom")
    )
  )
  actor.start()
  try:
    fut = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 0})
    )
    with pytest.raises(ValueError):
      fut.result(timeout=5)
    # Actor survives: a subsequent coalesced write commits normally.
    fut2 = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 1})
    )
    assert fut2.result(timeout=5) is True
    actor.submit(Barrier()).result(timeout=5)
    assert committed == [{"n": 1}]
  finally:
    actor.stop(timeout=5)


def test_coalesced_commit_baseexception_resolves_originating_ack_and_goes_fatal():
  # A BaseException during a coalesced commit must fail the originating
  # PersistTranscript's ack (not leave it hanging) and take the actor
  # fatal so later submits fail fast.
  committed: list = []
  actor = ChatWriterActor(
    session_factory=lambda: _BoomOnNthCommit(
      committed, fail_on=1, exc=KeyboardInterrupt("coalesced fatal")
    )
  )
  actor.start()
  try:
    fut = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 0})
    )
    # The ack must resolve with the ACTUAL commit failure (a KeyboardInterrupt
    # the per-command Exception handler never caught) — not hang into a
    # concurrent.futures TimeoutError, which would mean it leaked.
    with pytest.raises(KeyboardInterrupt):
      fut.result(timeout=5)
    # Actor is fatal: a post-death submit fails fast rather than hanging.
    after = actor.submit(Barrier())
    with pytest.raises(RuntimeError):
      after.result(timeout=5)
  finally:
    actor.stop(timeout=5)


# -- CRITICAL 2: a stale pre-fence marker must not commit a post-fence snapshot
def test_stale_marker_does_not_reorder_finalize_before_post_fence_snapshot():
  # Interleaving: submit S1 (marker M1 queued) -> consumer dequeues M1 but
  # pauses before _take_pending -> submit Finalize F (fence: invalidates S1)
  # -> submit S2 (marker M2 queued) -> release M1. The stale M1 must NOT
  # commit S2 ahead of F. Committed order must be [F, S2]; S1 acks to None.
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))

  reached_take = threading.Event()
  release_take = threading.Event()

  def on_snapshot_ready():
    # Fires after a _SnapshotReady is dequeued, before _take_pending.
    reached_take.set()
    release_take.wait(timeout=5)

  actor._on_snapshot_ready_for_test = on_snapshot_ready
  actor.start()
  try:
    s1 = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 1})
    )
    # Wait until the consumer has dequeued M1 and is paused before take.
    assert reached_take.wait(timeout=5)
    # Fence: Finalize invalidates S1 and clears the key's pending snapshot.
    f = actor.submit(Finalize(chat_id="c1", run_token="t1", snapshot={"final": True}))
    # New snapshot AFTER the fence.
    s2 = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 2})
    )
    # Release the stale M1 — it must be a no-op (wrong generation).
    release_take.set()
    actor.submit(Barrier()).result(timeout=5)
    assert f.result(timeout=5) is True
    # S1 was superseded/invalidated by the fence; its ack resolves to None.
    assert s1.result(timeout=5) is None
    # S2 committed AFTER F — the stale marker did not reorder it.
    assert commits == [{"final": True}, {"n": 2}]
    assert s2.result(timeout=5) is None or s2.result(timeout=5) is True
  finally:
    release_take.set()
    actor.stop(timeout=5)


# -- CRITICAL 3: post-stop submits fail fast, pre-stop commands still drain ---
def test_stop_rejects_post_stop_submits_but_drains_prior_commands():
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))

  # A latch in commit lets us hold the consumer mid-drain so a concurrent
  # submit lands AFTER stop() has set _stopping + enqueued DrainAndStop.
  in_commit = threading.Event()
  release_commit = threading.Event()

  class _LatchingSession(_RecordingSession):
    def record_commit(self, snapshot):
      in_commit.set()
      release_commit.wait(timeout=5)
      super().record_commit(snapshot)

  actor = ChatWriterActor(session_factory=lambda: _LatchingSession(commits))
  actor.pause_for_test()
  actor.start()
  # Two commands enqueued BEFORE stop must both drain.
  pre1 = actor.submit(Finalize(chat_id="c1", run_token="t1", snapshot={"pre": 1}))
  pre2 = actor.submit(Finalize(chat_id="c1", run_token="t1", snapshot={"pre": 2}))
  actor.resume_for_test()
  assert in_commit.wait(timeout=5)  # consumer is inside pre1's commit

  stop_done = threading.Event()
  stop_err: list = []

  def do_stop():
    try:
      actor.stop(timeout=10)
    except BaseException as exc:  # pragma: no cover - defensive
      stop_err.append(exc)
    finally:
      stop_done.set()

  stopper = threading.Thread(target=do_stop, name="stopper")
  stopper.start()
  try:
    # Spin until _stopping flips (stop() sets it under the lock before join).
    for _ in range(500):
      if getattr(actor, "_stopping", False):
        break
      threading.Event().wait(0.005)
    assert getattr(actor, "_stopping", False), "stop() never set _stopping"
    # A concurrent post-stop submit must fail fast, not hang.
    after = actor.submit(Barrier())
    with pytest.raises(RuntimeError):
      after.result(timeout=5)
    # Let the drain finish.
    release_commit.set()
    assert pre1.result(timeout=5) is True
    assert pre2.result(timeout=5) is True
    assert commits == [{"pre": 1}, {"pre": 2}]
    assert stop_done.wait(timeout=5)
    assert not stop_err
  finally:
    release_commit.set()
    stopper.join(timeout=5)


def test_concurrent_stop_calls_all_return_without_hanging():
  # Two concurrent stop() calls must both return (neither hangs on an
  # unresolved DrainAndStop ack). The consumer exits at the first stop
  # marker, so a second enqueued marker's ack would otherwise never resolve.
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  done = []
  done_lock = threading.Lock()

  def stopper():
    actor.stop(timeout=5)
    with done_lock:
      done.append(True)

  threads = [threading.Thread(target=stopper) for _ in range(2)]
  for t in threads:
    t.start()
  for t in threads:
    t.join(timeout=8)
  assert not any(t.is_alive() for t in threads), "a stop() call hung"
  assert len(done) == 2


def test_repeated_stop_is_idempotent():
  import time

  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  actor.stop(timeout=5)
  # A second stop after the thread already exited must return FAST — not
  # enqueue a fresh DrainAndStop and burn the timeout waiting on an ack the
  # dead thread can never serve.
  start = time.monotonic()
  actor.stop(timeout=5)
  assert time.monotonic() - start < 1.0, "redundant stop() waited on a stranded ack"


# -- IMPORTANT 4: a Future cancellation must not break a concurrent submit ----
from concurrent.futures import InvalidStateError


class _RacingFuture(Future):
  """A Future whose set_* raises InvalidStateError exactly once.

  Models the real race: a concurrent cancellation lands AFTER `done()`
  reports False (so the `_safe_set_*` guard passes) but BEFORE `set_*`
  runs. `done()` stays False so the guard does not skip; the first
  `set_result`/`set_exception` then raises InvalidStateError the way a
  just-cancelled Future would.
  """

  def __init__(self):
    super().__init__()
    self._raised = False

  def set_result(self, value):
    if not self._raised:
      self._raised = True
      raise InvalidStateError("cancelled in the window")
    super().set_result(value)

  def set_exception(self, exc):
    if not self._raised:
      self._raised = True
      raise InvalidStateError("cancelled in the window")
    super().set_exception(exc)


def test_cancellation_race_in_set_result_does_not_break_concurrent_submit():
  # The producer supersedes a pending snapshot by calling set_result(None)
  # on its ack. If a cancellation raced into the done()->set_result window
  # (modeled by _RacingFuture), the non-atomic guard would raise
  # InvalidStateError out of submit() and strand the SUPERSEDING command.
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  try:
    actor.pause_for_test()
    c1 = PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 0})
    c1.ack = _RacingFuture()
    actor.submit(c1)
    # Superseding submit: the producer sets c1.ack result -> InvalidStateError
    # in the window. This must NOT propagate out of submit().
    c2 = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 1})
    )
    actor.resume_for_test()
    actor.submit(Barrier()).result(timeout=5)
    assert commits == [{"n": 1}]
    assert c2.result(timeout=5) is True
  finally:
    actor.stop(timeout=5)


def test_cancellation_race_in_set_exception_does_not_break_actor():
  # Same race on the failure path: a must-persist command's dispatch fails,
  # the consumer calls set_exception on a future that races to cancellation
  # mid-window. The actor must survive and the next command still commits.
  class _BoomFirst(_RecordingSession):
    def __init__(self, sink):
      super().__init__(sink)
      self._calls = 0

    def record_commit(self, snapshot):
      self._calls += 1
      if self._calls == 1:
        raise ValueError("boom")
      super().record_commit(snapshot)

  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _BoomFirst(commits))
  actor.start()
  try:
    bad = Finalize(chat_id="c1", run_token="t1", snapshot={"bad": True})
    bad.ack = _RacingFuture()
    actor.submit(bad)
    # The consumer's set_exception hits the InvalidStateError window; the
    # per-command handler must swallow it and keep serving.
    good = actor.submit(Finalize(chat_id="c1", run_token="t1", snapshot={"ok": True}))
    assert good.result(timeout=5) is True
    assert commits == [{"ok": True}]
  finally:
    actor.stop(timeout=5)


# -- IMPORTANT 5: one-thread invariant -----------------------------------------
def test_repeated_start_is_rejected():
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession([]))
  actor.start()
  try:
    with pytest.raises(RuntimeError):
      actor.start()
  finally:
    actor.stop(timeout=5)


def test_start_writer_is_idempotent_no_orphan_thread():
  from app import chat_writer

  before = {t.name for t in threading.enumerate()}
  chat_writer.start_writer(session_factory=lambda: _RecordingSession([]))
  try:
    first = chat_writer.get_writer()
    # A second start_writer must NOT spawn a second live writer thread.
    chat_writer.start_writer(session_factory=lambda: _RecordingSession([]))
    second = chat_writer.get_writer()
    assert first is second
    writer_threads = [
      t for t in threading.enumerate()
      if t.name == "chat-writer" and t.name not in before
    ]
    assert len(writer_threads) == 1, "start_writer spawned an orphan thread"
  finally:
    chat_writer.stop_writer(timeout=5)


# -- Strengthened ordering + multi-producer + superseded-ack tests -------------
def test_finalize_and_error_commit_in_exact_submit_order():
  # Strengthen the membership-only check: assert the FULL committed order.
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  try:
    actor.pause_for_test()
    actor.submit(PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 0}))
    actor.submit(Finalize(chat_id="c1", run_token="t1", snapshot={"final": 1}))
    actor.submit(PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 2}))
    actor.submit(PersistError(chat_id="c1", run_token="t1", snapshot={"error": 3}))
    actor.resume_for_test()
    actor.submit(Barrier()).result(timeout=5)
    # Both transcripts were invalidated by the following must-persist
    # commands, so only the two terminal writes commit, in submit order.
    assert commits == [{"final": 1}, {"error": 3}]
  finally:
    actor.stop(timeout=5)


def test_superseded_transcript_ack_resolves_to_none():
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  try:
    actor.pause_for_test()
    first = actor.submit(
      PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 0})
    )
    actor.submit(PersistTranscript(chat_id="c1", run_token="t1", snapshot={"n": 1}))
    actor.resume_for_test()
    actor.submit(Barrier()).result(timeout=5)
    # The superseded first snapshot's ack resolves to None (accepted, then
    # dropped) — it must never hang.
    assert first.result(timeout=5) is None
    assert commits == [{"n": 1}]
  finally:
    actor.stop(timeout=5)


def test_concurrent_multi_producer_submits_all_acks_resolve():
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  producers = 6
  per_producer = 8
  futures: list = []
  futures_lock = threading.Lock()
  try:
    def producer(pid: int):
      local: list = []
      for i in range(per_producer):
        local.append(
          actor.submit(
            Finalize(
              chat_id=f"chat-{pid}",
              run_token=f"rt-{pid}",
              snapshot={"p": pid, "i": i},
            )
          )
        )
      with futures_lock:
        futures.extend(local)

    threads = [
      threading.Thread(target=producer, args=(pid,)) for pid in range(producers)
    ]
    for t in threads:
      t.start()
    for t in threads:
      t.join(timeout=10)
    # A Barrier acked after all submitted commands proves the queue drained.
    actor.submit(Barrier()).result(timeout=10)
    assert len(futures) == producers * per_producer
    for fut in futures:
      assert fut.result(timeout=5) is True
    # FIFO per producer: each producer's snapshots commit in submit order.
    for pid in range(producers):
      seen = [c["i"] for c in commits if c["p"] == pid]
      assert seen == list(range(per_producer))
  finally:
    actor.stop(timeout=10)


def test_concurrent_submits_with_a_barrier_all_resolve():
  commits: list = []
  actor = ChatWriterActor(session_factory=lambda: _RecordingSession(commits))
  actor.start()
  acks: list = []
  acks_lock = threading.Lock()
  try:
    def producer(pid: int):
      f = actor.submit(
        Finalize(chat_id=f"c{pid}", run_token="t1", snapshot={"p": pid})
      )
      b = actor.submit(Barrier())
      with acks_lock:
        acks.extend([f, b])

    threads = [threading.Thread(target=producer, args=(i,)) for i in range(10)]
    for t in threads:
      t.start()
    for t in threads:
      t.join(timeout=10)
    for ack in acks:
      ack.result(timeout=5)  # every ack (Finalize + Barrier) resolves
    assert len(commits) == 10
  finally:
    actor.stop(timeout=10)
