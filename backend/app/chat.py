"""Agent chat via the official provider SDKs.

Routes each chat turn through the SDK-backed runner for the matching
provider (`claude_sdk_runner.py`, `codex_sdk_runner.py`) and bridges the
runner's events onto the chat's `ChatBroadcast` so any number of SSE
clients can subscribe.  Provider env / auth wiring lives in
`providers.py`.
"""

import asyncio
import copy
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import auth, chat_queue, models, questions, schemas
from app.broadcast import ChatBroadcast, create_broadcast, get_broadcast, set_active_broadcast
from app.config import get_settings
from app.events import (
  build_assistant_message,
  finalize_blocks,
  process_event,
  question_block_key,
)
from app.providers import effective_agent_settings, get_provider, get_skill_path
from app.runner_registry import registry
from app.runtime_types import ChatEvent


def _get_logger() -> logging.Logger:
  """Returns a logger that writes to the data/logs/chat.log file.

  Default level is INFO so chat.log stays small — one line per chat
  start/done/error, not one per streaming delta.  Set
  `MOEBIUS_CHAT_DEBUG=1` to capture all stream events when investigating
  a parser issue.  The env var is read once on first access (the logger
  handler is memoized); toggling it at runtime has no effect — restart
  the process to pick up a change.
  """
  logger = logging.getLogger("moebius.chat")
  if logger.handlers:
    return logger
  settings = get_settings()
  log_dir = Path(settings.data_dir) / "logs"
  log_dir.mkdir(parents=True, exist_ok=True)
  handler = RotatingFileHandler(
    log_dir / "chat.log",
    maxBytes=50 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
  )
  handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
  )
  logger.addHandler(handler)
  logger.setLevel(
    logging.DEBUG if os.getenv("MOEBIUS_CHAT_DEBUG") else logging.INFO
  )
  return logger


def _safe_commit(db: Session) -> bool:
  """Commits and returns True; on OperationalError (e.g. SQLite lock),
  rolls back and returns False so the caller can skip and continue.

  Without this, a single transient lock burst poisons the session and
  every subsequent operation in this turn raises PendingRollbackError,
  killing the chat. With it, a missed streaming-save is a missed
  streaming-save; the next event tries again.
  """
  try:
    db.commit()
    return True
  except OperationalError as exc:
    _get_logger().warning("db commit dropped (rolled back): %s", exc)
    try:
      db.rollback()
    except Exception:
      pass
    return False


def _save_message(db: Session, chat_id: str, message: dict):
  """Appends a message to the chat's messages array in the DB."""
  if not chat_id:
    return
  from app.models import Chat
  chat = db.query(Chat).filter(Chat.id == chat_id).first()
  if not chat:
    return
  msgs = list(chat.messages or [])
  msgs.append(message)
  chat.messages = msgs
  _safe_commit(db)


def _next_message_ts(existing: list) -> int:
  """A wall-clock-ms timestamp strictly greater than every ts already in
  `existing`. The streamed-assistant path doesn't flow through the queue's
  `_ensure_unique_ts`, so a fast first assistant write could otherwise land
  in the same millisecond as the user message — two sibling messages with
  equal ts produce duplicate React keys client-side. Callers pass the union
  of persisted + pending messages so the new ts clears both collections."""
  now = int(time.time() * 1000)
  max_ts = max((m.get("ts") or 0 for m in existing), default=0)
  return max(now, max_ts + 1)


def _update_last_assistant_message(db: Session, chat_id: str, message: dict) -> bool:
  """Updates the last assistant message in the chat (for streaming updates)."""
  if not chat_id:
    return True
  from app.models import Chat
  chat = db.query(Chat).filter(Chat.id == chat_id).first()
  if not chat or not chat.messages:
    return True
  msgs = list(chat.messages)
  # Allocate any new ts against persisted AND queued messages — a pending
  # user message (stamped by chats_stream._ensure_unique_ts off a disjoint
  # collection) must not collide with this assistant's ts once it promotes
  # into chat.messages (equal ts -> duplicate React keys).
  pending = list(chat.pending_messages or [])
  if msgs and msgs[-1].get("role") == "assistant":
    # Carry answers forward: _apply_answers_to_last_question writes
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
      message["ts"] = _next_message_ts(msgs[:-1] + pending)
    msgs[-1] = message
  else:
    # First write of this turn's assistant message — stamp a ts so the
    # bridge gate and the frontend's ts-keyed rendering have a stable id.
    message["ts"] = _next_message_ts(msgs + pending)
    msgs.append(message)
  chat.messages = msgs
  return _safe_commit(db)


# Queue management (per-chat lock, promote, drain_and_release) lives
# in `app.chat_queue` after ticket 033. The pending-question registry
# lives in `app.questions`. chat.py imports both and uses them
# directly; no shims remain.

class _ChatEventSink:
  """Bridges SDK-runner events to broadcast + DB state.

  SDK runners publish Möbius events via `sink.publish(event)`. The
  sink forwards each event to the real broadcast, accumulates
  assistant content blocks for the message-in-progress, throttles
  DB writes, and captures `session_id` + `cost_usd` from terminal
  events. This keeps SDK runners pure (one-way SDK → events) while
  the chat-side state stays here.

  Lifetime: one sink per `_run_chat_impl` call. After the runner
  returns, the chat-impl wrapper calls `finalize()` which writes the
  final assistant message + persists session_id/cost on the chat row.

  `publish()` is deliberately synchronous and cheap: it only queues
  an ordered command. One loop-owned consumer performs event
  accumulation, broadcasts, snapshot parking, and worker commits.
  This preserves SSE order without a threading lock while keeping
  SQLite I/O off the event loop.
  """

  _SAVE_INTERVAL_SECS = 1.0
  # Subset of app.events.EventType that forces a save so the user does
  # not reconnect into a stale transcript mid-turn.
  _IMMEDIATE_SAVE_TYPES = frozenset(
    {"tool_start", "tool_end", "error", "question"}
  )

  def __init__(self, bc, chat_id: str, db: Session):
    self.bc = bc
    self.chat_id = chat_id
    self.db = db
    self.assistant_blocks: list = []
    self.session_id: str | None = None
    self.cost_usd: float | None = None
    self._last_save = 0.0
    # Assistant-message snapshot recorded by the consumer when a non-
    # question save comes due, drained by a flush barrier into an off-loop
    # commit. None means nothing to write. Snapshots coalesce: a later
    # publish overwrites an undrained snapshot, and since each snapshot
    # is the FULL current message, the drained one still reflects every
    # block accumulated up to the flush.
    self._pending_save: tuple[int, dict] | None = None
    self._save_generation = 0
    self._persist_queue: asyncio.Queue = asyncio.Queue()
    self._consumer_task: asyncio.Task | None = None
    self._consumer_error: BaseException | None = None

  def _persist_in_worker(self, _generation: int, snapshot: dict) -> bool:
    """Persists one snapshot with a worker-owned session."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
      return _update_last_assistant_message(db, self.chat_id, snapshot)
    finally:
      db.close()

  def _start_consumer_if_running(self) -> None:
    """Starts the queue consumer when construction happened on a loop."""
    if self._consumer_task is not None:
      return
    # Some unit tests and sync callers construct sinks without a running
    # loop. Lazy start lets those callers enqueue safely; the first async
    # flush/finalize starts the consumer and drains the accumulated work.
    try:
      loop = asyncio.get_event_loop()
    except RuntimeError:
      return
    if loop.is_running():
      self._consumer_task = loop.create_task(self._run_persist_queue())

  def publish(self, event: ChatEvent) -> bool:
    """Queues an event for ordered loop-owned processing."""
    self._persist_queue.put_nowait(("event", event, None))
    self._start_consumer_if_running()
    return True

  async def _process_event(self, event: ChatEvent) -> None:
    """Processes one event in queue order."""
    event_type = event.get("type")
    accumulated = process_event(event, self.assistant_blocks)
    needs_save = accumulated and self.chat_id and (
      event_type in self._IMMEDIATE_SAVE_TYPES
      or time.monotonic() - self._last_save >= self._SAVE_INTERVAL_SECS
    )

    # Questions are saved before their broadcast so a fast answer submit
    # always finds the persisted block. The same consumer serializes this
    # behind prior barriers, so no lock or inline loop-thread commit exists.
    if needs_save and event_type == "question":
      self._pending_save = None
      self._save_generation += 1
      self._last_save = time.monotonic()
      snapshot = copy.deepcopy(
        build_assistant_message(self.assistant_blocks)
      )
      await asyncio.to_thread(
        self._persist_in_worker, self._save_generation, snapshot,
      )

    self.bc.publish(event)
    if event_type == "done":
      self.cost_usd = event.get("cost_usd")

    if needs_save and event_type != "question":
      self._last_save = time.monotonic()
      self._save_generation += 1
      # build_assistant_message aliases assistant_blocks. Freeze the
      # snapshot before a later event mutates the live list.
      self._pending_save = (
        self._save_generation,
        copy.deepcopy(build_assistant_message(self.assistant_blocks)),
      )

  async def _flush_pending(self) -> bool:
    """Persists the latest parked snapshot, if any."""
    pending = self._pending_save
    if pending is None:
      return True
    self._pending_save = None
    # The loop-owned consumer bumps generations before parking a
    # replacement snapshot. Drop stale work rather than letting an
    # older save overwrite a fuller question/final snapshot.
    if pending[0] != self._save_generation:
      return True
    return await asyncio.to_thread(self._persist_in_worker, *pending)

  def _fail_queued_futures(self, exc: BaseException) -> None:
    """Fails barriers left behind by a consumer-side exception."""
    while True:
      try:
        _, _, future = self._persist_queue.get_nowait()
      except asyncio.QueueEmpty:
        return
      if future is not None and not future.done():
        future.set_exception(exc)

  async def _run_persist_queue(self) -> None:
    """Consumes commands until finalize drains and terminates the sink."""
    # This task is the sole owner of assistant_blocks, pending snapshot
    # state, save generations, save throttling, broadcasts, and worker
    # commit dispatch. Keeping that ownership on one loop task removes
    # the old cross-thread lock and makes command ordering explicit.
    while True:
      command, payload, future = await self._persist_queue.get()
      try:
        if command == "event":
          await self._process_event(payload)
        elif command == "barrier":
          result = await self._flush_pending()
          if not future.done():
            future.set_result(result)
        elif command == "finalize":
          # Finalization supersedes any undrained streaming snapshot and
          # runs before run_chat clears its durable Stop marker.
          self._pending_save = None
          if self.chat_id and self.assistant_blocks:
            self._save_generation += 1
            snapshot = copy.deepcopy(self.assistant_blocks)
            await asyncio.to_thread(self._finalize_in_worker, snapshot)
          if not future.done():
            future.set_result(None)
          return
      except Exception as exc:
        # Never let the consumer die silently: fail the active barrier
        # plus every queued flush/finalize future so awaiters cannot hang.
        _get_logger().exception("chat event sink consumer failed: %s", exc)
        self._consumer_error = exc
        if future is not None and not future.done():
          future.set_exception(exc)
        self._fail_queued_futures(exc)
        return

  def _finalize_in_worker(self, blocks: list) -> bool:
    """Finalizes a response using a worker-owned session."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
      _finalize_response(db, self.chat_id, blocks)
      return True
    finally:
      db.close()

  async def flush(self) -> bool:
    """Queues a barrier and waits until prior commands are persisted."""
    if self._consumer_error is not None:
      raise self._consumer_error
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    self._persist_queue.put_nowait(("barrier", None, future))
    self._start_consumer_if_running()
    return await future

  async def finalize(self) -> None:
    """Queues terminal persistence, drains the consumer, and awaits exit."""
    if self._consumer_error is not None:
      if self._consumer_task is not None:
        await self._consumer_task
      raise self._consumer_error
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    self._persist_queue.put_nowait(("finalize", None, future))
    self._start_consumer_if_running()
    try:
      await future
    finally:
      # Every terminal caller awaits this method, including SDK success,
      # SDK error-result, exception, and Stop-driven exits.
      if self._consumer_task is not None:
        await self._consumer_task


_SKILL_TEXT_CACHE: str | None = None
# A stopped SDK handle drains before `_run_chat_impl` performs its final
# sink save. Hand durable-marker clearing back to that run's wrapper so
# the marker survives until persistence is complete.
_clear_after_terminal_generation: dict[str, int] = {}


def _read_skill_text() -> str:
  """Returns the agent skill (system-prompt) text, cached after first
  read. Used by SDK runners as the `system_prompt` option.

  Process-lifetime cache: an in-place edit to the skill file inside
  a running container won't be picked up until the container restarts.
  This is intentional given the deploy model (image rebuild → restart
  → fresh cache load), but worth knowing if you're testing skill edits
  on a live container — `docker restart mobius-test` (or prod) is
  required to refresh.
  """
  global _SKILL_TEXT_CACHE
  if _SKILL_TEXT_CACHE is not None:
    return _SKILL_TEXT_CACHE
  skill_path = get_skill_path()
  if skill_path is not None:
    try:
      text = skill_path.read_text(encoding="utf-8")
      _SKILL_TEXT_CACHE = text
      return text
    except (OSError, FileNotFoundError):
      pass
  # No skill file found — cache the empty fallback so subsequent calls
  # don't re-stat the filesystem. The empty case is genuinely degraded
  # (SDK runs without a system prompt) and the test suite relies on
  # this path working; warn loudly so the silent-failure variant
  # ("volume mount race", "CI without /app/skill mounted") is visible
  # in chat.log instead of disappearing into the cache.
  _get_logger().warning(
    "skill file not found at expected paths; SDK turns will run "
    "without a system prompt"
  )
  _SKILL_TEXT_CACHE = ""
  return ""


def current_run_generation(chat_id: str) -> int:
  """Returns the current generation for a chat (0 if none)."""
  return registry.current_generation(chat_id)


def bump_run_generation(chat_id: str) -> int:
  """Bumps the per-chat generation counter and returns the new value.

  Used by callers that need to invalidate any in-flight or about-to-
  start run for a chat without going through `stop_chat_for`. Delete
  uses this to close the idle→starting race: a concurrent POST that
  hits `mark_starting` between the delete's `is_chat_running` check
  and the soft-delete commit would otherwise leave a runner writing
  to the just-deleted row. Bumping the generation makes any future
  `we_own_gen` check fail, so the runner's auto-promote/continuation
  skips writing.
  """
  return registry.bump_generation(chat_id)


def forget_chat(chat_id: str) -> None:
  """Drops any per-chat bookkeeping so a deleted chat doesn't leak.

  Safe to call when the chat is already idle; mid-run callers should
  rely on stop_chat_for first. Currently scrubs the run-generation
  entry — extend here if future per-chat state shows up.
  """
  registry.forget(chat_id)


# Durable run marker. The runner registry holds the live "is this chat
# running" truth in memory; these two helpers mirror it onto the Chat
# row so it survives a process death. The pair (set on turn start,
# clear on turn end) is what lets startup reconciliation distinguish a
# chat that genuinely finished from one whose process was killed
# mid-turn. Best-effort, like the other small DB writers here: a
# missed write degrades to "reconciliation has nothing to fix" (clear
# missed) or "reconciliation resolves a turn that actually finished"
# (set missed) — both are self-correcting and never strand the chat.

def _mark_run_started(db: Session, chat_id: str) -> None:
  """Marks the chat's row as having a turn in flight (durable copy of
  the in-memory registry state)."""
  if not chat_id:
    return
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if chat is not None:
      chat.run_status = "running"
      chat.run_started_at = datetime.now(UTC)
      db.commit()
  except Exception:
    db.rollback()


def _clear_run_status(db: Session, chat_id: str) -> None:
  """Clears the chat's durable run marker once the turn has ended."""
  if not chat_id:
    return
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if chat is not None and (
      chat.run_status is not None or chat.run_started_at is not None
    ):
      chat.run_status = None
      chat.run_started_at = None
      db.commit()
  except Exception:
    db.rollback()


def reconcile_interrupted_chats(db: Session) -> list[str]:
  """Resolve chats stranded "running" by a process that died mid-turn.

  Called once from the FastAPI lifespan startup, BEFORE the server
  accepts requests. The runner registry is in-memory, so at boot it is
  always empty: every chat whose row still reads ``run_status ==
  "running"`` is therefore a turn the previous process never finished
  (a clean shutdown clears the marker in run_chat's finally; only a
  crash — OOM / SIGKILL — leaves it set). For each such chat we:

    - finalize the persisted transcript so a reopen renders a resolved
      turn rather than a forever-spinning tool block: any tool block
      still marked "running" on the last assistant message is forced
      to "done" (server-side truth, not just the client-side mask in
      ChatView), and a short interrupted-turn error block is appended;
    - drop any stranded ``pending_messages`` (queued sends that would
      otherwise never drain). They are CLEARED, not auto-resumed: the
      process most likely died from resource pressure, so re-spawning
      agent turns during boot risks a crash loop, and there is no live
      SSE client to receive them. Clearing is the reversible choice —
      the user resends if they still want the work. The count is noted
      in the appended error so the surprise is visible;
    - clear the durable run marker.

  No queue lock is taken: this runs single-threaded at startup before
  any POST /messages can land, so the serialization invariant that
  ``_clear_pending_messages`` documents has no concurrent writer to
  guard against here.

  Returns the ids of the chats it reconciled (empty list if none) so
  the caller can log/observe the recovery rather than have it happen
  silently.
  """
  log = _get_logger()
  reconciled: list[str] = []
  try:
    stale = (
      db.query(models.Chat)
      .filter(models.Chat.run_status == "running")
      .filter(models.Chat.deleted_at.is_(None))
      .all()
    )
  except Exception:
    log.exception("reconcile_interrupted_chats: query failed")
    return reconciled

  for chat in stale:
    # Belt-and-suspenders: if a live registry entry somehow exists for
    # this chat (it cannot at a cold boot, but a future warm-restart
    # path might call this), the turn is genuinely in flight — leave it
    # alone rather than yank a running turn's transcript out from under
    # it.
    if registry.is_alive(chat.id):
      continue
    try:
      dropped = len(chat.pending_messages or [])
      msgs = list(chat.messages or [])
      note = "The previous turn was interrupted (the server restarted)."
      if dropped:
        note += (
          f" {dropped} queued message(s) were cleared — resend them if"
          " you still need them."
        )
      # `message` (not `content`) is the error-block field the
      # transcript renderer reads — see MsgContent.jsx's error branch
      # and events.process_event's "error" handler, which both key on
      # block["message"]. Matching that shape makes the synthetic note
      # render identically to a live provider error.
      err_block = {"type": "error", "message": note}
      if msgs and msgs[-1].get("role") == "assistant":
        blocks = list(msgs[-1].get("blocks") or [])
        finalize_blocks(blocks)
        blocks.append(err_block)
        # build_assistant_message omits ts; carry the turn's existing
        # stable ts (the frontend bridge + React keys rely on it — a
        # ts-less message is dropped by useBridgePartial). Mirrors the
        # ts-carry in _update_last_assistant_message.
        prev_ts = msgs[-1].get("ts")
        msgs[-1] = build_assistant_message(blocks)
        msgs[-1]["ts"] = (
          prev_ts if prev_ts is not None else _next_message_ts(msgs[:-1])
        )
      else:
        # Process died before any assistant content persisted — surface
        # the interruption as a standalone assistant turn so the user
        # isn't left staring at their own unanswered message.
        new_msg = build_assistant_message([err_block])
        new_msg["ts"] = _next_message_ts(msgs)
        msgs.append(new_msg)
      chat.messages = msgs
      chat.pending_messages = []
      chat.run_status = None
      chat.run_started_at = None
      db.commit()
      reconciled.append(chat.id)
    except Exception:
      db.rollback()
      log.exception(
        "reconcile_interrupted_chats: failed to reconcile chat_id=%s",
        chat.id,
      )

  if reconciled:
    log.info(
      "reconciled %d interrupted chat(s) on startup: %s",
      len(reconciled), ", ".join(reconciled),
    )
  return reconciled


def _clear_pending_messages(db: Session | None, chat_id: str) -> None:
  """Clears persisted queued messages for the chat, best-effort.

  Caller MUST hold ``chat_queue.get_lock(chat_id)`` — this mutation
  shares the queue's serialization invariant with every other RMW
  on ``chat.pending_messages`` (append in routes/chats_stream.py:POST
  /messages, cancel in DELETE /pending, promote in
  chat_queue.drain_and_release). Without the lock, a concurrent
  append can read the queue, this clear can overwrite it with [],
  and the appender then writes back its stale snapshot — the user's
  freshly-queued message vanishes. See CLAUDE.md "Message queue +
  send-while-generating".
  """
  if db is None:
    return
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if chat and chat.pending_messages:
      chat.pending_messages = []
      db.commit()
  except Exception:
    db.rollback()


def _finalize_broadcast_if_running(chat_id: str) -> None:
  """Publishes a terminal done event when the chat broadcast is live."""
  bc = get_broadcast(chat_id)
  if bc and bc.running:
    bc.publish({"type": "done", "cost_usd": 0})
    bc.mark_completed()


def is_chat_running(chat_id: str) -> bool:
  """Returns True if an agent subprocess is running or starting for this chat."""
  if registry.is_alive(chat_id):
    return True
  bc = get_broadcast(chat_id)
  return bc is not None and bc.running


def mark_starting(chat_id: str) -> bool:
  """Atomically marks a chat as starting.  Returns False if already active."""
  if is_chat_running(chat_id):
    return False
  return registry.mark_starting(chat_id)


def discard_starting(chat_id: str) -> None:
  """Removes a chat_id from the starting set.  Call from send_message's
  error handler if the caller fails before scheduling run_chat — otherwise
  the chat_id leaks and the chat is stuck 'starting' until process restart."""
  registry.discard_starting(chat_id)


async def stop_chat(chat_id: str | None = None, db: Session = None) -> bool:
  """Kills the active subprocess for a chat, bumps its generation, and
  clears its pending queue so a queued continuation cannot auto-start
  after Stop. Session_id is preserved so the next message resumes."""
  if chat_id is not None:
    return await stop_chat_for(chat_id, db=db)
  from app.broadcast import _broadcasts
  # Snapshot `_broadcasts` via `list()` first — iterating the live
  # mapping can raise RuntimeError if a concurrent task creates a
  # new broadcast (e.g. a chat starts during a global Stop sweep).
  targets = registry.all_alive_chat_ids() | {
    cid for cid, bc in list(_broadcasts.items()) if bc.running
  }
  stopped_any = False
  for cid in targets:
    if await stop_chat_for(cid, db=db):
      stopped_any = True
  return stopped_any


async def stop_chat_for(chat_id: str, db: Session = None) -> bool:
  """Kills the agent subprocess for a specific chat.

  Bumps the generation counter so the dying run_chat's finally
  skips _promote_pending_messages / _schedule_continuation. Clears
  chat.pending_messages so any queued items don't auto-drain from
  the backend side. The frontend (ChatView.jsx:handleStop) snapshots
  the queue BEFORE POSTing /chat/stop, then re-submits the combined
  text as ONE follow-up turn via doSend — that's where queued work
  gets sent. Backend Stop is purely the interrupt; the frontend owns
  the "collapse + resend" UX. See CLAUDE.md "Stop-chat contract".

  Waits for the process to die with a bounded timeout.
  """
  stopped_gen = current_run_generation(chat_id)
  bump_run_generation(chat_id)
  handles = registry.get_handles(chat_id)
  if handles:
    _clear_after_terminal_generation[chat_id] = stopped_gen
  # The queue-lock window guards the pending_messages clear from
  # racing concurrent append/cancel/promote paths. Generation bump
  # happens BEFORE the lock so the dying runner sees the new gen as
  # soon as it next checks (no need for the lock — generation is its
  # own state).
  async with chat_queue.get_lock(chat_id):
    _clear_pending_messages(db, chat_id)
  questions.cancel(chat_id)
  all_stopped = True
  log = _get_logger()
  for handle in handles:
    stopped = await handle.stop(timeout=2.0)
    if not stopped:
      log.warning(
        "stop_chat_for: handle.stop() timed out for chat %s "
        "(%s) — unregistering anyway to converge state",
        chat_id, handle.kind,
      )
      all_stopped = False
    registry.unregister(chat_id, handle.kind)
  # With no active handle there is no runner-side final save left to
  # await, so clear immediately. Active handles hand this clear back to
  # run_chat's finally block: SDK stop waiters resolve before chat.py's
  # final sink save, and a SQLite-blocked flush can exceed Stop's 2s
  # timeout. If the process dies first, the retained marker lets crash
  # recovery reconcile the interrupted turn.
  if not handles:
    if db is not None:
      _clear_run_status(db, chat_id)
    else:
      from app.database import SessionLocal
      _db = SessionLocal()
      try:
        _clear_run_status(_db, chat_id)
      finally:
        _db.close()
  _finalize_broadcast_if_running(chat_id)
  registry.discard_starting(chat_id)
  return all_stopped


def _finalize_response(
  db: Session,
  chat_id: str,
  assistant_blocks: list,
) -> None:
  """End-of-response cleanup: force-complete tool blocks and save."""
  if not assistant_blocks:
    return
  finalize_blocks(assistant_blocks)
  _update_last_assistant_message(
    db, chat_id, build_assistant_message(assistant_blocks),
  )


def _clear_pending_queue(db: Session, chat_id: str) -> None:
  """Empties the pending_messages queue for a chat. Used on terminal
  setup errors (no owner, missing auth) so queued messages don't pile
  up repeating the same error.

  Caller MUST hold ``chat_queue.get_lock(chat_id)`` — same invariant
  as ``_clear_pending_messages``; see that docstring.
  """
  if not chat_id:
    return
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if chat and chat.pending_messages:
      chat.pending_messages = []
      db.commit()
  except Exception:
    db.rollback()


def _schedule_continuation(
  chat_id: str,
  messages: list,
  session_id: str | None,
  provider_id: str | None,
  next_user: dict,
) -> None:
  """Bumps generation and spawns the next-turn run_chat.

  Precondition: the caller already holds the 'starting' claim for
  this chat. Two paths satisfy that:
    - Turn-end continuation (finally in _run_chat_impl): the original
      send's mark_starting from chats_stream.py is still in _starting
      and gets handed off to the new run via the generation bump.
    - Stale-pending drain (chats_stream.py send_message): the route
      explicitly calls mark_starting before _promote_pending_messages.
  If scheduling fails, this function releases the claim so the chat
  isn't stuck 'starting' until process restart.
  """
  log = _get_logger()
  bc = None
  coro = None
  try:
    # Inside the try so any exception (even from these lines) releases
    # the _starting claim the caller held. Without this, a failure
    # here would leak _starting until process restart.
    next_gen = bump_run_generation(chat_id)
    bc = create_broadcast(chat_id)  # registered in global registry
    # Build the coroutine BEFORE create_task so the except block can
    # .close() it if scheduling raises — otherwise Python warns
    # "coroutine was never awaited" and leaks the un-driven coroutine.
    coro = run_chat(
      messages,
      chat_id=chat_id,
      session_id=session_id,
      provider_id=provider_id,
      run_gen=next_gen,
      attachments=next_user.get("attachments"),
      timezone=next_user.get("timezone"),
      viewport=next_user.get("viewport"),
    )
    asyncio.create_task(coro)
    # Task owns the coroutine now — don't close it in the except.
    coro = None
  except Exception as exc:
    log.exception(
      "continuation scheduling failed chat_id=%s: %s", chat_id, exc,
    )
    # Clean up the broadcast we just registered so is_chat_running
    # doesn't report this chat as permanently active.
    if bc is not None:
      bc.mark_completed()
    # Close the orphan coroutine to silence the unawaited-coro warning.
    if coro is not None:
      coro.close()
    discard_starting(chat_id)
    # The continuation never started, and the gen bump above means the
    # outgoing turn's finally won't clear the durable run marker (it no
    # longer owns the generation). Clear it here so the chat isn't left
    # falsely "running" — a real terminal state, just reached via a
    # scheduling failure rather than a normal turn end.
    if chat_id:
      from app.database import SessionLocal
      _db = SessionLocal()
      try:
        _clear_run_status(_db, chat_id)
      finally:
        _db.close()


# Queue drain helpers — pre-bound to the chat-side callbacks so the
# call sites in _run_chat_impl stay short. `chat_queue.drain_and_release`
# takes `discard_starting` + `forget_chat` as kwargs so it doesn't
# import back into chat.py (avoids a cycle); these bound names just
# keep that ergonomic.

async def _drain_and_release(
  db: Session,
  chat_id: str,
  we_own_gen: bool,
) -> tuple[dict | None, list, str | None]:
  """Local helper around chat_queue.drain_and_release that binds the
  chat.py-owned discard_starting + forget_chat callbacks. Behavior is
  identical to ticket 033's pre-extract _drain_and_release."""
  return await chat_queue.drain_and_release(
    db, chat_id, we_own_gen,
    discard_starting=discard_starting,
    forget_chat=forget_chat,
  )


async def _close_browser_session(chat_id: str) -> None:
  """Close this chat's agent-browser session so Chrome doesn't linger.

  Best-effort: logs and swallows any error so cleanup never blocks a
  chat from completing. agent-browser must be on PATH (installed by the
  Dockerfile); if it's not (e.g. local dev outside the container), the
  call silently no-ops.
  """
  if not chat_id:
    return
  log = _get_logger()
  try:
    proc = await asyncio.create_subprocess_exec(
      "agent-browser", "--session", f"chat-{chat_id}", "close",
      stdout=asyncio.subprocess.DEVNULL,
      stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.wait(), timeout=5.0)
    log.info("agent-browser session closed chat_id=%s", chat_id)
  except FileNotFoundError:
    pass  # agent-browser not installed (local dev)
  except asyncio.TimeoutError:
    log.warning("agent-browser close timed out for chat %s", chat_id)
  except Exception as exc:
    log.warning("agent-browser close failed for chat %s: %s", chat_id, exc)


async def run_chat(
  messages: list[schemas.ChatMessage],
  chat_id: str = "",
  session_id: str | None = None,
  provider_id: str | None = None,
  run_gen: int | None = None,
  attachments: list[dict] | None = None,
  timezone: str | None = None,
  viewport: dict | None = None,
) -> None:
  """Runs a chat turn through the provider's SDK runner and publishes
  events to the chat's ChatBroadcast.  Caller must create the broadcast
  before calling.

  The entire body is wrapped in a top-level try/finally so the
  `_starting` guard is released even if setup code raises before we
  reach the runner.  Without that, a crash during setup leaves the
  chat stuck 'starting' until process restart.
  """
  try:
    await _run_chat_impl(
      messages, chat_id=chat_id, session_id=session_id,
      provider_id=provider_id, run_gen=run_gen,
      attachments=attachments, timezone=timezone, viewport=viewport,
    )
  finally:
    stopped_gen = _clear_after_terminal_generation.get(chat_id)
    clear_stopped_run = run_gen is not None and stopped_gen == run_gen
    if clear_stopped_run:
      _clear_after_terminal_generation.pop(chat_id, None)
    # Only clear _starting if we still own this generation.
    # A newer stop_chat_for may have bumped the generation and
    # taken ownership of _starting.
    if run_gen is None or current_run_generation(chat_id) == run_gen:
      discard_starting(chat_id)
    # Clear the durable marker only while this run still owns the
    # generation. A continuation handoff leaves it set for the next
    # turn. A Stop handoff is handled separately below after the final
    # sink save, because Stop deliberately bumps the generation first.
    # Stop bumps the generation before interrupting the SDK handle, so
    # the normal ownership branch above deliberately skips this run.
    # Once _run_chat_impl returns, its final sink save is complete and
    # the stopped generation may clear the marker unless a newer run
    # has already claimed it.
    should_clear_status = (
      run_gen is None
      or current_run_generation(chat_id) == run_gen
      or (
        clear_stopped_run
        and current_run_generation(chat_id) == run_gen + 1
      )
    )
    if chat_id and should_clear_status:
      from app.database import SessionLocal
      _db = SessionLocal()
      try:
        _clear_run_status(_db, chat_id)
      finally:
        _db.close()


async def _run_chat_impl(
  messages: list[schemas.ChatMessage],
  chat_id: str = "",
  session_id: str | None = None,
  provider_id: str | None = None,
  run_gen: int | None = None,
  attachments: list[dict] | None = None,
  timezone: str | None = None,
  viewport: dict | None = None,
) -> None:
  """Inner implementation of run_chat; see wrapper for lifecycle notes."""
  # Check if a newer send superseded this one while we were queued.
  # Do NOT discard _starting here — the newer run owns it.
  if run_gen is not None and current_run_generation(chat_id) != run_gen:
    log = _get_logger()
    log.info("run_chat aborted: generation mismatch chat_id=%s", chat_id)
    return

  from app.database import SessionLocal
  db = SessionLocal()
  log = _get_logger()
  settings = get_settings()
  user_message = messages[-1].content

  # Durable run marker: record that a turn is in flight so a process
  # death (OOM / SIGKILL) mid-turn is recoverable on the next boot
  # (see reconcile_interrupted_chats). The matching clear lives in
  # run_chat's finally, gated on the same generation-ownership check
  # that releases the _starting claim, so a continuation handoff keeps
  # the marker continuously set across the whole chain of turns.
  _mark_run_started(db, chat_id)

  # On the first message of a session, prepend the agent experience file so
  # the agent always sees it without needing a tool call.  The system prompt
  # (skill) stays static for API-level caching; the dynamic experience
  # travels here instead.
  if not session_id:
    experience_path = (
      Path(settings.data_dir) / "shared" / "agent-experience.md"
    )
    try:
      ctx = experience_path.read_text(encoding="utf-8").strip()
    except OSError:
      ctx = ""
    # Dynamic fields go at the end for cache efficiency.  Use safe
    # dict access on viewport so a malformed payload (missing keys,
    # wrong types) doesn't crash the agent spawn — skip the line
    # instead.
    provider_obj = get_provider(provider_id)
    provider_line = f"\nProvider: {provider_obj.name}"
    tz_line = f"\nTimezone: {timezone}" if timezone else ""
    vp_w = (viewport or {}).get("width")
    vp_h = (viewport or {}).get("height")
    vp_line = f"\nViewport: {vp_w}x{vp_h}" if vp_w and vp_h else ""
    if ctx or provider_line or tz_line or vp_line:
      # One-line pointer so the agent knows the block is a real file.
      # The seed's "About this file" section inside the block owns the
      # full spec (how to read, append, delete).
      # Codex models occasionally echo the entire <agent_experience>
      # block back to the user as their reply preamble — particularly
      # on long, prose-heavy first prompts. Claude doesn't do this.
      # The explicit "do not echo / quote / summarize" sentence is
      # what stops it. Keep the "See 'About this file'" pointer so
      # the agent still knows it can edit the underlying file when
      # appropriate.
      meta = (
        "The <agent_experience> block below is PRIVATE CONTEXT — a "
        "snapshot of /data/shared/agent-experience.md. Read it "
        "silently; do NOT echo, quote, or summarize it back to the "
        "user. See 'About this file' inside for how to read and "
        "update it."
      )
      user_message = (
        f"{meta}\n\n"
        f"<agent_experience>\n{ctx}"
        f"{provider_line}{tz_line}{vp_line}\n</agent_experience>"
        f"\n\n{user_message}"
      )

  bc = get_broadcast(chat_id)
  if bc is None:
    # The broadcast should have been pre-created by the caller
    # (send_message).  Creating it here as a fallback would orphan
    # any SSE clients already subscribed to the original broadcast.
    log.warning(
      "run_chat: no broadcast found for chat_id=%s, "
      "creating fallback", chat_id,
    )
    bc = create_broadcast(chat_id)
  set_active_broadcast(bc)

  owner = db.query(models.Owner).first()
  if not owner:
    bc.publish({"type": "error", "message": "No owner configured."})
    async with chat_queue.get_lock(chat_id):
      _clear_pending_queue(db, chat_id)
    bc.publish({"type": "done"})
    set_active_broadcast(None)
    bc.mark_completed()
    # Close the session before bailing — every other terminal path in
    # run_chat closes explicitly, and a misconfigured instance hitting
    # this branch on every turn would otherwise leak a connection each
    # time.
    db.close()
    return

  agent_token = auth.create_access_token(
    {"sub": owner.username},
    expires_delta=timedelta(hours=2),
  )

  # Build the base environment shared by all providers.
  scripts_dir = Path(__file__).parent.parent / "scripts"
  _safe_keys = {
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
    "USER", "LOGNAME", "SHELL", "XDG_RUNTIME_DIR",
  }
  base_env = {
    k: v for k, v in os.environ.items() if k in _safe_keys
  }
  base_env.update({
    "AGENT_TOKEN": agent_token,
    "API_BASE_URL": get_settings().api_base_url,
    "SCRIPTS_DIR": str(scripts_dir),
    "CHAT_ID": chat_id,
  })
  # Partner viewport (sent by the React shell on each turn). The agent
  # uses these when taking screenshots so the framing matches what the
  # partner actually sees — preview_shell.sh reads them, mini-app
  # screenshots in the seed/skill recipes use them.
  vp_w = (viewport or {}).get("width")
  vp_h = (viewport or {}).get("height")
  if vp_w and vp_h:
    base_env["VIEWPORT_WIDTH"] = str(vp_w)
    base_env["VIEWPORT_HEIGHT"] = str(vp_h)
  # Per-chat persistent Chrome profile for agent-browser. Default
  # (no AGENT_BROWSER_PROFILE) spins up a fresh ephemeral profile per
  # invocation — no SW registered, no warm cache, no localStorage
  # from prior agent screenshots in this chat. That means the agent's
  # "I checked the app and it renders" is a fresh-Chromium path that
  # never reproduces the partner's persistent-PWA-cache state.
  # Pointing the profile at /data/agent-browser-profiles/chat-<id>
  # gives the agent a stable cache to warm against across screenshots
  # within one chat (faster startup, repeated previews skip the SW
  # register + bundle fetch). PER-CHAT keying is load-bearing: two
  # parallel agent chats both launching Chrome against a shared dir
  # would race on the profile lock. The dir is created on first
  # agent-browser invocation by the CLI itself; we just point at it.
  chat_id_safe = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")
  base_env["AGENT_BROWSER_PROFILE"] = (
    f"/data/agent-browser-profiles/chat-{chat_id_safe}"
  )

  # Get the provider first — needed for auth check.
  provider = get_provider(provider_id)

  # Resolve effective agent settings (model, effort, ...) for this turn.
  # Per-chat overrides from `Chat.agent_settings_json` win over the
  # global default in /data/shared/agent-settings.json. The slash
  # picker (see frontend/.../SlashPicker.jsx) writes overrides via
  # PATCH /api/chats/{id}; the file remains the fallback every chat
  # starts from. Computed once here and threaded into the SDK runner
  # for each provider.
  chat_overrides: dict | None = None
  chat_row = None
  if chat_id:
    try:
      chat_row = (
        db.query(models.Chat).filter(models.Chat.id == chat_id).first()
      )
      if chat_row and chat_row.agent_settings_json:
        if isinstance(chat_row.agent_settings_json, dict):
          chat_overrides = chat_row.agent_settings_json
        elif isinstance(chat_row.agent_settings_json, str):
          # SQLite JSON columns occasionally surface as raw strings
          # depending on driver version. Decode defensively so a
          # str value doesn't silently disable overrides.
          try:
            chat_overrides = json.loads(chat_row.agent_settings_json)
          except (json.JSONDecodeError, TypeError):
            chat_overrides = None
    except Exception:
      log.exception(
        "failed to load per-chat agent_settings chat_id=%s", chat_id,
      )
  agent_settings = effective_agent_settings(
    settings.data_dir, chat_overrides, provider=provider_id,
  )

  # Snapshot-on-first-send: if the chat has no overrides yet (created
  # empty, never had the picker touched), freeze the current effective
  # settings onto the row so subsequent turns in THIS chat don't drift
  # when the global default changes in another chat. Without this, a
  # user who starts a Codex/high conversation and later picks Codex/low
  # in a sibling chat would silently get the new effort on their next
  # turn in the original — a real "why did my model change?" surprise.
  # The picker's PATCH path is the other commit point; this one covers
  # the "just typed and sent without opening the picker" path.
  # Invariant: keep this block await-free through the commit below. A
  # picker PATCH from another coroutine can only interleave at await
  # points; if one is added here, a concurrent PATCH could clobber the
  # user's pick.
  if chat_row is not None and chat_overrides is None:
    snapshot = {
      k: agent_settings[k]
      for k in ("model", "effort", "effort_by_provider")
      if agent_settings.get(k) is not None
    }
    if snapshot:
      chat_row.agent_settings_json = snapshot
      try:
        db.commit()
      except Exception:
        log.exception(
          "failed to snapshot initial agent_settings chat_id=%s", chat_id,
        )
        db.rollback()

  # Pre-flight: check that provider credentials exist before invoking
  # the SDK runner. Without this, the SDK fails with a cryptic error.
  auth_error = provider.check_auth(settings.data_dir)
  if auth_error:
    bc.publish({"type": "error", "message": auth_error})
    async with chat_queue.get_lock(chat_id):
      _clear_pending_queue(db, chat_id)
    bc.publish({"type": "done"})
    set_active_broadcast(None)
    bc.mark_completed()
    db.close()
    return
  data_dir = Path(settings.data_dir)
  cwd = str(data_dir) if data_dir.exists() else str(Path.cwd())

  # SDK dispatch: route both Claude and Codex through their official
  # Agent SDK runners.
  is_claude = provider.name == "Claude Code"
  is_codex = provider.name == "Codex"
  if is_codex:
    log.info(
      "chat start chat_id=%s provider=%s session=%s msg_len=%d sdk=codex",
      chat_id, provider.name, session_id or "new", len(user_message),
    )
    sdk_env = provider.build_env(
      base_env=base_env,
      data_dir=settings.data_dir,
      chat_id=chat_id,
    )
    sink = _ChatEventSink(bc, chat_id, db)
    runner_result: dict = {}
    try:
      from app.codex_sdk_runner import run_codex_sdk_turn
      runner_result = await run_codex_sdk_turn(
        user_message=user_message,
        session_id=session_id,
        base_env=sdk_env,
        cwd=cwd,
        chat_id=chat_id,
        bc=sink,
        pending_questions=questions._pending,
        db=db,
        agent_settings=agent_settings,
      )
      new_session_id = runner_result.get("session_id")
      err = runner_result.get("error")
      if not err and new_session_id and chat_id:
        chat_obj = db.query(models.Chat).filter(
          models.Chat.id == chat_id
        ).first()
        if chat_obj:
          chat_obj.session_id = new_session_id
          _safe_commit(db)
      if err:
        log.error("codex SDK error chat_id=%s: %s", chat_id, err)
      else:
        log.info(
          "chat done chat_id=%s cost_usd=%.4f sdk=codex",
          chat_id, runner_result.get("cost_usd") or 0.0,
        )
    except Exception as exc:
      log.exception("codex SDK turn failed chat_id=%s: %s", chat_id, exc)
      # Publish through the sink BEFORE finalize so the error lands
      # in the persisted assistant transcript, not just the live wire.
      sink.publish({"type": "error", "message": str(exc)})
      await sink.finalize()
      set_active_broadcast(None)
      # Mirror the success-path drain. Crucially, still check gen
      # ownership: if a concurrent Stop bumped gen, we must NOT
      # promote queued messages — the frontend's stop-handler will
      # resend them as one combined turn (see AGENTS.md Stop-chat
      # contract). Otherwise we'd double-fire the queue.
      we_own_gen = (
        run_gen is None or current_run_generation(chat_id) == run_gen
      )
      try:
        next_user, next_messages, next_session_id = await asyncio.wait_for(
          _drain_and_release(db, chat_id, we_own_gen), timeout=5.0,
        )
      except asyncio.TimeoutError:
        log.error(
          "queue drain timed out chat_id=%s (codex sdk except)", chat_id,
        )
        discard_starting(chat_id)
        next_user, next_messages, next_session_id = None, [], None
      if next_user:
        bc.publish({
          "type": "queued_turn_starting",
          "ts": next_user.get("ts"),
        })
      bc.publish({"type": "done"})
      bc.mark_completed()
      if next_user:
        _schedule_continuation(
          chat_id=chat_id,
          messages=next_messages,
          session_id=next_session_id,
          provider_id=provider_id,
          next_user=next_user,
        )
      db.close()
      return
    err = runner_result.get("error")
    if err:
      # Same R2-5 rationale: publish through sink before finalize so
      # the error is persisted alongside any partial response that
      # streamed before the failure.
      sink.publish({"type": "error", "message": err})
    await sink.finalize()
    set_active_broadcast(None)
    we_own_gen = (
      run_gen is None or current_run_generation(chat_id) == run_gen
    )
    try:
      next_user, next_messages, next_session_id = await asyncio.wait_for(
        _drain_and_release(db, chat_id, we_own_gen), timeout=5.0,
      )
    except asyncio.TimeoutError:
      log.error(
        "queue drain timed out chat_id=%s (codex sdk path)", chat_id,
      )
      discard_starting(chat_id)
      next_user, next_messages, next_session_id = None, [], None
    if next_user:
      bc.publish({
        "type": "queued_turn_starting",
        "ts": next_user.get("ts"),
      })
    # Error event already broadcast via sink.publish before finalize
    # (R2-5). Don't re-emit here — it would double-deliver to live
    # subscribers.
    bc.publish({
      "type": "done",
      "cost_usd": runner_result.get("cost_usd") or 0,
    })
    bc.mark_completed()
    if next_user:
      _schedule_continuation(
        chat_id=chat_id,
        messages=next_messages,
        session_id=next_session_id,
        provider_id=provider_id,
        next_user=next_user,

      )
    await _close_browser_session(chat_id)
    db.close()
    return

  if is_claude:
    log.info(
      "chat start chat_id=%s provider=%s session=%s msg_len=%d sdk=claude",
      chat_id, provider.name, session_id or "new", len(user_message),
    )
    sdk_env = provider.build_env(
      base_env=base_env,
      data_dir=settings.data_dir,
      chat_id=chat_id,
    )
    sink = _ChatEventSink(bc, chat_id, db)
    try:
      from app.claude_sdk_runner import run_claude_sdk_turn
      runner_result = await run_claude_sdk_turn(
        user_message=user_message,
        session_id=session_id,
        base_env=sdk_env,
        cwd=cwd,
        chat_id=chat_id,
        skill_text=_read_skill_text(),
        bc=sink,
        pending_questions=questions._pending,
        db=db,
        agent_settings=agent_settings,
      )
      new_session_id = runner_result.get("session_id")
      err = runner_result.get("error")
      if not err and new_session_id and chat_id:
        chat_obj = db.query(models.Chat).filter(
          models.Chat.id == chat_id
        ).first()
        if chat_obj:
          chat_obj.session_id = new_session_id
          _safe_commit(db)
      if err:
        log.error("claude SDK error chat_id=%s: %s", chat_id, err)
      else:
        log.info(
          "chat done chat_id=%s cost_usd=%.4f sdk=claude",
          chat_id, runner_result.get("cost_usd") or 0.0,
        )
    except Exception as exc:
      log.exception("claude SDK turn failed chat_id=%s: %s", chat_id, exc)
      # Publish through the sink BEFORE finalize so the error lands
      # in the persisted assistant transcript, not just the live wire.
      sink.publish({"type": "error", "message": str(exc)})
      await sink.finalize()
      set_active_broadcast(None)
      # Mirror the success-path drain. Crucially, still check gen
      # ownership: if a concurrent Stop bumped gen, we must NOT
      # promote queued messages — the frontend's stop-handler will
      # resend them as one combined turn (see AGENTS.md Stop-chat
      # contract). Otherwise we'd double-fire the queue.
      we_own_gen = (
        run_gen is None or current_run_generation(chat_id) == run_gen
      )
      try:
        next_user, next_messages, next_session_id = await asyncio.wait_for(
          _drain_and_release(db, chat_id, we_own_gen), timeout=5.0,
        )
      except asyncio.TimeoutError:
        log.error(
          "queue drain timed out chat_id=%s (claude sdk except)", chat_id,
        )
        discard_starting(chat_id)
        next_user, next_messages, next_session_id = None, [], None
      if next_user:
        bc.publish({
          "type": "queued_turn_starting",
          "ts": next_user.get("ts"),
        })
      bc.publish({"type": "done"})
      bc.mark_completed()
      if next_user:
        _schedule_continuation(
          chat_id=chat_id,
          messages=next_messages,
          session_id=next_session_id,
          provider_id=provider_id,
          next_user=next_user,
        )
      db.close()
      return
    if err:
      # Same R2-5 rationale: persist the error alongside any partial
      # response that streamed before the failure.
      sink.publish({"type": "error", "message": err})
    await sink.finalize()
    set_active_broadcast(None)
    we_own_gen = (
      run_gen is None or current_run_generation(chat_id) == run_gen
    )
    try:
      next_user, next_messages, next_session_id = await asyncio.wait_for(
        _drain_and_release(db, chat_id, we_own_gen), timeout=5.0,
      )
    except asyncio.TimeoutError:
      log.error(
        "queue drain timed out chat_id=%s (sdk path)", chat_id,
      )
      discard_starting(chat_id)
      next_user, next_messages, next_session_id = None, [], None
    if next_user:
      bc.publish({
        "type": "queued_turn_starting",
        "ts": next_user.get("ts"),
      })
    # Error event already broadcast via sink.publish before finalize
    # (R2-5). Don't re-emit here — it would double-deliver to live
    # subscribers.
    bc.publish({
      "type": "done",
      "cost_usd": runner_result.get("cost_usd") or 0,
    })
    bc.mark_completed()
    if next_user:
      _schedule_continuation(
        chat_id=chat_id,
        messages=next_messages,
        session_id=next_session_id,
        provider_id=provider_id,
        next_user=next_user,

      )
    await _close_browser_session(chat_id)
    db.close()
    return

  # Unknown provider — every supported provider is handled by an SDK
  # branch above. Surface a clear error rather than hanging silently.
  log.error(
    "unsupported provider chat_id=%s provider=%s — no SDK path",
    chat_id, provider.name,
  )
  bc.publish({
    "type": "error",
    "message": f"Provider {provider.name!r} has no supported runtime.",
  })
  set_active_broadcast(None)
  bc.publish({"type": "done"})
  bc.mark_completed()
  await _close_browser_session(chat_id)
  db.close()
