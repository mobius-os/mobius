"""Per-chat pending-message queue (ticket 033).

Three operations touch `chat.pending_messages` (the JSON column):
append (POST /messages), cancel (DELETE /pending), and promote
(turn-end drain).

DB serialization authority is the single-writer actor (C2), NOT this
lock. Every JSON-blob read-modify-write now runs as a writer-actor
command (`AppendPending` / `CancelPending` / `PromotePending` /
`ClearPending`) on the actor's one thread, so the lost-update race the
lock used to guard is closed at the source — the actor is the sole
runtime mutator of the blob.

What `get_lock` still guards is the asyncio-side COMPOUND decision the
turn-end drain makes: "promote queued follow-ups AND, if nothing was promoted,
release the `_starting` claim + forget the chat" must be one atomic
critical section against a racing POST that checks `is_chat_running` and
takes the start path. That handoff is loop-state bookkeeping, not the DB
write. The actor NEVER acquires this lock, and awaiting an actor ack
WHILE holding the lock is safe (the actor runs on its own thread and
never reaches back for the lock, so there is no lock-ordering cycle).

Lock-storage correctness invariants:

  - `_locks` is a `WeakValueDictionary`. Entries collect when no
    caller holds a reference, so the dict can't grow unbounded
    across the long-running container.
  - `get_lock` is fully synchronous — NO `await` between the get +
    None-check + insert. The asyncio scheduler can only run
    another task at an await point, so two concurrent callers for
    the same chat_id walk through the function in series and
    receive the same `asyncio.Lock` instance. Introducing an
    await mid-method here would break the atomic get-or-create
    and let two callers get two different locks for the same
    chat — silently re-racing the _starting handoff.

`drain_and_release` is a composite primitive: take the lock,
promote queued follow-ups (via the actor), and if the queue was
empty release the `_starting` claim and forget the chat — all under one
lock acquisition. It does NOT call back into `run_chat`; the caller
(chat.py:_run_chat_impl) schedules the continuation AFTER the lock
releases.

Markerless pending queues are a tolerated steady state. A Stop's
`ClearPending` committing just before a racing POST's `AppendPending`
leaves `run_status=None` with a non-empty queue. Boot reconciliation
(which only scans `run_status="running"`) deliberately does NOT consume
these — auto-promoting at startup would spawn a turn after a crash. The
repair path is the NEXT POST's stale-pending drain: it claims
`mark_starting` and promotes queued follow-ups. Reconciliation only logs a warning
so an accumulating queue is visible (see `reconcile_interrupted_chats`).
"""

from __future__ import annotations

import asyncio
import enum
import weakref

from sqlalchemy.orm import Session

from app import schemas


# Bound on EVERY terminal lock acquisition (the turn-end drain, the
# no-owner / auth-error / unsupported-provider cleanup, and Stop's queue
# cleanup). = 2·ACK_TIMEOUT_SECS + busy_timeout: a terminal transition may
# await two sequential strict acks (PromotePending → ClearRunStatus) plus
# SQLite's 5s busy_timeout, so a healthy-but-contended terminal lock holder
# completes well inside this bound. Exceeding it means the holder is wedged
# — the timeout converts that hang into a FAILED_LEAVE_MARKER disposition
# (transport error + done, marker LEFT set for reconciliation) rather than
# stalling the turn forever. A patchable module constant so tests can
# monkeypatch it small and trip the bound deterministically without waiting
# 65 real seconds.
TERMINAL_LOCK_TIMEOUT_SECS = 65.0


class TerminalDisposition(enum.Enum):
  """How a turn's locked terminal transition resolved.

  The single decision shared by `run_chat` / `_complete_turn` /
  `drain_and_release`, replacing the old loose bool + post-hoc generation
  re-read. `run_chat` no longer independently decides whether to clear the
  durable run marker after `_run_chat_impl` returns — the clear happens
  INSIDE the locked terminal transition per the disposition and the single
  marker invariant (clear IFF the current owner reached a durable terminal
  state AND no continuation work remains; otherwise LEAVE it set for
  startup reconciliation).
  """

  CONTINUATION_PROMOTED = "continuation_promoted"
  # A queued message was promoted; the marker stays continuously set and
  # ownership passes to the scheduled continuation. Do NOT clear/forget.
  EMPTY_TERMINAL_CLEARED = "empty_terminal_cleared"
  # Queue empty, terminal state durable; marker cleared + chat forgotten,
  # all inside the one bounded lock (clear-before-forget ordering).
  STOP_HANDOFF_CLEARED = "stop_handoff_cleared"
  # A Stop-bumped generation reached terminal persistence and cleared the
  # marker for the immediate successor generation it still owns.
  STALE_NO_ACTION = "stale_no_action"
  # A newer generation owns this chat (generation mismatch / Stop handed
  # off to a newer run); this run touches nothing — no clear, no forget.
  FAILED_LEAVE_MARKER = "failed_leave_marker"
  # A terminal persistence failure (Finalize / PromotePending ack raised or
  # timed out) OR a terminal lock-acquisition timeout. The marker is LEFT
  # set so reconciliation recovers the incomplete turn + queued messages;
  # no continuation is scheduled.
  LIMIT_PARKED = "limit_parked"
  # The turn ended on a provider rate/usage-limit kill. The marker is cleared
  # (the turn is over) but the pending queue is deliberately NOT drained —
  # promoting it would fire every queued message straight into the same limit
  # (the "limit storm"). The queue is preserved and self-heals on the user's
  # next send via the stale-pending drain. No auto-resume: the user resends
  # (or waits for the limit to reset) themselves.
  DRAINED_FOR_RESTART = "drained_for_restart"
  # The turn was interrupted by a drain-gated restart (design §2.2), NOT by
  # Stop. Its partial blocks + a "paused for a platform update" note were
  # finalized, but the durable run marker AND the pending queue are
  # DELIBERATELY LEFT INTACT — unlike a Stop handoff (which clears the marker)
  # or an empty-terminal (which clears + forgets). Boot reconcile finalizes the
  # preserved marker and marks the note resumable so the owner's one-tap Resume
  # works; the queue self-heals on the next send. Never promoted at drain time —
  # promoting would start a turn while the worker is shutting down.


_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
  weakref.WeakValueDictionary()
)


def get_lock(chat_id: str) -> asyncio.Lock:
  """Returns the per-chat queue lock, creating it if needed.

  Atomic get-or-create — see module docstring for why this MUST
  stay synchronous, and for why this lock is no longer the DB
  serialization authority (the writer actor is).
  """
  lock = _locks.get(chat_id)
  if lock is None:
    lock = asyncio.Lock()
    _locks[chat_id] = lock
  return lock


def reset_for_tests() -> None:
  """Drops the lock registry. Test fixtures call this so a lock
  held by a leaked task from a prior test can't be returned to
  the next test's caller."""
  global _locks
  _locks = weakref.WeakValueDictionary()


async def promote_pending_messages_locked(
  db: Session,
  chat_id: str,
  run_token: str,
) -> tuple[list[schemas.ChatMessage], dict | None, str | None]:
  """Inner promote logic. PRECONDITION: caller holds the per-chat
  queue lock.

  Routes the promote through the single-writer actor's `PromotePending`
  command (C2): the actor — the SOLE runtime mutator of the JSON blobs —
  builds the next-turn history, moves queued follow-ups into the transcript,
  sets the run marker, and commits, all under `(chat_id, run_token)`. The
  ack is awaited (commit-before-ack) so the caller sees the promote land
  before it schedules the continuation. The asyncio queue lock still
  serializes this critical section's _starting handoff against a racing
  POST; the actor NEVER acquires that lock, and awaiting its ack while
  holding the lock is safe (the actor runs on its own thread).

  `db` is unused now (the actor owns the write through its own session)
  but kept in the signature so the two callers' shape is unchanged.

  Returns (next_messages, promoted_message, session_id) on success.
  Returns ([], None, session_id) when the pending queue is empty. Raises
  if the actor ack fails — missing row / dropped commit / a MALFORMED
  pending entry (the actor leaves the queue intact and fails the ack rather
  than returning promoted=None, which would be indistinguishable from an
  empty queue and let the caller clear the marker on stranded work) — so
  the turn-end caller surfaces a transport error / leaves the marker
  rather than promoting a write that never landed.
  """
  del db  # the actor owns the JSON-blob write on its own session
  if not chat_id:
    return [], None, None
  from app.chat_writer import PromotePending, await_ack, get_writer

  ack = get_writer().submit(
    PromotePending(chat_id=chat_id, run_token=run_token)
  )
  result = await await_ack(ack)
  promoted = result["promoted"]
  if promoted is None:
    # Empty queue — nothing to promote (the actor returned promoted=None
    # with an empty history). A MALFORMED head instead RAISES out of
    # await_ack above (the actor fails the ack), so it never reaches here.
    return [], None, result["session_id"]
  return result["history"], promoted, result["session_id"]


async def promote_pending_messages(
  db: Session,
  chat_id: str,
  run_token: str,
) -> tuple[list[schemas.ChatMessage], dict | None, str | None]:
  """Atomically promotes queued follow-ups into the
  transcript via the writer actor.

  Held under the per-chat queue lock so the _starting handoff doesn't
  race append (POST /messages) or cancel (DELETE /pending/{cid}); the
  JSON RMW itself is serialized by the actor. `run_token` is the
  promoted turn's persistence run identity (the scheduler allocates it
  once and threads it into both this promote and the continuation's
  run_chat, so the marker PromotePending sets matches the runner's
  token).

  This function does NOT claim _starting — the caller ensures exclusive
  promotion (mark_starting before call in the stale-pending path, or by
  being the only finally block for a given run in the turn-end path).

  The lock acquisition is bounded by `TERMINAL_LOCK_TIMEOUT_SECS`,
  matching every other terminal lock (the turn-end drain, the setup-error
  cleanups, Stop's queue cleanup). This is the stale-pending promotion path
  (chats_stream.send_message): a wedged lock holder would otherwise hang the
  POST that triggered the drain. On a timeout the asyncio.TimeoutError
  propagates to the caller, which discards _starting and surfaces the error
  rather than blocking on a stuck lock; the queue stays intact for retry.
  """
  if not chat_id:
    return [], None, None
  async with asyncio.timeout(TERMINAL_LOCK_TIMEOUT_SECS):
    async with get_lock(chat_id):
      return await promote_pending_messages_locked(db, chat_id, run_token)


async def drain_and_release(
  db: Session,
  chat_id: str,
  run_gen: int | None,
  run_token: str,
  *,
  discard_starting,
  forget_chat,
  clear_run_status_strict,
  current_generation,
  ending_run_token: str = "",
) -> tuple[dict | None, list, str | None, "TerminalDisposition"]:
  """End-of-turn queue drain. Returns (next_user, next_messages,
  next_session_id, disposition) for the caller to publish + schedule.

  Under ONE bounded per-chat queue lock acquisition
  (`asyncio.timeout(TERMINAL_LOCK_TIMEOUT_SECS)` around `get_lock`):
    - Promotes pending_messages (if any) via the actor's
      `PromotePending` (keyed on `run_token`, the continuation's token).
      Promoted follow-ups → `CONTINUATION_PROMOTED`: the marker stays
      continuously set (PromotePending re-set it for the next turn) and
      ownership passes to the scheduled continuation; do NOT clear/forget.
    - If nothing to promote AND we_own_gen, clears the durable run marker
      (strict `ClearRunStatus`, identity-keyed on `ending_run_token`), then
      releases _starting, then forgets the chat — the clear-before-forget
      ordering, ALL inside this lock. The clear naming the finishing run's
      token means a fresh `StartTurn` that set a new marker mid-drain isn't
      wiped: the actor sees the new owner and no-ops our clear. Returns
      `EMPTY_TERMINAL_CLEARED`.

  Doing this in a single locked critical section closes the race
  between the run_chat finally and a POST that arrives in the window
  after the subprocess exits but before _starting is released. Both
  ends serialize on the same lock; the JSON RMW itself is serialized by
  the actor. Whichever side wins the lock, the message is either
  promoted here or POST takes the start path.

  Ownership is re-decided UNDER the lock from `run_gen` via the injected
  `current_generation` — never from a bool the caller snapshotted before the
  lock-acquisition await. A Stop bumps the run generation synchronously, so
  reading it the instant we hold the lock observes a Stop that landed during
  lock acquisition (not a stale snapshot). When the current generation no
  longer matches `run_gen` (Stop bumped the gen), we must not promote / clear
  / release _starting — the newer owner (Stop, or the continuation it
  scheduled) is responsible. Returns `STALE_NO_ACTION`.

  Bounding: the lock acquisition is wrapped in
  `asyncio.timeout(TERMINAL_LOCK_TIMEOUT_SECS)`. A lock-acquisition timeout
  (another task holds the lock past the bound) OR a failed strict ack
  (PromotePending / ClearRunStatus didn't land, timed out, or hit a
  malformed pending entry) raises out of this function; the caller maps that
  to `FAILED_LEAVE_MARKER`, leaving the marker set for reconciliation
  rather than scheduling a continuation / clearing on a lost write.

  `discard_starting`, `forget_chat`, and `clear_run_status_strict` are
  injected so this module stays free of an import cycle back into chat.py /
  runner_registry. Caller (chat.py:_complete_turn) keeps responsibility for
  the post-lock `_schedule_continuation` call — this function does NOT
  schedule continuations or call back into `run_chat`.
  """
  async with asyncio.timeout(TERMINAL_LOCK_TIMEOUT_SECS):
    async with get_lock(chat_id):
      # Ownership is decided HERE, under the lock, as the first statement after
      # acquiring it — never from a bool computed before the await. A Stop bumps
      # the run generation synchronously, so reading it the instant we hold the
      # lock makes "do we still own this turn?" atomic with the promote/clear
      # below, closing the window where a Stop landing between the caller's
      # check and this lock would let us promote or clear a superseded turn.
      # (The caller's pre-finalize gate is a separate, earlier decision: whether
      # to finalize the assistant message at all.)
      we_own_gen = run_gen is None or current_generation(chat_id) == run_gen
      if not we_own_gen:
        return None, [], None, TerminalDisposition.STALE_NO_ACTION
      next_messages, first_pending, next_session_id = (
        await promote_pending_messages_locked(db, chat_id, run_token)
      )
      if first_pending is None:
        # Clear-before-forget, all under this one lock: clear the durable
        # marker (strict — a failed ack raises and the caller leaves the
        # marker for reconciliation), THEN release _starting, THEN forget.
        # Clearing before releasing _starting closes the race where a racing
        # new StartTurn's marker (set after we released _starting) would be
        # erased by a clear running outside the lock. The clear is identity-
        # keyed on the finishing run's token, so even a StartTurn that lands
        # mid-drain (no generation bump → we_own_gen still true) keeps its
        # marker — the actor no-ops a clear that names the old owner.
        await clear_run_status_strict(chat_id, ending_run_token)
        discard_starting(chat_id)
        forget_chat(chat_id)
        return (
          None, next_messages, next_session_id,
          TerminalDisposition.EMPTY_TERMINAL_CLEARED,
        )
      return (
        first_pending, next_messages, next_session_id,
        TerminalDisposition.CONTINUATION_PROMOTED,
      )
