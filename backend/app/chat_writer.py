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


@dataclass
class _SnapshotReady(_Command):
  """Internal marker: a coalesced snapshot is ready for (chat_id, run_token).

  Enqueued at most once per key while a snapshot is outstanding; the
  consumer pops the latest pending snapshot for the key and commits it.
  Carries no ack — coalesced writes are fire-and-forget (the originating
  `PersistTranscript`'s ack is acked once its snapshot, or a superseding
  one, lands; see `submit`).
  """

  chat_id: str = ""
  run_token: str = ""


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
    # Coalescing state, guarded on the producer side by `_pending_lock`.
    # `_pending` holds the latest snapshot command per key; `_outstanding`
    # is the set of keys whose `_SnapshotReady` marker is already queued,
    # so a burst of snapshots enqueues at most one marker per key.
    self._pending: dict[tuple[str, str], PersistTranscript] = {}
    self._outstanding: set[tuple[str, str]] = set()
    self._pending_lock = threading.Lock()
    # Optional consumer gate (tests only): cleared by `pause_for_test`,
    # set by `resume_for_test`.  Set by default so production never gates.
    self._gate = threading.Event()
    self._gate.set()

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

    `PersistTranscript` is coalesced: the latest snapshot per
    `(chat_id, run_token)` is recorded and a single `_SnapshotReady`
    marker enqueued; a snapshot superseded by a newer one before it
    commits is acked with `None` (accepted, then dropped).  Every
    must-persist command (`Finalize`/`PersistError`/`AnswerQuestion`)
    first invalidates that key's pending snapshot so a stale snapshot
    can't land after the terminal write.
    """
    if cmd.ack is None:
      cmd.ack = Future()
    if self._fatal:
      _safe_set_exception(cmd.ack, RuntimeError("chat writer is in a fatal state"))
      return cmd.ack
    if isinstance(cmd, (Finalize, PersistError, AnswerQuestion)):
      self._invalidate_pending(cmd.chat_id, cmd.run_token)
      self._q.put(cmd)
    elif isinstance(cmd, PersistTranscript):
      self._enqueue_snapshot(cmd)
    else:
      self._q.put(cmd)
    return cmd.ack

  def submit_test_persist(self, chat_id, run_token, payload) -> Future:
    """Test-only hook: enqueue a per-command persist carrying `payload`.

    Used by the FIFO test to assert raw ordering — bypasses coalescing so
    every payload is preserved in submit order (coalescing is exercised
    by the snapshot tests).
    """
    cmd = PersistTranscript(
      chat_id=chat_id, run_token=run_token, snapshot={"_test_payload": payload}
    )
    cmd.ack = Future()
    self._q.put(cmd)
    return cmd.ack

  # -- test hooks --------------------------------------------------------
  def pause_for_test(self) -> None:
    """Hold the consumer at the top of its loop (tests only)."""
    self._gate.clear()

  def resume_for_test(self) -> None:
    """Release a consumer paused by `pause_for_test` (tests only)."""
    self._gate.set()

  # -- coalescing helpers (producer side) --------------------------------
  def _enqueue_snapshot(self, cmd: PersistTranscript) -> None:
    """Record the latest snapshot for the key; enqueue one marker if none.

    A snapshot that supersedes an earlier uncommitted one acks the
    earlier one with `None` (accepted into the pipeline, then dropped) so
    no caller hangs waiting on a coalesced write.  The marker is
    lightweight — the consumer pops the latest snapshot at processing
    time, collapsing a flurry to one commit of the newest value.
    """
    key = (cmd.chat_id, cmd.run_token)
    with self._pending_lock:
      superseded = self._pending.get(key)
      self._pending[key] = cmd
      already_queued = key in self._outstanding
      self._outstanding.add(key)
    if superseded is not None:
      _safe_set_result(superseded.ack, None)
    if not already_queued:
      self._q.put(_SnapshotReady(chat_id=cmd.chat_id, run_token=cmd.run_token))

  def _invalidate_pending(self, chat_id: str, run_token: str) -> None:
    """Drop any coalescible snapshot for the key before a must-persist write.

    Prevents a stale snapshot enqueued before a `Finalize`/`PersistError`/
    `AnswerQuestion` from committing AFTER it and clobbering the terminal
    state.  The dropped snapshot's ack resolves to `None`.  An already-
    queued marker may still be in the queue; the consumer skips it
    because `_pending` no longer has the key.
    """
    key = (chat_id, run_token)
    with self._pending_lock:
      stale = self._pending.pop(key, None)
      self._outstanding.discard(key)
    if stale is not None:
      _safe_set_result(stale.ack, None)

  def _take_pending(self, chat_id: str, run_token: str) -> PersistTranscript | None:
    """Pop the latest snapshot for the key (or None if already invalidated)."""
    key = (chat_id, run_token)
    with self._pending_lock:
      self._outstanding.discard(key)
      return self._pending.pop(key, None)

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
    try:
      db = self._session_factory()
    except BaseException:
      # The session factory raising at first use is thread-fatal: there
      # is no session to write through.  Mark fatal and fail every queued
      # ack rather than dying silently and hanging awaiters.
      self._fatal = True
      log.exception("chat writer session factory failed")
      self._drain_failing()
      return
    cmd: _Command | None = None
    try:
      while True:
        self._gate.wait()
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
    except BaseException:
      # Thread-fatal (a BaseException the per-command handler didn't
      # catch — e.g. the queue or session itself broke): fail every
      # outstanding and future ack so no awaiter hangs forever.  Also
      # fail the in-flight command's ack, which the inner handler never
      # reached.
      self._fatal = True
      log.exception("chat writer thread died")
      _safe_set_exception(cmd.ack if cmd is not None else None, sys.exc_info()[1])
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
    if isinstance(cmd, _SnapshotReady):
      pending = self._take_pending(cmd.chat_id, cmd.run_token)
      if pending is None:
        # Superseded by a must-persist command that invalidated the key.
        return None
      result = self._commit_snapshot(db, pending.snapshot)
      _safe_set_result(pending.ack, result)
      return None
    if isinstance(cmd, PersistTranscript):
      # Reached only via `submit_test_persist`, which bypasses coalescing
      # and enqueues the command directly; kept for that path.
      return self._commit_snapshot(db, cmd.snapshot)
    if isinstance(cmd, (Finalize, PersistError)):
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
