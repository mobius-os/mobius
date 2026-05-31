"""Single-writer chat-persistence actor.

One dedicated thread owns a SQLAlchemy Session and consumes a tagged
FIFO queue of domain commands.  Async callers submit a command and await
its `concurrent.futures.Future` ack via `asyncio.wrap_future`.  The actor
NEVER touches asyncio primitives or `ChatBroadcast` (those stay
loop-owned), so the blocking `db.commit()` that SQLite's `busy_timeout`
can stall for up to five seconds no longer runs on the event loop.

Commands are DOMAIN-level (`PersistTranscript`, `Finalize`,
`PersistError`, `AnswerQuestion`, `Barrier`, `DrainAndStop`) rather than
row-level so a later milestone can swap their dispatch for normalized-row
writes without rewriting the actor.

See ~/mobius-persistence-redesign-2026-05-31.md for the rationale and
~/mobius-chat-writer-actor-plan-2026-05-31.md for the staged rollout.
This module is dormant until the activation milestone wires the runners
and routes onto it.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field

log = logging.getLogger("moebius.chat_writer")


# -- Commands (domain-level; a later milestone swaps their dispatch) -----
@dataclass
class _Command:
  """Base for every queued command.

  `ack` is the `Future` the submitter awaits.  `submit()` allocates one
  when absent so callers that don't care about the result still get a
  consistent contract.
  """

  ack: Future | None = field(default=None)


@dataclass
class PersistTranscript(_Command):
  """Coalescible full-snapshot write: latest per (chat_id, run_token) wins.

  The snapshot is the COMPLETE current assistant message (a dict with a
  `blocks` list), so dropping an intermediate snapshot loses nothing —
  the later one already contains every block the earlier one had.
  """

  chat_id: str = ""
  run_token: str = ""
  snapshot: dict = field(default_factory=dict)


@dataclass
class Finalize(_Command):
  """Terminal full-snapshot write for a turn.  Never coalesces."""

  chat_id: str = ""
  run_token: str = ""
  snapshot: dict = field(default_factory=dict)


@dataclass
class PersistError(_Command):
  """Error-state write for a turn.  Never coalesces."""

  chat_id: str = ""
  run_token: str = ""
  snapshot: dict = field(default_factory=dict)


@dataclass
class AnswerQuestion(_Command):
  """Identity-keyed answer write.  Never coalesces."""

  chat_id: str = ""
  run_token: str = ""
  question_id: str = ""
  answers: dict = field(default_factory=dict)


@dataclass
class Barrier(_Command):
  """Acked only after every preceding command is processed.

  Carries no DB work — it is a fence the submitter can await to know the
  queue has drained up to this point.
  """


@dataclass
class DrainAndStop(_Command):
  """Drain to this point, close the session, ack, exit the thread."""


class ChatWriterActor:
  """A single thread that serializes chat-domain persistence.

  The thread opens ONE session from `session_factory` and loops:
  `get()` a command, dispatch it, ack it.  One failing command does not
  kill the actor (it logs, rolls back, fails that command's ack, and
  keeps going).  A thread-fatal error (e.g. the session factory raising)
  sets `_fatal`, fails every pending and future ack, and stops — callers
  see a raised ack rather than a hang.
  """

  def __init__(self, session_factory):
    self._session_factory = session_factory
    self._q: "queue.Queue[_Command]" = queue.Queue()
    self._thread: threading.Thread | None = None
    self._fatal = False

  # -- lifecycle ---------------------------------------------------------
  def start(self) -> None:
    self._thread = threading.Thread(
      target=self._run, name="chat-writer", daemon=True
    )
    self._thread.start()

  def submit(self, cmd: _Command) -> Future:
    """Enqueue a command and return its ack `Future`.

    Allocates an ack when the caller didn't supply one.  When the actor
    is already fatal the ack is failed immediately (never enqueued) so a
    caller after a thread death gets a raised result rather than a hang.
    """
    if cmd.ack is None:
      cmd.ack = Future()
    if self._fatal:
      _safe_set_exception(cmd.ack, RuntimeError("chat writer is in a fatal state"))
      return cmd.ack
    self._q.put(cmd)
    return cmd.ack

  def submit_test_persist(self, chat_id, run_token, payload) -> Future:
    """Test-only hook: enqueue a per-command persist carrying `payload`.

    Used by the FIFO test to assert raw ordering — each command is
    enqueued directly so every payload is preserved in submit order.
    """
    cmd = PersistTranscript(
      chat_id=chat_id, run_token=run_token, snapshot={"_test_payload": payload}
    )
    return self.submit(cmd)

  def stop(self, timeout: float = 10.0) -> None:
    """Drain to a `DrainAndStop`, wait its ack, then join the thread."""
    fut = self.submit(DrainAndStop())
    try:
      fut.result(timeout=timeout)
    except Exception:
      # A fatal actor fails this ack; still join the thread below so a
      # caller's stop() never raises out of teardown.
      pass
    finally:
      if self._thread:
        self._thread.join(timeout=timeout)

  # -- consumer (the writer thread) --------------------------------------
  def _run(self) -> None:
    db = self._session_factory()
    try:
      while True:
        cmd = self._q.get()
        if isinstance(cmd, DrainAndStop):
          _safe_set_result(cmd.ack, None)
          return
        if isinstance(cmd, Barrier):
          _safe_set_result(cmd.ack, None)
          continue
        try:
          result = self._dispatch(db, cmd)
          _safe_set_result(cmd.ack, result)
        except Exception:
          # One bad command must not kill the actor: log, roll back the
          # poisoned transaction, fail this command's ack, keep serving.
          log.exception(
            "chat writer command failed: %s", type(cmd).__name__
          )
          try:
            db.rollback()
          except Exception:
            log.exception("chat writer rollback failed")
          _safe_set_exception(cmd.ack, sys.exc_info()[1])
    except Exception:
      # Thread-fatal (e.g. the queue or session itself broke): fail every
      # outstanding and future ack so no awaiter hangs forever.
      self._fatal = True
      log.exception("chat writer thread died")
      self._drain_failing()
    finally:
      try:
        db.close()
      except Exception:
        log.exception("chat writer session close failed")

  def _dispatch(self, db, cmd: _Command):
    """Apply one command's persistence effect.

    Test-backed and minimal for the dormant milestone: snapshot-bearing
    commands route their snapshot through the session stub's record
    hooks.  A later milestone replaces these branches with the real
    `_update_last_assistant_message` / `_finalize_response` writes (and
    the `AnswerQuestion` block-merge), reusing this same dispatch seam.
    """
    if isinstance(cmd, PersistTranscript):
      return self._commit_snapshot(db, cmd.snapshot)
    raise NotImplementedError(type(cmd).__name__)

  @staticmethod
  def _commit_snapshot(db, snapshot: dict):
    """Persist one snapshot through the session.

    Dormant-milestone behavior routes through the session stub's record
    hooks: `_test_payload` (FIFO test) records the bare payload, anything
    else records the full snapshot.  Real DB writes arrive in the
    activation milestone.
    """
    if "_test_payload" in snapshot:
      db.commit_test(snapshot["_test_payload"])
    else:
      db.record_commit(snapshot)
    return True

  def _drain_failing(self) -> None:
    """Fail every queued command's ack after a thread-fatal error.

    `_fatal` is already set, so `submit()` fails future acks inline; this
    drains whatever was already queued so no awaiter hangs.
    """
    while True:
      try:
        cmd = self._q.get_nowait()
      except queue.Empty:
        return
      _safe_set_exception(cmd.ack, RuntimeError("chat writer is dead"))


# -- ack guards (double-set safe) ----------------------------------------
def _safe_set_result(ack: Future | None, value) -> None:
  if ack is not None and not ack.done():
    ack.set_result(value)


def _safe_set_exception(ack: Future | None, exc: BaseException | None) -> None:
  if ack is not None and not ack.done():
    ack.set_exception(exc or RuntimeError("chat writer failed"))
