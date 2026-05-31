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
  Barrier,
  ChatWriterActor,
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
