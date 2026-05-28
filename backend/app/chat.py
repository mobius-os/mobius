"""Agent chat via the official provider SDKs.

Routes each chat turn through the SDK-backed runner for the matching
provider (`claude_sdk_runner.py`, `codex_sdk_runner.py`) and bridges the
runner's events onto the chat's `ChatBroadcast` so any number of SSE
clients can subscribe.  Provider env / auth wiring lives in
`providers.py`; the subprocess fallback that used to live here is gone.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
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
from app.runner_registry import RunnerKind, registry
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


@dataclass
class SubprocessHandle:
  """Registry handle for one subprocess-backed chat turn."""

  chat_id: str
  proc: asyncio.subprocess.Process
  kind: RunnerKind = RunnerKind.SUBPROCESS

  async def stop(self, timeout: float = 2.0) -> bool:
    """Stops the subprocess and waits up to `timeout` seconds."""
    try:
      if self.proc.returncode is None:
        self.proc.kill()
      await asyncio.wait_for(self.proc.wait(), timeout=timeout)
      return True
    except asyncio.CancelledError:
      raise
    except asyncio.TimeoutError:
      _get_logger().warning(
        "Subprocess stop timed out chat_id=%s", self.chat_id,
      )
      return False
    except Exception:
      _get_logger().exception(
        "Subprocess stop failed chat_id=%s", self.chat_id,
      )
      return False


def _update_last_assistant_message(db: Session, chat_id: str, message: dict) -> bool:
  """Updates the last assistant message in the chat (for streaming updates)."""
  if not chat_id:
    return True
  from app.models import Chat
  chat = db.query(Chat).filter(Chat.id == chat_id).first()
  if not chat or not chat.messages:
    return True
  msgs = list(chat.messages)
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
    msgs[-1] = message
  else:
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

  DB-lock-drop behavior: broadcasts still happen even when the DB
  write path hits `_safe_commit()` returning False (for example a
  transient SQLite lock). `publish()` returns that commit outcome so
  callers can observe a broadcast-without-persist gap if they care.
  """

  _SAVE_INTERVAL_SECS = 1.0
  # Subset of app.events.EventType that forces a sync DB commit so the
  # user does not reconnect into a stale transcript mid-turn.
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

  def publish(self, event: ChatEvent) -> bool:
    """Publishes an event and returns whether persistence succeeded.

    Live broadcast is best-effort independent from persistence. When
    `_safe_commit()` returns False because the database is locked, the
    event is still published to SSE subscribers and the boolean return
    exposes that observability gap to the caller.
    """
    event_type = event.get("type")
    commit_ok = True

    # Accumulate the event into assistant_blocks and decide whether a
    # save is due (immediate for save-triggering types, throttled
    # otherwise).
    accumulated = process_event(event, self.assistant_blocks)
    needs_save = accumulated and self.chat_id and (
      event_type in self._IMMEDIATE_SAVE_TYPES
      or time.monotonic() - self._last_save >= self._SAVE_INTERVAL_SECS
    )

    # AskUserQuestion is the one event that MUST persist before the
    # broadcast: the frontend renders the question card the moment the
    # broadcast lands, and a fast user Submit races the DB write
    # otherwise. _apply_answers_to_last_question (chats_stream.py)
    # iterates chat.messages looking for the latest assistant
    # message's question block — if the runner hasn't written it yet,
    # the lookup silently returns False, the SDK future resolves
    # without persisted answers, and the answer disappears on the
    # next runner writeback (which has no block.answers to carry over).
    if needs_save and event_type == "question":
      self._last_save = time.monotonic()
      commit_ok = _update_last_assistant_message(
        self.db, self.chat_id,
        build_assistant_message(self.assistant_blocks),
      )

    self.bc.publish(event)

    # done: capture cost.
    if event_type == "done":
      self.cost_usd = event.get("cost_usd")

    # All other event types save AFTER broadcast — preserves streaming
    # latency for text events (most don't trigger save anyway due to
    # the 1s throttle).
    if needs_save and event_type != "question":
      self._last_save = time.monotonic()
      commit_ok = _update_last_assistant_message(
        self.db, self.chat_id,
        build_assistant_message(self.assistant_blocks),
      )
    return commit_ok

  def finalize(self) -> None:
    """Write the final assistant message snapshot to the DB."""
    if self.chat_id and self.assistant_blocks:
      _finalize_response(self.db, self.chat_id, self.assistant_blocks)


_SKILL_TEXT_CACHE: str | None = None


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
  bump_run_generation(chat_id)
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
  for handle in registry.get_handles(chat_id):
    stopped = await handle.stop(timeout=2.0)
    if not stopped:
      log.warning(
        "stop_chat_for: handle.stop() timed out for chat %s "
        "(%s) — unregistering anyway to converge state",
        chat_id, handle.kind,
      )
      all_stopped = False
    registry.unregister(chat_id, handle.kind)
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
    # Only clear _starting if we still own this generation.
    # A newer stop_chat_for may have bumped the generation and
    # taken ownership of _starting.
    if run_gen is None or current_run_generation(chat_id) == run_gen:
      discard_starting(chat_id)


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
      sink.finalize()
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
    sink.finalize()
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
      sink.finalize()
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
    sink.finalize()
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
