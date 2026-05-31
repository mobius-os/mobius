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
turn-end drain makes: "promote the head AND, if nothing was promoted,
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
promote the head of the queue (via the actor), and if the queue was
empty release the `_starting` claim and forget the chat — all under one
lock acquisition. It does NOT call back into `run_chat`; the caller
(chat.py:_run_chat_impl) schedules the continuation AFTER the lock
releases.
"""

from __future__ import annotations

import asyncio
import weakref

from sqlalchemy.orm import Session

from app import schemas


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
  builds the next-turn history, moves the queue head into the transcript,
  sets the run marker, and commits, all under `(chat_id, run_token)`. The
  ack is awaited (commit-before-ack) so the caller sees the promote land
  before it schedules the continuation. The asyncio queue lock still
  serializes this critical section's _starting handoff against a racing
  POST; the actor NEVER acquires that lock, and awaiting its ack while
  holding the lock is safe (the actor runs on its own thread).

  `db` is unused now (the actor owns the write through its own session)
  but kept in the signature so the two callers' shape is unchanged.

  Returns (next_messages, first_pending, session_id) on success.
  Returns ([], None, session_id) when the pending queue is empty or when
  the actor left the queue intact (malformed transcript entry). Raises if
  the actor ack fails (missing row / dropped commit) so the turn-end
  caller surfaces a transport error rather than promoting a write that
  never landed.
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
    # Empty queue OR a malformed head left the queue intact for retry —
    # the actor logged and returned promoted=None with an empty history.
    return [], None, result["session_id"]
  return result["history"], promoted, result["session_id"]


async def promote_pending_messages(
  db: Session,
  chat_id: str,
  run_token: str,
) -> tuple[list[schemas.ChatMessage], dict | None, str | None]:
  """Atomically promotes the head of the pending queue into the
  transcript via the writer actor.

  Held under the per-chat queue lock so the _starting handoff doesn't
  race append (POST /messages) or cancel (DELETE /pending/{ts}); the
  JSON RMW itself is serialized by the actor. `run_token` is the
  promoted turn's persistence run identity (the scheduler allocates it
  once and threads it into both this promote and the continuation's
  run_chat, so the marker PromotePending sets matches the runner's
  token).

  This function does NOT claim _starting — the caller ensures exclusive
  promotion (mark_starting before call in the stale-pending path, or by
  being the only finally block for a given run in the turn-end path).
  """
  if not chat_id:
    return [], None, None
  async with get_lock(chat_id):
    return await promote_pending_messages_locked(db, chat_id, run_token)


async def drain_and_release(
  db: Session,
  chat_id: str,
  we_own_gen: bool,
  run_token: str,
  *,
  discard_starting,
  forget_chat,
) -> tuple[dict | None, list, str | None]:
  """End-of-turn queue drain. Returns (next_user, next_messages,
  next_session_id) for the caller to publish + schedule.

  Under the per-chat queue lock:
    - Promotes the head of pending_messages (if any) via the actor's
      `PromotePending` (keyed on `run_token`, the continuation's token).
    - If nothing to promote AND we_own_gen, releases _starting so
      any subsequent POST sees is_chat_running=False and starts a
      fresh run, then forgets the chat (drops the per-chat
      generation counter so long-running containers don't
      accumulate one entry per chat-ever-touched).

  Doing this in a single locked critical section closes the race
  between the run_chat finally and a POST that arrives in the window
  after the subprocess exits but before _starting is released. Both
  ends serialize on the same lock; the JSON RMW itself is serialized by
  the actor. Whichever side wins the lock, the message is either
  promoted here or POST takes the start path.

  When we_own_gen is False (Stop bumped the gen), we must not
  promote or release _starting — the newer owner (Stop, or the
  continuation it scheduled) is responsible for those.

  `discard_starting` and `forget_chat` are injected so this module
  stays free of an import-cycle back into chat.py / runner_registry.
  Caller (chat.py:_run_chat_impl) keeps responsibility for the
  post-lock `_schedule_continuation` call — this function does NOT
  schedule continuations or call back into `run_chat`. A failed actor
  ack (promote write didn't land) propagates so the caller leaves the
  run marker set for reconciliation rather than scheduling a
  continuation on a lost write.
  """
  if not we_own_gen:
    return None, [], None
  async with get_lock(chat_id):
    next_messages, first_pending, next_session_id = (
      await promote_pending_messages_locked(db, chat_id, run_token)
    )
    if first_pending is None:
      discard_starting(chat_id)
      forget_chat(chat_id)
    return first_pending, next_messages, next_session_id
