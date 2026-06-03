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
The activation milestone has landed: this actor IS the live chat-persistence
path — the runners and routes (`get_writer()` in the C2 write paths) submit
every transcript write through it.

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

import asyncio
import enum
import logging
import queue
import secrets
import sys
import threading
import time
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import schemas
from app.events import build_assistant_message, finalize_blocks, question_block_key

# Child of `moebius.chat` (NOT a sibling `moebius.chat_writer`) so the actor's
# records PROPAGATE to the RotatingFileHandler that `chat._get_logger` attaches
# to `moebius.chat` — the same `chat.log` file `/api/debug/logs` serves. A
# persistence failure during Finalize/PersistTranscript/AnswerQuestion is
# exactly the failure the redesign's failure-semantics route here, so it has to
# be operator-visible; under the old `moebius.chat_writer` name (a sibling) the
# records landed on no handler and the post-mortem was blind. No double-logging:
# `moebius.chat` holds the only handler, this child adds none, and the root
# logger has none — so a propagated record hits exactly one handler. The parent
# handler is built lazily by `chat._get_logger`, which the lifespan's
# `reconcile_interrupted_chats` always calls BEFORE `start_writer()`, so the
# handler exists by the time the writer thread can log.
log = logging.getLogger("moebius.chat.writer")


# Bounded wait for a strict (commit-before-ack) writer-actor command.
# Every must-persist caller (QuestionCommit / Finalize / AnswerQuestion /
# StartTurn / PromotePending / ...) awaits the ack before taking the next
# step — broadcasting a card, promoting the queue, resolving a future. The
# timeout is defense-in-depth: the actor's own `db.commit()` is bounded by
# SQLite's 5s busy_timeout, so a healthy-but-contended write completes well
# inside this bound; exceeding it means the writer thread is wedged, which
# the failure-semantics paths treat as persistence-unavailable (transport
# error / 503) rather than hanging the turn forever.
ACK_TIMEOUT_SECS = 30.0


async def await_ack(ack: Future, *, timeout: float | None = None):
  """Await a writer-actor ack on the loop, bounded by `timeout`.

  Wraps the actor's `concurrent.futures.Future` with `asyncio.wrap_future`
  so the event loop stays free while the writer thread commits, then awaits
  it under a timeout. Re-raises the actor's exception on a failed write (the
  caller maps it to its own failure-semantics response — deny / 503 /
  transport error) and raises `asyncio.TimeoutError` when the actor never
  acked in time. The single seam every strict path uses, so the timeout
  policy lives in one place; lives here (not chat.py) so chat_queue can use
  it without importing back into chat.py.

  When `timeout` is None (the default for every production caller) the bound
  is read from the MODULE-LEVEL `ACK_TIMEOUT_SECS` at call time — not bound
  into the signature default — so a test can `monkeypatch.setattr(
  chat_writer, "ACK_TIMEOUT_SECS", small)` and have the seam take effect for
  every strict path without touching call sites.
  """
  if timeout is None:
    timeout = ACK_TIMEOUT_SECS
  return await asyncio.wait_for(asyncio.wrap_future(ack), timeout=timeout)


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
class QuestionCommit(_Command):
  """Save-before-broadcast write for an AskUserQuestion card.

  A question is a protocol barrier: its `question_id` must be persisted
  before the SSE card is shown, or a fast Submit races the DB write and
  the answer is lost.  Distinct from `PersistTranscript` (not just a
  flag) so the runner can `await` the commit ack and only then broadcast.
  Never coalesces; commits the full assistant-message snapshot via
  `update_last_assistant_message` and RAISES (fails its ack) if the
  commit didn't land, so the caller does NOT broadcast the card.
  """

  chat_id: str = ""
  run_token: str = ""
  snapshot: dict = field(default_factory=dict)


@dataclass
class AnswerQuestion(_Command):
  """Identity-keyed answer write.  Never coalesces.

  Re-reads the Chat row fresh, applies the answers to the question block
  matched by `question_id` (or the latest question when absent), and
  commits.  Raises (fails its ack) when no matching block was found or
  the commit didn't land, so the answer route returns 503 and keeps the
  pending question registered for retry rather than resolving the future
  with answers that never persisted.
  """

  chat_id: str = ""
  run_token: str = ""
  question_id: str = ""
  answers: dict = field(default_factory=dict)


# -- queue + turn commands (the JSON-blob RMW the actor owns) ------------
# These own the read-modify-write of the chat's `messages` /
# `pending_messages` blobs: initial-send (`StartTurn`, from
# routes/chats_stream.py), append (`AppendPending`), cancel
# (`CancelPending`), promote (`PromotePending`, from chat_queue.py), and
# clear / run markers (`ClearPending`, from chat.py).  The routes/queue
# submit these instead of mutating the row directly, so every mutation is
# serialized on the actor thread.  Each is must-persist (commit-before-ack,
# non-coalescing) and fences any pending coalescible snapshot for its
# (chat_id, run_token) key on submit.


@dataclass
class StartTurn(_Command):
  """Initial send: append the user message, set title/provider, mark run.

  Replicates the fresh-start branch of `send_message`: appends `user_msg`
  to `chat.messages`, sets the chat title from `title_source` on the
  first message, sets `provider` when the chat had no messages, stamps
  `updated_at`, and sets the durable run marker — all in one commit.
  Returns `{"history", "session_id", "provider"}` for the caller to spawn
  the runner; `history` is a list of `schemas.ChatMessage` (run_chat reads
  `messages[-1].content`), built exactly as the production initial-send path.
  """

  chat_id: str = ""
  run_token: str = ""
  user_msg: dict = field(default_factory=dict)
  title_source: str = ""
  default_provider: str = "claude"


@dataclass
class AppendPending(_Command):
  """Queue a send behind an active turn (or stale pending).

  Replicates `_append_to_pending`: optionally applies `answers` to the
  last question block, bumps the new message's `ts` so it is unique
  within the queue + transcript, appends it to `pending_messages`, stamps
  `updated_at`, and commits.  Returns `{"stored", "pending"}` — the stored
  message (with its final ts) and the resulting queue.
  """

  chat_id: str = ""
  run_token: str = ""
  user_msg: dict = field(default_factory=dict)
  answers: dict | None = None
  question_id: str | None = None


@dataclass
class AppendSteeredUserMessage(_Command):
  """Insert a mid-turn steered user message into the live transcript.

  Used by the Codex steer path (routes/chats_stream.py): when a send
  lands while a Codex turn is streaming and `steer_into_active_turn`
  accepts it, the user message belongs in the TRANSCRIPT, not the
  pending queue — the live turn already saw the text, so it is part of
  this turn rather than a queued follow-up.

  Placement keeps the streaming invariant intact: the message is
  inserted just BEFORE the trailing assistant partial (when one exists)
  so `chat.messages[-1]` stays the in-progress assistant message that
  the runner's `update_last_assistant_message` / `Finalize` snapshots
  target. Visually that renders the steered user row above the still-
  streaming assistant block, which is the frontend contract. The `ts`
  is made unique against both the transcript and the pending queue so
  it can't collide with a sibling message's React key. Returns
  `{"stored"}` — the stored message with its final ts.
  """

  chat_id: str = ""
  run_token: str = ""
  user_msg: dict = field(default_factory=dict)


@dataclass
class PromotePending(_Command):
  """Move the pending-queue head into the transcript and mark the run.

  Replicates `promote_pending_messages_locked`: refreshes the row, builds
  the next-turn message history (a list of `schemas.ChatMessage`) BEFORE
  committing (so a malformed entry can't silently consume a turn — building
  the validated schema surfaces it), moves the head of `pending_messages`
  into `messages`, sets the durable run marker, stamps `updated_at`, and
  commits.  Returns `{"history", "promoted", "session_id"}`; `promoted`
  is None (queue unchanged) only when there was nothing to promote. A
  malformed head that can't build a valid history instead RAISES
  `_PersistFailed` — the turn-end drain maps that to FAILED_LEAVE_MARKER
  (leave the marker for reconciliation) rather than confusing it with an
  empty queue and clearing the marker on stranded work.
  """

  chat_id: str = ""
  run_token: str = ""


@dataclass
class CancelPending(_Command):
  """Remove a queued (not-yet-started) message by its `ts`.

  Replicates the `DELETE /pending/{ts}` route: drops the entry whose `ts`
  matches, stamps `updated_at` only when something changed, commits.
  Returns `{"pending"}` — the remaining queue.
  """

  chat_id: str = ""
  run_token: str = ""
  ts: int = 0


@dataclass
class ClearPending(_Command):
  """Empty the pending queue (Stop / terminal-setup-error paths).

  Clears `pending_messages` and commits only when the queue was
  non-empty (an empty queue is a no-op, so we skip the commit).  Returns
  `{"cleared"}` — the count removed.
  """

  chat_id: str = ""
  run_token: str = ""


@dataclass
class SetGoal(_Command):
  """Store the active autonomous goal in Chat.agent_settings_json.

  The goal rides inside the existing JSON settings escape hatch so the
  feature needs no schema migration. This command owns the read-modify-write
  because runtime chat state must stay serialized through the writer actor.
  """

  chat_id: str = ""
  run_token: str = ""
  goal: dict = field(default_factory=dict)


@dataclass
class ClearGoal(_Command):
  """Remove the active autonomous goal from Chat.agent_settings_json."""

  chat_id: str = ""
  run_token: str = ""


@dataclass
class IncrementGoalTurn(_Command):
  """Persist one autonomous continuation attempt for the active goal."""

  chat_id: str = ""
  run_token: str = ""
  reason: str | None = None


@dataclass
class RecordGoalTokens(_Command):
  """Persist token usage for the active goal."""

  chat_id: str = ""
  run_token: str = ""
  token_delta: int = 0


@dataclass
class ResetGoalProgress(_Command):
  """Reset per-run goal counters when a stored goal resumes."""

  chat_id: str = ""
  run_token: str = ""


@dataclass
class PauseGoal(_Command):
  """Mark the active goal paused without clearing it."""

  chat_id: str = ""
  run_token: str = ""


@dataclass
class ResumeGoal(_Command):
  """Mark the active goal active and reset per-run counters."""

  chat_id: str = ""
  run_token: str = ""


@dataclass
class CompleteGoal(_Command):
  """Record the achieved goal, then clear active goal state."""

  chat_id: str = ""
  run_token: str = ""
  turns: int = 0
  reason: str = ""
  token_spend: int = 0


@dataclass
class PersistCompaction(_Command):
  """Append a compaction-summary block to the transcript as its own message.

  Used by `POST /api/chats/{id}/compact` (feature 091's provider-switch
  groundwork): a cross-provider switch loses the session, so a one-shot
  summarize turn produces a portable plain-text briefing. That briefing is
  stored here as a NEW assistant message — distinct from the streaming
  snapshot path, which REPLACES the in-progress assistant message — so it
  never clobbers a live turn's transcript and renders as its own block.

  The stored message carries `kind="compaction"` (so the frontend can render
  a recognizable, collapsible "compacted chat" block) and `content` set to
  the briefing text (so any plain renderer still shows it). Returns
  `{"stored"}` — the message as appended, with its final ts.
  """

  chat_id: str = ""
  run_token: str = ""
  summary: str = ""


@dataclass
class ReplaceTranscript(_Command):
  """Replace the whole `messages` blob (PUT /api/chats/{id}).

  Backs `update_chat`'s transcript branch: sets `messages` to `messages`
  (when not None), `title` (when supplied), stamps `updated_at`, and
  commits.  Routes through the actor so the replace serializes with
  streaming snapshots for the same chat.
  """

  chat_id: str = ""
  run_token: str = ""
  messages: list | None = None
  title: str | None = None


@dataclass
class ClearRunStatus(_Command):
  """Clear the durable run marker once a turn has ended.

  Replicates `_clear_run_status`: clears `run_status` / `run_started_at`
  when either is set, commits.  Returns None.
  """

  chat_id: str = ""
  run_token: str = ""


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


# Every must-persist command: non-coalescing, takes the fence path in
# `submit` (invalidate any pending coalescible snapshot for the key, then
# enqueue in FIFO order) and the GC path in the consumer's `finally`
# (reclaim the key's fence epoch once the unit of work for that
# (chat_id, run_token) is done).  PersistTranscript is the ONLY
# coalescible command and is deliberately absent.
_FENCE_COMMANDS = (
  Finalize,
  PersistError,
  QuestionCommit,
  AnswerQuestion,
  StartTurn,
  AppendPending,
  AppendSteeredUserMessage,
  PersistCompaction,
  PromotePending,
  CancelPending,
  ClearPending,
  SetGoal,
  ClearGoal,
  IncrementGoalTurn,
  RecordGoalTokens,
  ResetGoalProgress,
  PauseGoal,
  ResumeGoal,
  CompleteGoal,
  ReplaceTranscript,
  ClearRunStatus,
)


def _needs_broad_chat_fence(cmd: _Command) -> bool:
  """True when a must-persist command must fence ALL of the chat's snapshots.

  The exact-key fence (`_invalidate_pending`) only invalidates the command's
  own `(chat_id, run_token)` snapshot.  Two cases can be clobbered by a
  snapshot pending under a DIFFERENT run_token and so need a broad-by-chat
  fence (`_invalidate_chat`):

  - `ReplaceTranscript` replaces the WHOLE `messages` blob, so ANY in-flight
    snapshot for the chat (any run_token) could overwrite the replacement.
  - A legacy `/question-answers` `AnswerQuestion` has no live run_token (the
    tokenless path), so its exact-key fence reaches nothing — a snapshot under
    the live streaming token would survive and clobber the answer.

  - An `AppendSteeredUserMessage` inserts the steered user turn mid-run with no
    run_token; the interrupted run's still-pending snapshot (its own run_token)
    would otherwise commit afterward and overwrite the insert. It must fence the
    chat broadly so the pre-steer snapshot can't clobber it; the run's
    post-steer snapshots get the new generation and still land.

  A token-bearing `AnswerQuestion` (the live path) keeps the precise key fence.
  """
  if isinstance(cmd, ReplaceTranscript):
    return True
  if isinstance(cmd, AppendSteeredUserMessage):
    return True
  if isinstance(cmd, AnswerQuestion):
    return not cmd.run_token
  return False


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
    # The single session the actor owns, opened on its thread in `_run`.
    # Held as an attribute (not a `_run` local) so the DB-error branch can
    # swap in a fresh session via `_recreate_session` mid-loop.  None
    # before startup and after a recreate-failure escalates to fatal.
    self._db = None
    # Set by `_run` once the DB session is open, so readiness can tell "thread
    # alive" apart from "thread alive AND able to persist". `start()` publishes
    # the actor BEFORE `_run` opens the session, so a live thread whose
    # `_session_factory()` hasn't returned yet (or hangs) must not report ready.
    # Stays clear if session-open fails (the thread goes fatal instead).
    self._session_ready = threading.Event()
    # Per-chat owner of the durable run marker: the run_token whose StartTurn /
    # PromotePending most recently set `run_status='running'`. `ClearRunStatus`
    # is an identity-keyed compare-and-clear against this — a clear that names a
    # run_token clears ONLY if that token still owns the marker, so a dying
    # run's stale clear can't wipe the marker a fresh turn just set (the
    # markerless-run race). Touched only on the consumer thread, so no lock; a
    # tokenless clear (run_token="") stays unconditional for paths that already
    # know they own the marker.
    self._run_token_owner: dict[str, str] = {}
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
    # lock is released (see the deadlock note below).  A broad-chat fence can
    # drop several snapshots at once, so this is a list rather than a scalar.
    reject_exc: BaseException | None = None
    dropped_acks: list[Future] = []
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
      elif isinstance(cmd, _FENCE_COMMANDS):
        # Must-persist, non-coalescing.  A `ReplaceTranscript` (whole-blob
        # replace) or a tokenless legacy `AnswerQuestion` (no live run_token)
        # can be clobbered by a snapshot pending under ANY other run_token for
        # the chat, which the exact-key fence cannot reach — so they fence
        # BROADLY by chat_id.  Every other must-persist command fences only
        # its own (chat_id, run_token) key.  Then enqueue in FIFO order.
        if _needs_broad_chat_fence(cmd):
          dropped_acks = self._invalidate_chat(cmd.chat_id)
        else:
          dropped = self._invalidate_pending(cmd.chat_id, cmd.run_token)
          if dropped is not None:
            dropped_acks = [dropped]
        self._q.put(cmd)
      elif isinstance(cmd, PersistTranscript):
        dropped = self._enqueue_snapshot(cmd)
        if dropped is not None:
          dropped_acks = [dropped]
      else:
        self._q.put(cmd)
    # Lock released — now safe to resolve acks even if a done-callback
    # re-enters the actor.
    if reject_exc is not None:
      _safe_set_exception(cmd.ack, reject_exc)
      return cmd.ack
    for dropped in dropped_acks:
      _safe_set_result(dropped, None)
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

  def _invalidate_chat(self, chat_id: str) -> list[Future]:
    """Broad fence: drop EVERY coalescible snapshot for `chat_id`, all tokens.

    A `ReplaceTranscript` (replaces the whole `messages` blob) or a tokenless
    legacy `AnswerQuestion` (no live run_token) can be clobbered by a snapshot
    pending under ANY run_token for the chat — the exact-key `_invalidate_pending`
    only reaches one key, so a snapshot under a different (e.g. the streaming)
    token would survive and overwrite the write.  This fences every key whose
    chat_id matches: it bumps each key's generation (so an in-flight
    `_SnapshotReady` marker stamped with the OLD generation becomes a no-op in
    `_take_pending`, even if a new snapshot for the key arrives afterward) and
    pops every pending snapshot + outstanding marker for the chat.

    Returns the list of dropped snapshot acks (each to be resolved with `None`
    by the caller AFTER releasing `submit`'s `_fatal_lock` — the same deferred
    contract as `_invalidate_pending`, collecting under the lock and resolving
    outside it so a synchronous done-callback can't deadlock on a producer lock).
    """
    dropped: list[Future] = []
    with self._pending_lock:
      # Snapshot the matching keys first: bumping generation reads the union
      # of every key the chat currently touches across the three maps. The
      # `_generation` term is load-bearing, not redundant: between turns a
      # key lives ONLY in `_generation` (no pending snapshot, no outstanding
      # marker — `_gc_generation` drops it only once it is quiescent in both
      # other maps), so a broad fence must bump it too, or a late stale
      # marker for that key could slip past after this fence ran.
      matching = {
        key
        for key in (
          set(self._pending) | set(self._outstanding) | set(self._generation)
        )
        if key[0] == chat_id
      }
      for key in matching:
        self._generation[key] = self._generation.get(key, 0) + 1
        stale = self._pending.pop(key, None)
        self._outstanding.discard(key)
        if stale is not None:
          dropped.append(stale.ack)
    return dropped

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
    fatal_exc = None
    with self._fatal_lock:
      if self._fatal:
        # Already dead: every queued ack was failed by `_go_fatal`. Capture
        # the failure and resolve the ack OUTSIDE the lock below — the module
        # invariant forbids resolving an ack while holding a producer lock (a
        # synchronous done-callback could re-enter `submit`/`stop` and
        # deadlock on the lock), and `_go_fatal` follows the same discipline.
        fatal_exc = RuntimeError("chat writer is in a fatal state")
      elif self._stopping:
        # A prior stop() already enqueued the one DrainAndStop; don't add a
        # second the consumer will never reach.  Just join below.
        pass
      else:
        self._stopping = True
        self._q.put(drain)
        enqueued = True
    if fatal_exc is not None:
      _safe_set_exception(drain.ack, fatal_exc)
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
      if self._thread.is_alive():
        # The bounded join expired but the thread is still running — a
        # command's DB work outlasted the timeout (a wedged commit, a
        # stuck session).  stop() still returns (callers must not block
        # teardown), but a surviving writer thread is an operator-visible
        # anomaly, so log it loudly via `moebius.chat.writer` (propagates
        # to chat.log).
        log.warning(
          "chat writer thread still alive after %.1fs join timeout "
          "(a command's DB work outlasted the join); stop() returning anyway",
          timeout,
        )

  # -- consumer (the writer thread) --------------------------------------
  def _run(self) -> None:
    try:
      self._db = self._session_factory()
      # Prove the session can actually execute before advertising ready, so
      # "ready" means "provably usable" rather than "the factory returned an
      # object". A bare SELECT 1 forces the lazy connection to open and reads
      # nothing, so it raises on a broken session (missing/locked DB, corrupt
      # WAL) but has no side effect on a healthy one. If the factory raised, or
      # this probe raises, `.set()` is skipped and the except below marks the
      # writer fatal — it never reports ready on a writer that can't persist.
      self._db.execute(text("SELECT 1"))
      self._session_ready.set()
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
        # Cheap defense against an abandoned ack on a run-marker-SETTING
        # command: a `StartTurn` / `PromotePending` whose caller already gave
        # up (its `asyncio.wrap_future` ack was cancelled, or is otherwise
        # done) must NOT mutate state — the awaiter has moved on, so
        # committing now would promote/start a turn nobody will run AND set
        # the durable run marker for it, stranding a queued send (the FIX-1
        # hazard: a timed-out promote that still commits after the
        # continuation flow abandoned it).
        #
        # Scope is deliberately narrow: only these two. `AnswerQuestion` must
        # persist EVEN when its ack is abandoned (the Stop-races-answer
        # contract — the route re-claims by identity and returns 410, but the
        # answer is durable regardless; see chats_stream + the
        # stop-races-answer tests). `Finalize`/`QuestionCommit`/`PersistError`
        # are terminal transcript writes that are beneficial to land even if
        # the awaiter went away. `Clear*`/`Cancel*`/`Replace*` are idempotent
        # convergence writes. So the skip targets exactly the commands whose
        # post-abandon commit strands a turn. The `finally` still GCs the
        # key's fence epoch for the skipped command.
        if (
          isinstance(cmd, (StartTurn, PromotePending))
          and cmd.ack is not None
          and cmd.ack.done()
        ):
          log.warning(
            "chat writer skipping %s with abandoned ack chat_id=%s",
            type(cmd).__name__, cmd.chat_id,
          )
          self._gc_generation(cmd.chat_id, cmd.run_token)
          continue
        try:
          # `expire_all` before each command so a row dirtied by an
          # ALLOWED direct writer (e.g. session_id persistence, still on
          # the loop at C2) is re-read fresh — the actor's long-lived
          # session would otherwise serve a stale identity-map copy and
          # clobber that write.
          self._db.expire_all()
          result = self._dispatch(self._db, cmd)
          _safe_set_result(cmd.ack, result)
        except SQLAlchemyError:
          # A DB-level failure (broken session / commit error the helpers
          # didn't swallow) can poison the session for every later command.
          # Roll back, fail this ack, and recreate the session so the actor
          # keeps serving.  If recreation itself fails there is no session
          # to write through — escalate to the thread-fatal path so callers
          # see a raised ack rather than a hang.
          # Drop readiness the instant the session is known-poisoned — before
          # the ack/recreate work below — so a concurrent `/api/ready` in this
          # window can't read the still-set event and report ready against a
          # dead session. `_recreate_session` re-sets it once a fresh session
          # opens (or leaves it clear and goes fatal if the factory fails).
          self._session_ready.clear()
          log.exception(
            "chat writer DB error on %s; recreating session",
            type(cmd).__name__,
          )
          exc = sys.exc_info()[1]
          _safe_set_exception(cmd.ack, exc)
          _safe_set_exception(self._inflight_ack, exc)
          self._inflight_ack = None
          self._recreate_session()  # raises to the outer handler on failure
        except Exception:
          # A non-DB command failure must not kill the actor: log, roll
          # back the poisoned transaction, fail this command's ack, keep
          # serving.  For a coalesced `_SnapshotReady`, `cmd.ack` is the
          # marker's None ack — the ack a caller awaits is the popped
          # snapshot's (`_inflight_ack`), so fail that too.
          log.exception(
            "chat writer command failed: %s", type(cmd).__name__
          )
          try:
            self._db.rollback()
          except Exception:
            log.exception("chat writer rollback failed")
          exc = sys.exc_info()[1]
          _safe_set_exception(cmd.ack, exc)
          _safe_set_exception(self._inflight_ack, exc)
          self._inflight_ack = None
        finally:
          # A must-persist command ends a unit of work for its key; reclaim
          # the key's fence epoch whether the commit succeeded or raised (the
          # run_token won't be reused).  GC is a no-op unless the key is fully
          # quiescent, so a post-fence snapshot enqueued for the same key
          # keeps its epoch.
          if isinstance(cmd, _FENCE_COMMANDS):
            self._gc_generation(cmd.chat_id, cmd.run_token)
    except BaseException:
      # Thread-fatal (a BaseException the per-command handler didn't
      # catch — e.g. the queue broke, or session recreation failed): fail
      # every outstanding and future ack so no awaiter hangs forever.
      # Also fail the in-flight command's ack, which the inner handler
      # never reached, AND the popped snapshot's ack (`_inflight_ack`) —
      # which `_go_fatal` can't reach because it was already removed from
      # `_pending`.
      log.exception("chat writer thread died")
      exc = sys.exc_info()[1]
      _safe_set_exception(cmd.ack if cmd is not None else None, exc)
      _safe_set_exception(self._inflight_ack, exc)
      self._inflight_ack = None
      self._go_fatal()
    finally:
      try:
        if self._db is not None:
          self._db.close()
      except Exception:
        log.exception("chat writer session close failed")

  def _recreate_session(self) -> None:
    """Roll back + close the poisoned session and open a fresh one.

    Called from the consumer's DB-error branch.  Any failure here (the
    factory raising, or the old session refusing to close) is re-raised so
    the outer `except BaseException` runs `_go_fatal` — a writer with no
    usable session must fail callers' acks, not silently spin.
    """
    old = self._db
    # The session is going away — readiness must report not-ready for the WHOLE
    # recreate window. Cleared before we drop `_db` and re-set only once the
    # replacement actually opens, so a hung/raising factory below keeps the
    # writer looking not-ready (a raise propagates to `_go_fatal`) instead of
    # advertising a session it doesn't have.
    self._session_ready.clear()
    self._db = None
    try:
      old.rollback()
    except Exception:
      log.exception("chat writer rollback failed during session recreate")
    try:
      old.close()
    except Exception:
      log.exception("chat writer close failed during session recreate")
    self._db = self._session_factory()
    # Same readiness contract as `_run`: prove the replacement session executes
    # before re-advertising ready. A raising probe propagates to the consumer's
    # outer `except BaseException` → `_go_fatal`, leaving readiness clear rather
    # than advertising a session that can't persist.
    self._db.execute(text("SELECT 1"))
    self._session_ready.set()

  def _dispatch(self, db, cmd: _Command):
    """Apply one command's persistence effect against the actor's session.

    Each command maps to a real DB mutation (the dispatch table in
    ~/mobius-activation-design-2026-05-31.md).  Must-persist commands
    commit before their ack resolves and RAISE to fail the ack when the
    commit didn't land; `PersistTranscript`/`PersistError` are
    fire-and-forget (a later snapshot/Finalize repairs a dropped write).

    The mechanics tests in `test_chat_writer.py` drive the actor with a
    `_RecordingSession` stub (no `.query`) to assert ordering / coalescing
    / fencing without a DB; the snapshot commands detect that stub via its
    `record_commit`/`commit_test` hooks and route there instead of the
    real helpers.  The contention tests in `test_chat_writer_contention`
    drive a real `SessionLocal`, exercising the real dispatch below.
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
      result = self._persist_message(db, pending.chat_id, pending.snapshot)
      _safe_set_result(pending.ack, result)
      self._inflight_ack = None
      # The committed snapshot was popped from `_pending`/`_outstanding`; if no
      # newer snapshot re-added the key it is now dead — reclaim its generation
      # epoch so the map can't grow unbounded.
      self._gc_generation(cmd.chat_id, cmd.run_token)
      return None
    if isinstance(cmd, _TestPersist):
      # Test-only non-coalescing per-command persist (raw FIFO ordering).
      return self._commit_snapshot(db, {"_test_payload": cmd.payload})
    if isinstance(cmd, PersistError):
      # Fire-and-forget like PersistTranscript: an unwritten error state is
      # repaired by a later Finalize/snapshot; never raises.
      return self._persist_message(db, cmd.chat_id, cmd.snapshot)
    if isinstance(cmd, PersistTranscript):
      # Defensive: a directly-enqueued PersistTranscript (no current path
      # does this) still commits its own snapshot.
      return self._persist_message(db, cmd.chat_id, cmd.snapshot)
    if isinstance(cmd, QuestionCommit):
      # Save-before-broadcast: the commit MUST land before the ack resolves
      # so the runner only broadcasts the card after the question_id
      # persisted.  Anything but APPLIED (a NOOP on a missing row / empty
      # transcript, or a DROPPED commit) raises so the caller does NOT
      # broadcast a card whose question was never written.
      outcome = self._persist_message_required(db, cmd.chat_id, cmd.snapshot)
      if outcome is not _WriteOutcome.APPLIED:
        raise _PersistFailed(f"QuestionCommit did not persist ({outcome.value})")
      return True
    if isinstance(cmd, Finalize):
      # Must-persist terminal write: a NOOP (missing row / empty transcript /
      # no blocks) is a silent loss, not a success — raise so the caller does
      # not promote the queue / schedule a continuation on a write that never
      # landed.
      outcome = self._finalize_required(db, cmd.chat_id, cmd.snapshot)
      if outcome is not _WriteOutcome.APPLIED:
        raise _PersistFailed(f"Finalize did not persist ({outcome.value})")
      return True
    if isinstance(cmd, AnswerQuestion):
      return self._answer_question(db, cmd)
    if isinstance(cmd, StartTurn):
      return self._start_turn(db, cmd)
    if isinstance(cmd, AppendPending):
      return self._append_pending(db, cmd)
    if isinstance(cmd, AppendSteeredUserMessage):
      return self._append_steered_user_message(db, cmd)
    if isinstance(cmd, PersistCompaction):
      return self._persist_compaction(db, cmd)
    if isinstance(cmd, PromotePending):
      return self._promote_pending(db, cmd)
    if isinstance(cmd, CancelPending):
      return self._cancel_pending(db, cmd)
    if isinstance(cmd, ClearPending):
      return self._clear_pending(db, cmd)
    if isinstance(cmd, SetGoal):
      return self._set_goal(db, cmd)
    if isinstance(cmd, ClearGoal):
      return self._clear_goal(db, cmd)
    if isinstance(cmd, IncrementGoalTurn):
      return self._increment_goal_turn(db, cmd)
    if isinstance(cmd, RecordGoalTokens):
      return self._record_goal_tokens(db, cmd)
    if isinstance(cmd, ResetGoalProgress):
      return self._reset_goal_progress(db, cmd)
    if isinstance(cmd, PauseGoal):
      return self._pause_goal(db, cmd)
    if isinstance(cmd, ResumeGoal):
      return self._resume_goal(db, cmd)
    if isinstance(cmd, CompleteGoal):
      return self._complete_goal(db, cmd)
    if isinstance(cmd, ReplaceTranscript):
      return self._replace_transcript(db, cmd)
    if isinstance(cmd, ClearRunStatus):
      return self._clear_run_status(db, cmd)
    raise NotImplementedError(type(cmd).__name__)

  # -- real DB dispatch (one method per command) -------------------------
  # Each method runs on the actor thread against `db` (the actor's single
  # session), delegating to the module-level persistence helpers below so
  # the RMW logic lives in one place.  `_RecordingSession`-stub detection
  # (`record_commit`) keeps the mechanics tests DB-free.

  def _persist_message(self, db, chat_id: str, snapshot: dict) -> bool:
    """Write `snapshot` as the chat's last assistant message.

    Backs `PersistTranscript`/`PersistError`/`QuestionCommit`/the coalesced
    snapshot path — all of which replace the in-progress assistant message
    with the full current snapshot.
    """
    if hasattr(db, "record_commit"):
      return self._commit_snapshot(db, snapshot)
    return update_last_assistant_message(db, chat_id, snapshot)

  def _persist_message_required(self, db, chat_id: str, snapshot: dict):
    """Must-persist variant of `_persist_message`, returning a `_WriteOutcome`.

    Backs `QuestionCommit`: the dispatch raises unless this is APPLIED, so a
    NOOP (missing row / empty transcript) fails the ack instead of falsely
    succeeding.  On the DB-free recording stub a recorded commit IS the write,
    so it reports APPLIED.
    """
    if hasattr(db, "record_commit"):
      self._commit_snapshot(db, snapshot)
      return _WriteOutcome.APPLIED
    return _apply_last_assistant_message(db, chat_id, snapshot)

  def _finalize_required(self, db, chat_id: str, snapshot: dict):
    """Force-complete tool blocks and write the terminal assistant message.

    Backs `Finalize` and returns a `_WriteOutcome`: the dispatch raises
    unless APPLIED, so a NOOP (missing row / empty transcript / no blocks)
    fails the ack instead of falsely succeeding.  `snapshot["blocks"]` is
    the accumulated block list; `finalize_response_outcome` closes any
    running tool blocks before persisting.  On the recording stub the
    recorded commit IS the terminal write, so it reports APPLIED.
    """
    if hasattr(db, "record_commit"):
      self._commit_snapshot(db, snapshot)
      return _WriteOutcome.APPLIED
    outcome = finalize_response_outcome(
      db, chat_id, snapshot.get("blocks") or []
    )
    if outcome is _WriteOutcome.NOOP:
      # The sink only submits Finalize with non-empty blocks, so a NOOP here
      # means the row had nothing to finalize onto. Distinguish two causes: a
      # STILL-EXISTING chat whose transcript was wiped by a concurrent
      # ReplaceTranscript is benign (nothing to save, not a failure) — promote
      # to APPLIED so the dispatch doesn't raise a spurious "could not be
      # saved". A missing / soft-deleted row stays a hard NOOP (dispatch
      # raises → FAILED_LEAVE_MARKER → reconciliation), because delete owns the
      # row's removal and a stray finalize must not resurrect it.
      from app.models import Chat
      still_exists = db.query(Chat).filter(
        Chat.id == chat_id,
        Chat.deleted_at.is_(None),
      ).first()
      if still_exists is not None:
        return _WriteOutcome.APPLIED
    return outcome

  def _answer_question(self, db, cmd: AnswerQuestion) -> bool:
    """Re-read the chat fresh, merge answers into the question block, commit.

    Raises `_PersistFailed` when no matching block was found or the commit
    dropped, so the answer route returns 503 and the pending question stays
    registered for retry instead of the future resolving on a lost write.
    """
    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("AnswerQuestion: chat not found or deleted")
    applied = apply_answers_to_last_question(
      chat, cmd.answers, cmd.question_id
    )
    if not applied:
      raise _PersistFailed("AnswerQuestion: no matching question block")
    if not _commit_or_rollback(db):
      raise _PersistFailed("AnswerQuestion did not persist")
    return True

  def _start_turn(self, db, cmd: StartTurn) -> dict:
    """Append the initial user message, set title/provider, mark the run.

    Replicates the fresh-start branch of `send_message`: builds the agent
    history, appends `user_msg`, sets the title from `title_source` on the
    first message, sets the provider when the chat had no messages, sets
    the durable run marker, commits.  Returns history/session/provider.
    """
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("StartTurn: chat not found or deleted")
    existing = list(chat.messages or [])
    if not existing:
      chat.provider = cmd.default_provider or "claude"
    # Build the agent history as schemas.ChatMessage objects, exactly as the
    # production initial-send path in routes/chats_stream.py does — run_chat
    # consumes `messages[-1].content` (attribute access), so a raw dict would
    # break.  An initial send starts a fresh chat (no prior transcript) so a
    # malformed entry is not expected here; if one ever appeared, the
    # ValidationError surfaces (the consumer fails the ack) rather than being
    # silently consumed, matching how production validates.
    history = [
      schemas.ChatMessage(
        role=m.get("role", "user"), content=m.get("content", "") or ""
      )
      for m in existing
    ]
    history.append(
      schemas.ChatMessage(
        role=cmd.user_msg.get("role", "user"),
        content=cmd.user_msg.get("content", "") or "",
      )
    )
    existing.append(cmd.user_msg)
    chat.messages = existing
    if len(existing) == 1:
      chat.title = cmd.title_source[:40] or "New chat"
    chat.run_status = "running"
    chat.run_started_at = datetime.now(UTC)
    chat.updated_at = datetime.now(UTC)
    # Durable per-run record (077 Step 3), inserted in the SAME commit as the
    # run_status marker so the two can never diverge. Keyed on this turn's
    # run_token — the one identity the sink and ClearRunStatus also carry.
    # A fresh start claims an idle chat (mark_starting guarantees no live run),
    # so any still-running row is a prior run whose clear was dropped — mark it
    # interrupted before opening this one (at most one run is ever live).
    from app.models import ChatRun
    self._close_running_runs(db, cmd.chat_id, "interrupted")
    db.add(ChatRun(
      id=cmd.run_token, chat_id=cmd.chat_id, status="running",
      provider=chat.provider, started_at=chat.run_started_at,
    ))
    if not _commit_or_rollback(db):
      raise _PersistFailed("StartTurn did not persist")
    # This run_token now owns the marker — a later ClearRunStatus naming a
    # different token (a dying run) must not clear it.
    self._run_token_owner[cmd.chat_id] = cmd.run_token
    return {
      "history": history,
      "session_id": chat.session_id,
      "provider": chat.provider,
    }

  def _append_pending(self, db, cmd: AppendPending) -> dict:
    """Queue `user_msg` behind the active turn; optionally apply answers.

    Replicates `_append_to_pending`: applies `answers` to the last question
    block (when present), bumps the message `ts` so it is unique within the
    queue + transcript, appends to `pending_messages`, commits.  Returns the
    stored message (with its final ts) and the resulting queue.
    """
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("AppendPending: chat not found or deleted")
    apply_answers_to_last_question(chat, cmd.answers, cmd.question_id)
    pending = list(chat.pending_messages or [])
    new_msg = dict(cmd.user_msg)
    _ensure_unique_ts(new_msg, pending + list(chat.messages or []))
    pending.append(new_msg)
    chat.pending_messages = pending
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("AppendPending did not persist")
    return {"stored": new_msg, "pending": pending}

  def _append_steered_user_message(
    self, db, cmd: AppendSteeredUserMessage
  ) -> dict:
    """Insert the steered user message into the live transcript; commit.

    The message lands just before the trailing assistant partial (when
    one exists) so the in-progress assistant message stays last and the
    runner's snapshot / finalize writes keep targeting it. When there is
    no assistant message yet (the turn hasn't streamed any text), the
    message is simply appended. The `ts` is bumped past every message in
    the transcript and the pending queue so it can't collide with a
    sibling's React key.
    """
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed(
        "AppendSteeredUserMessage: chat not found or deleted"
      )
    msgs = list(chat.messages or [])
    new_msg = dict(cmd.user_msg)
    _ensure_unique_ts(new_msg, msgs + list(chat.pending_messages or []))
    if msgs and msgs[-1].get("role") == "assistant":
      msgs.insert(len(msgs) - 1, new_msg)
    else:
      msgs.append(new_msg)
    chat.messages = msgs
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("AppendSteeredUserMessage did not persist")
    return {"stored": new_msg}

  def _persist_compaction(self, db, cmd: PersistCompaction) -> dict:
    """Append the compaction briefing as a new assistant message; commit.

    The block is its OWN message (appended, not a replace) so it can't
    clobber a streaming snapshot, and it carries `kind="compaction"` plus
    a single text block so both the dedicated frontend renderer and any
    plain renderer surface the briefing. The `ts` is set strictly past
    every message in the transcript and the pending queue (via
    `next_message_ts`, so even an empty transcript gets a wall-clock ts)
    so it can't collide with a sibling's React key.
    """
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("PersistCompaction: chat not found or deleted")
    msgs = list(chat.messages or [])
    new_msg = {
      "role": "assistant",
      "kind": "compaction",
      "content": cmd.summary,
      "blocks": [{"type": "text", "content": cmd.summary}],
      "ts": next_message_ts(msgs + list(chat.pending_messages or [])),
    }
    msgs.append(new_msg)
    chat.messages = msgs
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("PersistCompaction did not persist")
    return {"stored": new_msg}

  def _promote_pending(self, db, cmd: PromotePending) -> dict:
    """Move the pending-queue head into the transcript and mark the run.

    Replicates `promote_pending_messages_locked`: builds the next-turn
    history BEFORE committing (a malformed entry can't silently consume a
    turn), moves the head into `messages`, sets the durable run marker,
    commits.  `promoted` is None (queue left intact) only when there was
    nothing to promote; a malformed head that fails schema construction
    instead RAISES `_PersistFailed` (the drain maps that to
    FAILED_LEAVE_MARKER, not an empty-queue clear).
    """
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("PromotePending: chat not found or deleted")
    pending = list(chat.pending_messages or [])
    if not pending:
      return {"history": [], "promoted": None, "session_id": chat.session_id}
    existing = list(chat.messages or [])
    first_pending = pending[0]
    # Build the next-turn history as schemas.ChatMessage objects BEFORE
    # committing, exactly as chat_queue.promote_pending_messages_locked does —
    # run_chat consumes `messages[-1].content` (attribute access).  A
    # malformed transcript entry (e.g. a non-string content) raises here, so
    # the except below leaves the pending queue intact for retry rather than
    # silently consuming the turn — the validation a raw-dict build skipped.
    try:
      history = [
        schemas.ChatMessage(
          role=m.get("role", "user"), content=m.get("content", "") or ""
        )
        for m in existing
      ]
      history.append(
        schemas.ChatMessage(
          role=first_pending.get("role", "user"),
          content=first_pending.get("content", "") or "",
        )
      )
    except Exception as exc:
      # A malformed head can't be turned into a turn. RAISE (not return
      # promoted=None) so the ack fails and the turn-end drain maps it to
      # FAILED_LEAVE_MARKER — the run marker is LEFT set and the queue is
      # left intact for reconciliation / next-POST self-heal. Returning
      # promoted=None here would be indistinguishable from an empty queue,
      # so drain_and_release would clear the marker + forget the chat while
      # the malformed message still sits in the queue (claiming
      # EMPTY_TERMINAL_CLEARED while work remains). No DB mutation has
      # happened yet (chat.messages / pending_messages are reassigned only
      # below, after this try), so nothing is half-written.
      log.exception(
        "promote: next_messages construction failed chat_id=%s — leaving "
        "pending queue intact, failing the ack so the marker is kept",
        cmd.chat_id,
      )
      raise _PersistFailed("PromotePending: malformed queue head") from exc
    chat.messages = existing + [first_pending]
    chat.pending_messages = pending[1:]
    chat.run_status = "running"
    chat.run_started_at = datetime.now(UTC)
    chat.updated_at = datetime.now(UTC)
    # The prior turn finished (the queue had more) and hands off to this
    # continuation. Close its run record completed and open the continuation's,
    # all in the SAME commit as the marker (077 Step 3). A continuation never
    # gets a ClearRunStatus of its own — the marker stays set across the
    # handoff — so this is where the prior run's record is closed.
    from app.models import ChatRun
    self._close_running_runs(
      db, cmd.chat_id, "completed", except_token=cmd.run_token
    )
    db.add(ChatRun(
      id=cmd.run_token, chat_id=cmd.chat_id, status="running",
      provider=chat.provider, started_at=chat.run_started_at,
    ))
    if not _commit_or_rollback(db):
      raise _PersistFailed("PromotePending did not persist")
    # The promoted continuation now owns the marker under its run_token.
    self._run_token_owner[cmd.chat_id] = cmd.run_token
    return {
      "history": history,
      "promoted": first_pending,
      "session_id": chat.session_id,
    }

  def _cancel_pending(self, db, cmd: CancelPending) -> dict:
    """Remove the queued message whose `ts` matches; return the remainder.

    Replicates the `DELETE /pending/{ts}` route: stamps `updated_at` and
    commits only when something was removed.
    """
    from datetime import UTC, datetime

    from app.models import Chat

    chat = db.query(Chat).filter(Chat.id == cmd.chat_id).first()
    if chat is None:
      raise _PersistFailed("CancelPending: chat not found")
    pending = list(chat.pending_messages or [])
    remaining = [m for m in pending if m.get("ts") != cmd.ts]
    if len(remaining) != len(pending):
      chat.pending_messages = remaining
      chat.updated_at = datetime.now(UTC)
      if not _commit_or_rollback(db):
        raise _PersistFailed("CancelPending did not persist")
    return {"pending": remaining}

  def _clear_pending(self, db, cmd: ClearPending) -> dict:
    """Empty the pending queue; return the count removed.

    Commits only when the queue was non-empty — clearing an already-empty
    queue is a no-op and skips the commit.
    """
    from app.models import Chat

    chat = db.query(Chat).filter(Chat.id == cmd.chat_id).first()
    if chat is None:
      raise _PersistFailed("ClearPending: chat not found")
    cleared = len(chat.pending_messages or [])
    if cleared:
      chat.pending_messages = []
      if not _commit_or_rollback(db):
        raise _PersistFailed("ClearPending did not persist")
    return {"cleared": cleared}

  def _settings_dict(self, raw) -> dict:
    """Return a mutable settings dict from a JSON column value."""
    if isinstance(raw, dict):
      return dict(raw)
    return {}

  def _set_goal(self, db, cmd: SetGoal) -> dict:
    """Set the active goal and preserve unrelated agent settings."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("SetGoal: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    goal = dict(cmd.goal)
    goal.setdefault("state", "active")
    goal.setdefault("turns", 0)
    goal.setdefault("token_spend", 0)
    goal["run_started_at"] = datetime.now(UTC).isoformat()
    settings["goal"] = goal
    chat.agent_settings_json = settings
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("SetGoal did not persist")
    return {"goal": settings["goal"]}

  def _clear_goal(self, db, cmd: ClearGoal) -> dict:
    """Clear the active goal while preserving unrelated agent settings."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("ClearGoal: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    had_goal = "goal" in settings
    settings.pop("goal", None)
    chat.agent_settings_json = settings or None
    chat.updated_at = datetime.now(UTC)
    if had_goal and not _commit_or_rollback(db):
      raise _PersistFailed("ClearGoal did not persist")
    return {"cleared": had_goal}

  def _increment_goal_turn(self, db, cmd: IncrementGoalTurn) -> dict:
    """Increment the stored goal turn counter after a failed evaluation."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("IncrementGoalTurn: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    goal = settings.get("goal")
    if not isinstance(goal, dict):
      return {"turns": 0, "goal": None}
    turns = int(goal.get("turns") or 0) + 1
    goal = dict(goal)
    goal["turns"] = turns
    goal["last_reason"] = cmd.reason
    goal["elapsed_time_s"] = _goal_elapsed_time_s(goal)
    settings["goal"] = goal
    chat.agent_settings_json = settings
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("IncrementGoalTurn did not persist")
    return {"turns": turns, "goal": goal}

  def _record_goal_tokens(self, db, cmd: RecordGoalTokens) -> dict:
    """Add token usage to the stored active goal."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("RecordGoalTokens: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    goal = settings.get("goal")
    if not isinstance(goal, dict):
      return {"token_spend": 0, "goal": None}
    goal = dict(goal)
    token_spend = int(goal.get("token_spend") or 0) + max(
      0, int(cmd.token_delta or 0)
    )
    goal["token_spend"] = token_spend
    goal["elapsed_time_s"] = _goal_elapsed_time_s(goal)
    settings["goal"] = goal
    chat.agent_settings_json = settings
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("RecordGoalTokens did not persist")
    return {"token_spend": token_spend, "goal": goal}

  def _reset_goal_progress(self, db, cmd: ResetGoalProgress) -> dict:
    """Reset per-run counters while keeping the same active goal."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("ResetGoalProgress: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    goal = settings.get("goal")
    if not isinstance(goal, dict):
      return {"goal": None}
    goal = dict(goal)
    goal["turns"] = 0
    goal["last_reason"] = None
    goal["elapsed_time_s"] = 0
    goal["token_spend"] = 0
    goal["state"] = "active"
    goal["run_started_at"] = datetime.now(UTC).isoformat()
    settings["goal"] = goal
    chat.agent_settings_json = settings
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("ResetGoalProgress did not persist")
    return {"goal": goal}

  def _pause_goal(self, db, cmd: PauseGoal) -> dict:
    """Mark the goal paused while preserving progress."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("PauseGoal: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    goal = settings.get("goal")
    if not isinstance(goal, dict):
      return {"goal": None}
    goal = dict(goal)
    goal["state"] = "paused"
    goal["elapsed_time_s"] = _goal_elapsed_time_s(goal)
    settings["goal"] = goal
    chat.agent_settings_json = settings
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("PauseGoal did not persist")
    return {"goal": goal}

  def _resume_goal(self, db, cmd: ResumeGoal) -> dict:
    """Mark the goal active and reset counters for a fresh run."""
    return self._reset_goal_progress(
      db,
      ResetGoalProgress(chat_id=cmd.chat_id, run_token=cmd.run_token),
    )

  def _complete_goal(self, db, cmd: CompleteGoal) -> dict:
    """Append an achieved record and clear the active goal."""
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("CompleteGoal: chat not found or deleted")
    settings = self._settings_dict(chat.agent_settings_json)
    goal = settings.get("goal")
    if not isinstance(goal, dict):
      return {"completed": False, "achieved": None}
    elapsed = _goal_elapsed_time_s(goal)
    token_spend = max(
      int(goal.get("token_spend") or 0),
      int(cmd.token_spend or 0),
    )
    achieved = {
      "condition": str(goal.get("condition") or ""),
      "turns": max(0, int(cmd.turns or 0)),
      "reason": str(cmd.reason or goal.get("last_reason") or ""),
      "elapsed_time_s": elapsed,
      "token_spend": token_spend,
      "completed_at": datetime.now(UTC).isoformat(),
    }
    history = settings.get("achieved_goals")
    if not isinstance(history, list):
      history = []
    history = [*history, achieved][-20:]
    settings["achieved_goals"] = history
    settings.pop("goal", None)
    chat.agent_settings_json = settings or None
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("CompleteGoal did not persist")
    return {"completed": True, "achieved": achieved}

  def _replace_transcript(self, db, cmd: ReplaceTranscript) -> bool:
    """Replace the whole `messages` blob (and optional title); commit.

    Replicates `update_chat`'s transcript branch.  Routes through the actor
    so it serializes with streaming snapshots for the same chat.
    """
    from datetime import UTC, datetime

    chat = _active_chat(db, cmd.chat_id)
    if chat is None:
      raise _PersistFailed("ReplaceTranscript: chat not found or deleted")
    if cmd.title is not None:
      chat.title = cmd.title
    if cmd.messages is not None:
      chat.messages = cmd.messages
    chat.updated_at = datetime.now(UTC)
    if not _commit_or_rollback(db):
      raise _PersistFailed("ReplaceTranscript did not persist")
    return True

  @staticmethod
  def _close_running_runs(db, chat_id, status, except_token=None):
    """Mark every still-running `chat_runs` row for a chat terminal.

    At most one run is ever live per chat (the run marker is per-chat and
    single-owner), so when a new run takes over or the chat goes idle, any
    OTHER row still ``status == "running"`` is by definition stale and is
    moved to `status` ("completed" / "interrupted"). `except_token` keeps the
    incoming run's own row untouched. Keyed off the per-chat single-run
    invariant, so it needs no run-token side bookkeeping. Returns True when it
    changed a row (the caller folds the commit in).
    """
    from datetime import UTC, datetime

    from app.models import ChatRun

    q = db.query(ChatRun).filter(
      ChatRun.chat_id == chat_id, ChatRun.status == "running"
    )
    if except_token is not None:
      q = q.filter(ChatRun.id != except_token)
    changed = False
    for run in q.all():
      run.status = status
      run.ended_at = datetime.now(UTC)
      changed = True
    return changed

  def _clear_run_status(self, db, cmd: ClearRunStatus):
    """Clear the durable run marker when set; commit.

    Identity-keyed compare-and-clear: when `cmd.run_token` is set, clear ONLY
    if that token still owns the marker (`_run_token_owner`). A dying run's
    stale clear thus can't wipe the marker a fresh turn just set — the
    fresh turn's StartTurn recorded itself as the owner, so the dying token
    no longer matches and this is a no-op (the markerless-run race). A
    tokenless clear (run_token="") is unconditional, for paths that already
    know they own the marker (reconciliation, no-handoff cleanup).

    The per-run `chat_runs` record (077 Step 3) is closed in the SAME commit:
    a tokened clear marks its own run's row "completed" IF it is still running.
    A run superseded by a fresh StartTurn is already terminal ("interrupted",
    set by that StartTurn's `_close_running_runs`), so its dying
    no-op-on-the-marker clear correctly leaves the row terminal rather than
    re-stamping it — either way the run's record is closed, never left
    "running". A tokenless clear closes every still-running row for the chat
    (the chat is going idle, so any lingering record is stale).
    """
    from datetime import UTC, datetime

    from app.models import Chat, ChatRun

    owner = self._run_token_owner.get(cmd.chat_id)
    marker_is_ours = not (
      cmd.run_token and owner is not None and owner != cmd.run_token
    )
    changed = False
    if cmd.run_token:
      run = (
        db.query(ChatRun).filter(ChatRun.id == cmd.run_token).first()
      )
      if run is not None and run.status == "running":
        run.status = "completed"
        run.ended_at = datetime.now(UTC)
        changed = True
    elif marker_is_ours:
      changed = self._close_running_runs(
        db, cmd.chat_id, "completed"
      ) or changed
    if not marker_is_ours:
      # A dying run's stale clear: its own run record is closed above, but the
      # per-chat marker belongs to the fresh turn — don't touch it or the
      # owner map.
      if changed and not _commit_or_rollback(db):
        raise _PersistFailed("ClearRunStatus did not persist")
      return None
    chat = db.query(Chat).filter(Chat.id == cmd.chat_id).first()
    if chat is not None and (
      chat.run_status is not None or chat.run_started_at is not None
    ):
      chat.run_status = None
      chat.run_started_at = None
      changed = True
    if changed and not _commit_or_rollback(db):
      raise _PersistFailed("ClearRunStatus did not persist")
    self._run_token_owner.pop(cmd.chat_id, None)
    return None

  @staticmethod
  def _commit_snapshot(db, snapshot: dict):
    """Record one snapshot through the test recording stub.

    The mechanics tests in `test_chat_writer.py` drive the actor with a
    `_RecordingSession` (no real DB) to assert ordering / coalescing /
    fencing: `_test_payload` (FIFO test) records the bare payload, anything
    else the full snapshot.  Real DB writes go through the per-command
    dispatch methods above; this stays only as the DB-free test seam.
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
# One token is allocated per TURN (initial send and each continuation get
# their own), centrally — outside the per-turn sink — so run identity has a
# single source rather than being derived from sink/chat state. The token is
# an OPAQUE string with no semantics callers may lean on; it IS the durable
# `chat_runs.id` (077 Step 3).
#
# It MUST be unique across process RESTARTS, not merely within one process.
# A process-local monotonic counter resets to 1 on every boot, and terminal
# `chat_runs` rows live forever (reconciliation only flips them, never
# deletes), so the first post-restart turn would reissue `rt-1` and collide
# with a surviving row on the `chat_runs.id` PRIMARY KEY — a UNIQUE-constraint
# IntegrityError that fails the turn. A random 128-bit token can't collide
# (within a process or across restarts), needs no DB-seeded high-water mark,
# and `secrets.token_hex` is itself thread-safe, so no lock is required.


def alloc_run_token() -> str:
  """Allocate a process-unique, restart-stable run token.

  Returns an opaque `"rt-<random hex>"` string — callers must treat it as an
  identity tag, not a number (nothing relies on ordering or the integer form).
  It is the durable `chat_runs.id`, so it is random rather than a per-process
  counter that would reissue PKs after a restart and collide with surviving
  terminal rows.
  """
  return f"rt-{secrets.token_hex(16)}"


class _PersistFailed(Exception):
  """A must-persist command's write did not land (no row, no block, or a
  dropped commit).  Raised inside `_dispatch` so the consumer's generic
  `except Exception` fails that command's ack — the awaiting caller then
  declines to broadcast / resolve, per the design's failure semantics —
  without poisoning the actor.  Distinct from `SQLAlchemyError`, so it
  does NOT trigger a session recreate (the session is fine; the write was
  legitimately impossible)."""


# -- chat-transcript persistence helpers ---------------------------------
# These mutate the two JSON blobs on the Chat row (`messages`,
# `pending_messages`).  They live here so the writer actor can call them
# on its own thread without importing back into `chat.py` (which imports
# `alloc_run_token` from this module — the reverse import would cycle).
# `chat.py` re-imports the few it still needs BACK from here, so the
# dependency runs one way (chat.py -> chat_writer).


def _ensure_unique_ts(new_msg: dict, others: list) -> None:
  """Bump `new_msg['ts']` so it's strictly greater than every ts in `others`.

  Used by the `AppendPending` command — two sends in the same millisecond
  would otherwise collide, producing duplicate React keys client-side and
  ambiguous DELETE-by-ts.  Callers pass the union of pending + persisted
  messages (so a queued ts can't equal a persisted assistant ts once it
  promotes).
  """
  if not others:
    return
  max_ts = max((m.get("ts", 0) for m in others), default=0)
  if new_msg.get("ts", 0) <= max_ts:
    new_msg["ts"] = max_ts + 1


def _commit_or_rollback(db) -> bool:
  """Commit and return True; on OperationalError roll back and return False.

  A local copy of `chat._safe_commit` so this module doesn't import back
  into `chat.py`.  A transient SQLite lock returns False (the caller skips
  and a later write repairs) rather than poisoning the session — without
  it one lock burst raises PendingRollbackError on every subsequent
  operation in the turn.
  """
  from sqlalchemy.exc import OperationalError

  try:
    db.commit()
    return True
  except OperationalError as exc:
    log.warning("db commit dropped (rolled back): %s", exc)
    try:
      db.rollback()
    except Exception:
      pass
    return False


def next_message_ts(existing: list) -> int:
  """A wall-clock-ms timestamp strictly greater than every ts in `existing`.

  The streamed-assistant path doesn't flow through the queue's
  `_ensure_unique_ts`, so a fast first assistant write could otherwise
  land in the same millisecond as the user message — two sibling messages
  with equal ts produce duplicate React keys client-side.  Callers pass
  the union of persisted + pending messages so the new ts clears both
  collections.
  """
  now = int(time.time() * 1000)
  max_ts = max((m.get("ts") or 0 for m in existing), default=0)
  return max(now, max_ts + 1)


class _WriteOutcome(enum.Enum):
  """Tri-state result of an assistant-message write.

  The streaming sink path treats NOOP as success (a write with "nothing to
  update yet" is normal mid-stream).  A MUST-PERSIST command
  (`QuestionCommit`/`Finalize`) instead treats NOOP as a FAILURE: there was a
  durable write the caller depends on (the question card it's about to
  broadcast, the terminal turn state), and "nothing was written" must fail
  the ack rather than falsely succeed (silent loss).
  `_apply_last_assistant_message` returns this; `update_last_assistant_message`
  collapses it to the bool the sink caller still expects.
  """

  APPLIED = "applied"  # found a row + assistant slot, committed cleanly
  NOOP = "noop"  # no chat_id / missing row / no messages to write into
  DROPPED = "dropped"  # write attempted but the commit dropped (lock)


def _active_chat(db, chat_id: str):
  """The chat row for `chat_id`, or None if it is missing OR soft-deleted.

  The single gate for "may an actor command write to this chat." Every command
  that ADDS content or SETS the run marker — StartTurn, PromotePending,
  AppendPending, AnswerQuestion, ReplaceTranscript, and the assistant-message
  write below — loads the row through this, so a command queued before a
  concurrent delete and processed after the soft-delete commits cannot write to
  (and thereby resurrect) the dead row. The route handlers check `deleted_at`
  at request time; this closes the TOCTOU against the asynchronous actor.
  Treating deleted like missing means each command's existing `chat is None`
  branch already does the right thing (raise → the ack fails, so no marker or
  continuation lands on the dead row; reconciliation skips deleted chats).
  Clear/cancel commands deliberately do NOT gate on `deleted_at` — removing
  state from a deleted chat cannot resurrect it.
  """
  from app.models import Chat

  return db.query(Chat).filter(
    Chat.id == chat_id, Chat.deleted_at.is_(None)
  ).first()


def _goal_elapsed_time_s(goal: dict) -> float:
  """Return elapsed seconds since the active goal run began."""
  from datetime import UTC, datetime

  started_at = goal.get("run_started_at") or goal.get("started_at")
  if not started_at:
    return float(goal.get("elapsed_time_s") or 0)
  try:
    parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
  except ValueError:
    return float(goal.get("elapsed_time_s") or 0)
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=UTC)
  return max(0.0, datetime.now(UTC).timestamp() - parsed.timestamp())


def _apply_last_assistant_message(db, chat_id: str, message: dict):
  """Core assistant-message write, returning a `_WriteOutcome`.

  Distinguishes APPLIED (the write landed), NOOP (no chat_id / missing row /
  empty transcript — nothing to update yet), and DROPPED (the commit dropped
  on a transient lock).  The lenient sink path (`PersistTranscript`/
  `PersistError`) and `update_last_assistant_message` map NOOP to success; a
  must-persist command maps it to a raised ack.
  """
  if not chat_id:
    return _WriteOutcome.NOOP
  # `_active_chat` filters soft-deleted rows, so a finalize/snapshot enqueued
  # before a delete maps to NOOP here instead of resurrecting the dead row.
  chat = _active_chat(db, chat_id)
  if not chat or not chat.messages:
    return _WriteOutcome.NOOP
  msgs = list(chat.messages)
  if msgs and msgs[-1].get("role") == "assistant":
    # Carry answers forward: apply_answers_to_last_question writes
    # them here; the runner rebuilds from assistant_blocks (no
    # answers), so merge keyed by question_block_key to avoid wiping
    # them on writeback. Multi-question turns are supported.
    existing_answers_by_key = {}
    for ob in msgs[-1].get("blocks") or []:
      if ob.get("type") == "question" and ob.get("answers"):
        existing_answers_by_key[question_block_key(ob)] = ob["answers"]
    for nb in message.get("blocks") or []:
      if nb.get("type") == "question" and not nb.get("answers"):
        carried = existing_answers_by_key.get(question_block_key(nb))
        if carried:
          nb["answers"] = carried
    # Carry a STABLE per-turn ts. build_assistant_message omits ts, so
    # assistant messages historically persisted with ts=None — which
    # silently defeated the frontend bridge gate (useBridgePartial keys
    # the kept partial by ts). On reconnect mid-question the persisted
    # card AND the replayed stream card both rendered (the duplicate
    # question/answer bug). Preserve the existing message's ts across
    # every streaming replace so the id stays stable for the whole turn;
    # backfill one only if an older, tsless message is being updated.
    message["ts"] = msgs[-1].get("ts")
    if message["ts"] is None:
      # Backfilling an older, tsless assistant message. Allocate against
      # persisted messages EXCLUDING msgs[-1] (it's the tsless one being
      # stamped, so it can only contribute 0) plus queued messages, so the
      # new ts can't collide with a pending user msg once it promotes into
      # chat.messages (equal ts -> duplicate React keys). The pending read
      # is here, not on the hot path, so the common replace does no extra
      # work. Mirrors `_ensure_unique_ts` (the queue allocator, which also
      # unions in chat.messages), so the two allocators can't hand out the
      # same ms.
      message["ts"] = next_message_ts(
        msgs[:-1] + list(chat.pending_messages or [])
      )
    msgs[-1] = message
  else:
    # First write of this turn's assistant message — stamp a ts greater
    # than every persisted AND queued message so the bridge gate and the
    # frontend's ts-keyed rendering get a stable, collision-free id.
    message["ts"] = next_message_ts(msgs + list(chat.pending_messages or []))
    msgs.append(message)
  chat.messages = msgs
  return (
    _WriteOutcome.APPLIED
    if _commit_or_rollback(db)
    else _WriteOutcome.DROPPED
  )


def update_last_assistant_message(db, chat_id: str, message: dict) -> bool:
  """Updates the last assistant message in the chat (for streaming updates).

  The bool adapter the streaming sink still consumes: APPLIED/NOOP -> True
  (a mid-stream write with nothing to update yet is fine), DROPPED -> False.
  Must-persist commands call `_apply_last_assistant_message` directly so they
  can fail their ack on a NOOP (silent-loss guard); this wrapper's semantics
  are unchanged for the sink caller.
  """
  return _apply_last_assistant_message(db, chat_id, message) is not (
    _WriteOutcome.DROPPED
  )


def finalize_response_outcome(db, chat_id: str, assistant_blocks: list):
  """End-of-response cleanup, returning a `_WriteOutcome`.

  Empty blocks -> NOOP (no terminal state to write); otherwise force-complete
  the tool blocks and delegate to `_apply_last_assistant_message`, which
  distinguishes APPLIED / NOOP (missing row, empty transcript) / DROPPED.  A
  must-persist `Finalize` raises on anything but APPLIED so it never acks
  success on a write that did not land.
  """
  if not assistant_blocks:
    return _WriteOutcome.NOOP
  finalize_blocks(assistant_blocks)
  return _apply_last_assistant_message(
    db, chat_id, build_assistant_message(assistant_blocks)
  )


def apply_answers_to_last_question(
  chat, answers: dict | None, question_id: str | None = None
) -> bool:
  """Writes `answers` into the question block being answered.

  When `question_id` is supplied, the answers are written into the block
  whose `question_id` matches EXACTLY — this routes the answer to the
  right question when two are open at once (the latest-question search
  below would hit the wrong, later one). An unknown id matches nothing
  and returns False rather than silently falling back, which would
  re-introduce the wrong-block bug. When `question_id` is absent, the
  LAST assistant message's last question block is updated (backward-
  compatible with clients that don't send the id).

  Returns True if a question block was found and updated.
  """
  if not answers:
    return False
  from sqlalchemy.orm.attributes import flag_modified

  msgs = list(chat.messages or [])

  if question_id:
    # Identity match: scan every assistant message's question blocks for
    # the exact id. Precise — never falls back to "latest".
    for msg in reversed(msgs):
      if msg.get("role") != "assistant":
        continue
      for block in msg.get("blocks") or []:
        if (
          block.get("type") == "question"
          and block.get("question_id") == question_id
        ):
          block["answers"] = answers
          chat.messages = msgs  # rebind so SQLAlchemy detects the mutation
          flag_modified(chat, "messages")
          return True
    return False

  # No question_id supplied — preserve the legacy latest-question
  # behaviour (older clients that don't send the id).
  log.debug(
    "answer applied without question_id; using latest-question fallback "
    "chat_id=%s",
    getattr(chat, "id", "?"),
  )
  for msg in reversed(msgs):
    if msg.get("role") != "assistant":
      continue
    for block in reversed(msg.get("blocks") or []):
      if block.get("type") == "question":
        block["answers"] = answers
        chat.messages = msgs  # rebind so SQLAlchemy detects JSON mutation
        flag_modified(chat, "messages")
        return True
  return False


# -- module singleton + lifespan accessors -------------------------------
# One actor per process.  `start_writer` is called from the FastAPI
# lifespan AFTER db init + crash reconciliation (which must run before the
# actor exists — recovery cannot depend on a healthy writer); `stop_writer`
# drains on shutdown.
_writer: ChatWriterActor | None = None
# Serializes the singleton check+create in `start_writer` (and the
# clear in `stop_writer`) so two concurrent callers can't both pass the
# "already started" check and each construct + start a writer, orphaning one
# daemon thread that keeps consuming a stranded queue.
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


def writer_readiness() -> tuple[bool, str | None]:
  """Report whether the process writer can serve chat persistence right now.

  Returns `(ready, reason)`: `ready` is True only when the writer can
  actually accept and commit a command, and `reason` is a short
  human-readable explanation of the FIRST failed condition (None when
  ready) so a readiness probe can log WHY it failed without poking the
  actor's privates itself.

  Liveness (`/api/health`) is not the same as readiness. A writer can be
  the published singleton yet unable to persist a single chat write, in
  five distinct ways — all of them matter, and "alive + not fatal" alone is
  incomplete (a stopping writer, or one whose session hasn't opened, would
  wrongly report ready):

  - no singleton: `start_writer` hasn't run (or `stop_writer` cleared it),
    so there is nothing to write through;
  - dead thread: the worker thread never spawned, or exited (its `_run`
    loop returned at a `DrainAndStop` or crashed out), so the FIFO queue
    has no consumer;
  - session not open: the thread is alive but its DB session hasn't opened
    yet — `start()` publishes the actor BEFORE `_run` opens the session, so a
    live (or session-hung) thread can't persist until `_session_ready` fires;
  - fatal: the worker hit a thread-fatal error (e.g. the session factory
    raised at first use) and now fails every ack rather than committing;
  - stopping: `stop()` flipped `_stopping`, so `submit()` rejects every
    new command — the writer is draining toward exit and cannot accept work.

  Centralizing the predicate here keeps the readiness route (and the
  deploy gate it backs) from reaching into the actor's internals, and
  keeps all five conditions in one place where the actor's own contract
  lives.
  """
  writer = _writer
  if writer is None:
    return False, "writer not started"
  thread = writer._thread
  if thread is None or not thread.is_alive():
    return False, "writer thread not alive"
  if not writer._session_ready.is_set():
    # Thread is alive but its DB session hasn't opened yet (`start()` publishes
    # the actor before `_run` opens the session) — not yet able to persist.
    return False, "writer session not ready"
  if writer._fatal:
    return False, "writer is fatal"
  if writer._stopping:
    return False, "writer is stopping"
  return True, None


def is_writer_ready() -> bool:
  """True when the process writer can accept and commit a command.

  Thin boolean wrapper over `writer_readiness` for callers that only need
  the verdict (see `writer_readiness` for the five conditions and why each
  matters).
  """
  ready, _ = writer_readiness()
  return ready
