"""Unit tests for the single-writer chat-persistence actor.

The actor (`app.chat_writer.ChatWriterActor`) owns one Session on a
dedicated thread and consumes a FIFO queue of domain commands. These
tests exercise the actor in isolation with a `_RecordingSession` stub
that records committed payloads instead of touching SQLite — no DB, no
asyncio, no broadcast. The production write path is wired in a later
milestone; here the actor is dormant.
"""

import threading

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
