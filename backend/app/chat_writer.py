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

Concurrency invariant: ack `Future`s are NEVER resolved while a producer
lock (`_fatal_lock`/`_pending_lock`) is held.  Every method that resolves an
ack from a producer path (`submit`'s fatal/stopping reject, `_enqueue_snapshot`'s
supersession, `_invalidate_pending`'s stale drop, `_go_fatal`'s queue drain)
collects the `(ack, value)` pairs while holding the lock and resolves them AFTER
releasing it; the consumer (`_dispatch`/`_take_pending`) likewise resolves
outside `_pending_lock`.  The intended consumer of an ack is
`asyncio.wrap_future`, whose done-callback only *schedules* resolution on the
loop thread.  But because no `set_*` runs under a lock, even a SYNCHRONOUS
`add_done_callback` that re-enters `submit()`/`stop()` cannot deadlock — the
re-entrant call reacquires a free lock.  Keep it that way: do not move an ack
resolution back inside a `with` block.
"""

from __future__ import annotations

import itertools
import logging
import queue
import sys
import threading
from concurrent.futures import Future, InvalidStateError
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

  `_generation` is the key's fence epoch at the time this snapshot was
  recorded (stamped by `_enqueue_snapshot`); `_take_pending` only commits a
  snapshot whose generation matches the marker that popped it, so a fence
  (`Finalize`/`PersistError`/`AnswerQuestion`) cannot be reordered behind a
  stale marker.
  """

  chat_id: str = ""
  run_token: str = ""
  snapshot: dict = field(default_factory=dict)
  _generation: int = 0


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
  one, lands; see `submit`).  `generation` stamps the key's fence epoch at
  enqueue time so a stale pre-fence marker is a no-op (see `_take_pending`).
  """

  chat_id: str = ""
  run_token: str = ""
  generation: int = 0


@dataclass
class _TestPersist(_Command):
  """Test-only NON-coalescing per-command persist carrying a raw payload.

  Routes through `submit()` (so it respects the fatal/stopping gate) and is
  enqueued directly via the generic `else` branch, then committed in
  `_dispatch`.  Used by the FIFO test to assert raw submit ordering without
  the coalescing collapse that `PersistTranscript` applies.
  """

  chat_id: str = ""
  run_token: str = ""
  payload: object = None


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
    # Serializes the check-and-set in `start()` so two concurrent callers
    # can't both pass the `_thread is None` check and each spawn a consumer
    # thread (which would violate the single-consumer invariant and corrupt
    # FIFO ordering).  The second caller raises instead.
    self._start_lock = threading.Lock()
    self._fatal = False
    # `_stopping` is set under `_fatal_lock` by `stop()` before it enqueues
    # the `DrainAndStop` marker.  Once set, `submit()` rejects new commands
    # (failing their ack) so a command can't queue behind the stop marker
    # and then strand when the thread exits at the marker.
    self._stopping = False
    # Serializes the fatal/stopping check+enqueue in `submit` against the
    # set-fatal+drain in the thread's fatal handler and against `stop`'s
    # set-stopping+enqueue, so a command can't slip into the queue after
    # the final drain (or behind the stop marker) and then hang forever.
    self._fatal_lock = threading.Lock()
    # Coalescing state, guarded on the producer side by `_pending_lock`.
    # `_pending` holds the latest snapshot command per key; `_outstanding`
    # is the set of keys whose `_SnapshotReady` marker is already queued,
    # so a burst of snapshots enqueues at most one marker per key.
    # `_generation` is a per-key fence counter, bumped by
    # `_invalidate_pending`; every snapshot and its marker are stamped with
    # the key's generation at enqueue time so a stale pre-fence marker can't
    # commit a post-fence (new-generation) snapshot (see `_take_pending`).
    self._pending: dict[tuple[str, str], PersistTranscript] = {}
    self._outstanding: set[tuple[str, str]] = set()
    self._generation: dict[tuple[str, str], int] = {}
    self._pending_lock = threading.Lock()
    # The ack of the snapshot currently being committed in a `_SnapshotReady`
    # dispatch (the originating `PersistTranscript`'s ack, popped off
    # `_pending`).  The marker carries no ack, so without this the consumer's
    # per-command `except` (which fails `cmd.ack`, the marker's None ack) and
    # the fatal `except BaseException`/`_go_fatal` would never resolve the
    # popped snapshot's ack — a permanent hang.  Set before the commit,
    # cleared after it succeeds; failed on every error path.
    self._inflight_ack: Future | None = None
    # Optional consumer gate (tests only): cleared by `pause_for_test`,
    # set by `resume_for_test`.  Set by default so production never gates.
    self._gate = threading.Event()
    self._gate.set()
    # Optional test hook fired after a `_SnapshotReady` is dequeued but
    # BEFORE `_take_pending`, so a test can deterministically interleave a
    # fence between dequeue and take.  None in production.
    self._on_snapshot_ready_for_test = None

  # -- lifecycle ---------------------------------------------------------
  def start(self) -> None:
    """Spawn the single writer thread.

    Enforces the one-thread invariant: a second `start()` on the same actor
    raises rather than spawning an orphan daemon thread that would consume
    the same queue and corrupt FIFO ordering.  The check-and-set runs under
    `_start_lock` so two CONCURRENT callers can't both pass the
    `_thread is None` check (the window between check and assignment, widened
    by the `Thread(...)` construction, is otherwise a real race) — exactly
    one wins, the rest raise.
    """
    with self._start_lock:
      if self._thread is not None:
        raise RuntimeError("chat writer already started")
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
    # Collect every ack to resolve and DEFER its resolution until after the
    # lock is released (see the deadlock note below).
    reject_exc: BaseException | None = None
    dropped_ack: Future | None = None
    # Hold `_fatal_lock` across the check + enqueue so the thread's fatal
    # handler (which takes the same lock to set `_fatal` then drain) and
    # `stop` (which takes it to set `_stopping` then enqueue the marker)
    # can't interleave and strand this command in the queue after the drain
    # or behind the stop marker.  No `_safe_set_*` runs INSIDE this block: a
    # synchronous `add_done_callback` could re-enter `submit()`/`stop()` and
    # deadlock reacquiring `_fatal_lock`, so all ack resolution is hoisted
    # below the `with`.
    with self._fatal_lock:
      if self._fatal:
        reject_exc = RuntimeError("chat writer is in a fatal state")
      elif self._stopping:
        reject_exc = RuntimeError("chat writer is stopping")
      elif isinstance(cmd, (Finalize, PersistError, AnswerQuestion)):
        dropped_ack = self._invalidate_pending(cmd.chat_id, cmd.run_token)
        self._q.put(cmd)
      elif isinstance(cmd, PersistTranscript):
        dropped_ack = self._enqueue_snapshot(cmd)
      else:
        self._q.put(cmd)
    # Lock released — now safe to resolve acks even if a done-callback
    # re-enters the actor.
    if reject_exc is not None:
      _safe_set_exception(cmd.ack, reject_exc)
      return cmd.ack
    if dropped_ack is not None:
      _safe_set_result(dropped_ack, None)
    return cmd.ack

  def submit_test_persist(self, chat_id, run_token, payload) -> Future:
    """Test-only hook: enqueue a per-command persist carrying `payload`.

    Used by the FIFO test to assert raw ordering — a NON-coalescing
    `_TestPersist` so every payload is preserved in submit order
    (coalescing is exercised by the snapshot tests).  Routes through
    `submit()` so it respects the fatal/stopping gate rather than writing
    to the queue directly.
    """
    return self.submit(
      _TestPersist(chat_id=chat_id, run_token=run_token, payload=payload)
    )

  # -- test hooks --------------------------------------------------------
  def pause_for_test(self) -> None:
    """Hold the consumer at the top of its loop (tests only)."""
    self._gate.clear()

  def resume_for_test(self) -> None:
    """Release a consumer paused by `pause_for_test` (tests only)."""
    self._gate.set()

  # -- coalescing helpers (producer side) --------------------------------
  def _enqueue_snapshot(self, cmd: PersistTranscript) -> Future | None:
    """Record the latest snapshot for the key; enqueue one marker if none.

    A snapshot that supersedes an earlier uncommitted one acks the
    earlier one with `None` (accepted into the pipeline, then dropped) so
    no caller hangs waiting on a coalesced write.  The marker is
    lightweight — the consumer pops the latest snapshot at processing
    time, collapsing a flurry to one commit of the newest value.

    The snapshot and (if newly enqueued) its marker are stamped with the
    key's CURRENT generation.  Coalescing stays within a single generation
    (latest wins, one marker); a fence (`_invalidate_pending`) bumps the
    generation so a marker queued before the fence cannot commit a snapshot
    recorded after it.

    Returns the superseded snapshot's ack (to be resolved with `None` by the
    caller AFTER it has released `submit`'s `_fatal_lock`) or `None`.  The
    resolution is deferred to the caller because a synchronous
    `add_done_callback` could re-enter `submit()`/`stop()` and deadlock if the
    ack were resolved while a producer lock is still held (see `submit`).
    """
    key = (cmd.chat_id, cmd.run_token)
    with self._pending_lock:
      generation = self._generation.get(key, 0)
      cmd._generation = generation
      superseded = self._pending.get(key)
      self._pending[key] = cmd
      already_queued = key in self._outstanding
      self._outstanding.add(key)
    if not already_queued:
      self._q.put(
        _SnapshotReady(
          chat_id=cmd.chat_id, run_token=cmd.run_token, generation=generation
        )
      )
    return superseded.ack if superseded is not None else None

  def _invalidate_pending(self, chat_id: str, run_token: str) -> Future | None:
    """Drop any coalescible snapshot for the key before a must-persist write.

    Prevents a stale snapshot enqueued before a `Finalize`/`PersistError`/
    `AnswerQuestion` from committing AFTER it and clobbering the terminal
    state.  The dropped snapshot's ack resolves to `None`.  This is the
    FENCE: it bumps the key's generation so any `_SnapshotReady` marker
    already in the queue (stamped with the OLD generation) becomes a no-op
    in `_take_pending`, even if a NEW snapshot for the key is enqueued
    afterward (which gets the new generation and its own marker).

    Returns the stale snapshot's ack (to be resolved with `None` by the
    caller AFTER releasing `submit`'s `_fatal_lock`) or `None` — same
    deferred-resolution contract as `_enqueue_snapshot`, to keep ack
    resolution off the producer locks.
    """
    key = (chat_id, run_token)
    with self._pending_lock:
      self._generation[key] = self._generation.get(key, 0) + 1
      stale = self._pending.pop(key, None)
      self._outstanding.discard(key)
    return stale.ack if stale is not None else None

  def _take_pending(
    self, chat_id: str, run_token: str, generation: int
  ) -> PersistTranscript | None:
    """Pop the snapshot to commit for a marker, honouring the fence.

    Returns the pending snapshot ONLY when the marker's generation matches
    BOTH the key's current generation AND the pending snapshot's own
    generation; otherwise the marker is stale (a fence advanced the
    generation after it was enqueued) and this is a no-op.  This guarantees
    a pre-fence marker can never commit a post-fence (new-generation)
    snapshot — the reordering bug.  On a match the snapshot is popped so a
    later same-key fence + snapshot can't double-commit it.
    """
    key = (chat_id, run_token)
    with self._pending_lock:
      if self._generation.get(key, 0) != generation:
        # A fence advanced the generation after this marker was enqueued;
        # leave any current (newer-generation) pending snapshot in place for
        # its own marker.
        return None
      pending = self._pending.get(key)
      if pending is None or pending._generation != generation:
        return None
      self._pending.pop(key, None)
      self._outstanding.discard(key)
      return pending

  def _gc_generation(self, chat_id: str, run_token: str) -> None:
    """Delete a dead key's fence epoch so `_generation` can't grow unbounded.

    `_pending`/`_outstanding` are cleaned per key, but `_generation[key]` was
    never deleted, so every finalized turn (run_token is per-turn) leaked one
    permanent entry.  Called by the consumer after a TERMINAL dispatch
    (`Finalize`/`PersistError`/`AnswerQuestion`) and after a coalesced
    snapshot commits.  Deletes the entry ONLY when the key is fully quiescent
    — neither `_pending` nor `_outstanding` holds it — so a still-outstanding
    marker (or a post-fence snapshot enqueued for the same key) keeps its
    epoch.  A later snapshot for a deleted key simply restarts its generation
    at 0; that is safe because the deletion happens between turns (the fence
    write already drained), so no stale marker can use the reset epoch to
    reorder a snapshot ahead of an earlier fence.
    """
    key = (chat_id, run_token)
    with self._pending_lock:
      if key not in self._pending and key not in self._outstanding:
        self._generation.pop(key, None)

  def stop(self, timeout: float = 10.0) -> None:
    """Drain to a `DrainAndStop`, wait its ack, then join the thread.

    Sets `_stopping` under `_fatal_lock` and enqueues the `DrainAndStop`
    marker DIRECTLY (bypassing the public-`submit` reject — the stop command
    itself must not be rejected).  Commands already enqueued before this
    point stay ahead of the marker and drain in FIFO order; any command
    submitted concurrently AFTER `_stopping` is set is rejected by `submit`
    (ack failed) rather than stranded behind the marker after the thread
    exits.

    Idempotent + concurrency-safe: only the FIRST caller to flip `_stopping`
    enqueues the single `DrainAndStop` (the consumer exits at the first
    marker, so a second marker's ack would never resolve).  A later or
    concurrent `stop()` skips the enqueue and just joins — no stranded ack,
    no wasted timeout wait.
    """
    drain = DrainAndStop(ack=Future())
    enqueued = False
    with self._fatal_lock:
      if self._fatal:
        # Already dead: every queued ack was failed by `_go_fatal`; just
        # fail the marker's ack inline and fall through to join.
        _safe_set_exception(
          drain.ack, RuntimeError("chat writer is in a fatal state")
        )
      elif self._stopping:
        # A prior stop() already enqueued the one DrainAndStop; don't add a
        # second the consumer will never reach.  Just join below.
        pass
      else:
        self._stopping = True
        self._q.put(drain)
        enqueued = True
    if enqueued:
      try:
        drain.ack.result(timeout=timeout)
      except Exception:
        # A fatal actor fails this ack; still join the thread below so a
        # caller's stop() never raises out of teardown.
        pass
    # Always join — whether we enqueued the marker, skipped it (a sibling
    # stop already did), or the actor was fatal — so stop() never returns
    # with the writer thread still alive.
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
      log.exception("chat writer session factory failed")
      self._go_fatal()
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
          # For a coalesced `_SnapshotReady`, `cmd.ack` is the marker's None
          # ack — the ack a caller awaits is the popped snapshot's
          # (`_inflight_ack`), so fail that too.
          log.exception(
            "chat writer command failed: %s", type(cmd).__name__
          )
          try:
            db.rollback()
          except Exception:
            log.exception("chat writer rollback failed")
          exc = sys.exc_info()[1]
          _safe_set_exception(cmd.ack, exc)
          _safe_set_exception(self._inflight_ack, exc)
          self._inflight_ack = None
        finally:
          # A terminal command ends its turn; reclaim the key's fence epoch
          # whether the commit succeeded or raised (the run_token won't be
          # reused).  GC is a no-op unless the key is fully quiescent, so a
          # post-fence snapshot enqueued for the same key keeps its epoch
          # (FIX C).
          if isinstance(cmd, (Finalize, PersistError, AnswerQuestion)):
            self._gc_generation(cmd.chat_id, cmd.run_token)
    except BaseException:
      # Thread-fatal (a BaseException the per-command handler didn't
      # catch — e.g. the queue or session itself broke): fail every
      # outstanding and future ack so no awaiter hangs forever.  Also
      # fail the in-flight command's ack, which the inner handler never
      # reached, AND the popped snapshot's ack (`_inflight_ack`) — which
      # `_go_fatal` can't reach because it was already removed from
      # `_pending`.
      log.exception("chat writer thread died")
      exc = sys.exc_info()[1]
      _safe_set_exception(cmd.ack if cmd is not None else None, exc)
      _safe_set_exception(self._inflight_ack, exc)
      self._inflight_ack = None
      self._go_fatal()
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
      if self._on_snapshot_ready_for_test is not None:
        # Test hook: lets a test interleave a fence between dequeue and take.
        self._on_snapshot_ready_for_test()
      pending = self._take_pending(cmd.chat_id, cmd.run_token, cmd.generation)
      if pending is None:
        # Superseded/invalidated, or a stale pre-fence marker — no-op.
        return None
      # Record the originating snapshot's ack as in-flight so BOTH the
      # per-command `except` and the fatal `except BaseException` resolve it
      # (the marker's own ack is None).  Cleared only after the commit and
      # its ack succeed.
      self._inflight_ack = pending.ack
      result = self._commit_snapshot(db, pending.snapshot)
      _safe_set_result(pending.ack, result)
      self._inflight_ack = None
      # The committed snapshot was popped from `_pending`/`_outstanding`; if no
      # newer snapshot re-added the key it is now dead — reclaim its generation
      # epoch so the map can't grow unbounded (FIX C).
      self._gc_generation(cmd.chat_id, cmd.run_token)
      return None
    if isinstance(cmd, _TestPersist):
      # Test-only non-coalescing per-command persist (raw FIFO ordering).
      return self._commit_snapshot(db, {"_test_payload": cmd.payload})
    if isinstance(cmd, PersistTranscript):
      # Defensive: a directly-enqueued PersistTranscript (no current path
      # does this) still commits its own snapshot.
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

  def _go_fatal(self) -> None:
    """Mark the actor fatal and fail every queued ack — under `_fatal_lock`.

    Holding the lock across set-fatal + drain serializes with `submit`,
    which checks `_fatal` and enqueues under the same lock.  So a
    concurrent `submit` either (a) sees `_fatal=True` and fails its ack
    inline without enqueuing, or (b) finished its enqueue first, in which
    case the drain below catches that command.  Either way no awaiter is
    stranded.  Snapshot markers carry no ack (their `PersistTranscript`'s
    ack is the submitter's, already failed by submit or by being in
    `_pending`), so failing them is a harmless no-op.  Also fail any
    pending coalesced snapshots so their acks don't hang.

    Ack resolution is hoisted OUT of both locks: the queue is fully drained
    (and `_pending` snapshotted) under the lock — preserving the
    set-fatal-then-drain race contract above — but every `_safe_set_*` runs
    after the `with` block, so a synchronous `add_done_callback` re-entering
    `submit()`/`stop()` can't deadlock on `_fatal_lock`/`_pending_lock`.
    """
    dead = RuntimeError("chat writer is dead")
    drained: list[Future | None] = []
    with self._fatal_lock:
      self._fatal = True
      while True:
        try:
          cmd = self._q.get_nowait()
        except queue.Empty:
          break
        drained.append(cmd.ack)
    with self._pending_lock:
      pending = list(self._pending.values())
      self._pending.clear()
      self._outstanding.clear()
    # Locks released — resolve every collected ack now.
    for ack in drained:
      _safe_set_exception(ack, dead)
    for snap in pending:
      _safe_set_exception(snap.ack, dead)
    # Belt-and-suspenders: if a popped-but-uncommitted snapshot's ack is
    # still in flight (it's no longer in `_pending`, so the loop above
    # missed it), fail it too.  Already-resolved acks are a no-op.
    _safe_set_exception(self._inflight_ack, dead)
    self._inflight_ack = None


# -- ack guards (double-set + cancellation-race safe) --------------------
# The `done()` check + `set_*` is NOT atomic: a concurrent cancellation
# (a caller's `asyncio.wrap_future` future being cancelled) can land between
# the two and make `set_*` raise `InvalidStateError`.  These guards run on
# producer paths mid-enqueue too, so an unguarded raise would abort an
# unrelated submission.  Treat an already-done/cancelled future as a no-op.
def _safe_set_result(ack: Future | None, value) -> None:
  if ack is None or ack.done():
    return
  try:
    ack.set_result(value)
  except InvalidStateError:
    pass


def _safe_set_exception(ack: Future | None, exc: BaseException | None) -> None:
  if ack is None or ack.done():
    return
  try:
    ack.set_exception(exc or RuntimeError("chat writer failed"))
  except InvalidStateError:
    pass


# -- per-turn run identity ------------------------------------------------
# Process-scoped, unique, monotonic run-token allocation.  One token is
# allocated per TURN (initial send and each continuation get their own),
# centrally — outside the per-turn sink — so run identity has a single
# source rather than being derived from sink/chat state.  The token is an
# OPAQUE string with no semantics callers may lean on; at a later milestone
# it BECOMES the durable `chat_runs.id`, so keeping it opaque now avoids a
# second identity to migrate.  `itertools.count` under a lock makes the
# sequence both unique and monotonic even when turns start concurrently on
# different threads.
_token_counter = itertools.count(1)
_token_lock = threading.Lock()


def alloc_run_token() -> str:
  """Allocate the next process-unique, monotonic run token.

  Thread-safe: `itertools.count.__next__` is not guaranteed atomic across
  arbitrary producers, so the increment runs under `_token_lock`.  Returns
  an opaque `"rt-<n>"` string — callers must treat it as an identity tag,
  not a number.
  """
  with _token_lock:
    return f"rt-{next(_token_counter)}"


# -- module singleton + lifespan accessors -------------------------------
# One actor per process.  `start_writer` is called from the FastAPI
# lifespan AFTER db init + crash reconciliation (which must run before the
# actor exists — recovery cannot depend on a healthy writer); `stop_writer`
# drains on shutdown.
_writer: ChatWriterActor | None = None
# Serializes the singleton check+create in `start_writer` (and the
# clear in `stop_writer`) so two concurrent callers can't both pass the
# "already started" check and each construct + start a writer, orphaning one
# daemon thread that keeps consuming a stranded queue (FIX F).
_writer_lock = threading.Lock()


def start_writer(session_factory=None) -> None:
  """Construct and start the process writer, idempotently.

  Defaults to `app.database.SessionLocal`.  A startup failure (thread
  spawn) is caught and the writer is marked fatal rather than raised —
  the app must boot even when persistence is degraded, so the recovery
  surface stays reachable.  A session factory that only raises when
  CALLED is tolerated separately on the writer thread (see `_run`), which
  sets `_fatal` and fails acks rather than dying silently.

  Idempotent + concurrency-safe: the singleton check+create runs under
  `_writer_lock`, so concurrent callers see exactly one writer.  If a live
  (non-fatal) writer already exists this is a no-op rather than overwriting
  the singleton — that would orphan the old daemon thread (still consuming
  its queue) and strand its awaiters.  A previously-fatal writer IS replaced
  so a degraded process can recover by re-calling `start_writer`.
  """
  global _writer
  with _writer_lock:
    if _writer is not None and not _writer._fatal:
      log.debug("chat writer already started; start_writer is a no-op")
      return
    if session_factory is None:
      from app.database import SessionLocal
      session_factory = SessionLocal
    writer = ChatWriterActor(session_factory)
    try:
      writer.start()
    except Exception:
      log.exception("chat writer failed to start; persistence degraded")
      writer._fatal = True  # submit() will ack-with-exception
    # Publish only after construction + start, so a concurrent caller either
    # sees the old singleton (and no-ops) or the fully-started new one.
    _writer = writer


def get_writer() -> ChatWriterActor:
  """Return the process writer; raise if `start_writer` hasn't run."""
  if _writer is None:
    raise RuntimeError("chat writer not started")
  return _writer


def stop_writer(timeout: float = 10.0) -> None:
  """Drain + join the process writer if it exists."""
  global _writer
  with _writer_lock:
    writer = _writer
    _writer = None
  if writer is not None:
    writer.stop(timeout=timeout)
