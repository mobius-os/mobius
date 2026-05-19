"""Agent chat via CLI subprocess.

Spawns the active provider's CLI tool, publishes events to a ChatBroadcast
so any number of SSE clients can subscribe.  Provider-specific logic
(command, args, output parsing) lives in providers.py.
"""

import asyncio
import json
import logging
import os
import time
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.broadcast import ChatBroadcast, create_broadcast, get_broadcast, set_active_broadcast
from app.config import get_settings
from app.events import process_event, build_assistant_message, finalize_blocks
from app.providers import get_provider


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
  handler = logging.FileHandler(log_dir / "chat.log", encoding="utf-8")
  handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
  )
  logger.addHandler(handler)
  logger.setLevel(
    logging.DEBUG if os.getenv("MOEBIUS_CHAT_DEBUG") else logging.INFO
  )
  return logger



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
  db.commit()


def _update_last_assistant_message(db: Session, chat_id: str, message: dict):
  """Updates the last assistant message in the chat (for streaming updates)."""
  if not chat_id:
    return
  from app.models import Chat
  chat = db.query(Chat).filter(Chat.id == chat_id).first()
  if not chat or not chat.messages:
    return
  msgs = list(chat.messages)
  if msgs and msgs[-1].get("role") == "assistant":
    msgs[-1] = message
  else:
    msgs.append(message)
  chat.messages = msgs
  db.commit()


async def _drain(stream: asyncio.StreamReader) -> None:
  """Reads and discards a subprocess stream to prevent pipe deadlock."""
  try:
    await stream.read()
  except Exception:
    pass


# Track active subprocesses per chat ID so we can stop them on demand.
_active_procs: dict[str, asyncio.subprocess.Process] = {}

# Guards against duplicate agent spawns.  send_message adds the chat_id
# before creating the background task; run_chat removes it in finally.
# This closes the TOCTOU gap between is_chat_running and proc registration.
_starting: set[str] = set()

# Per-run generation counter. Incremented by stop_chat_for so
# in-flight run_chat tasks for an older generation abort before
# spawning a subprocess.
_run_generation: dict[str, int] = {}

# Per-chat asyncio locks serialize read-modify-write on
# chat.pending_messages. The lock guards three operations that all
# touch the queue: append (POST /messages), cancel (DELETE /pending),
# and promote (turn-end drain). Without serialization, two of those
# fired concurrently can read the same snapshot and one commit
# overwrites the other — silently dropping queue entries.
#
# WeakValueDictionary lets entries collect when no caller is holding
# the lock, so the dict can't grow unbounded. Lookups are atomic from
# the asyncio scheduler's POV (no await between get + check + insert),
# so concurrent callers for the same chat get the same lock instance.
_queue_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
  weakref.WeakValueDictionary()
)


def get_queue_lock(chat_id: str) -> asyncio.Lock:
  """Returns the per-chat queue lock, creating it if needed."""
  lock = _queue_locks.get(chat_id)
  if lock is None:
    lock = asyncio.Lock()
    _queue_locks[chat_id] = lock
  return lock


def current_run_generation(chat_id: str) -> int:
  """Returns the current generation for a chat (0 if none)."""
  return _run_generation.get(chat_id, 0)


def get_active_procs() -> dict[str, asyncio.subprocess.Process]:
  """Accessor for the active-procs dict.  Prefer this over importing
  `_active_procs` directly so the internal structure can change without
  breaking debug/status routes and tests."""
  return _active_procs


def get_starting() -> set[str]:
  """Accessor for the starting-set.  See `get_active_procs` for why."""
  return _starting


def is_chat_running(chat_id: str) -> bool:
  """Returns True if an agent subprocess is running or starting for this chat."""
  if chat_id in _starting:
    return True
  proc = _active_procs.get(chat_id)
  if proc is not None and proc.returncode is None:
    return True
  bc = get_broadcast(chat_id)
  return bc is not None and bc.running


def mark_starting(chat_id: str) -> bool:
  """Atomically marks a chat as starting.  Returns False if already active."""
  if is_chat_running(chat_id):
    return False
  _starting.add(chat_id)
  return True


def discard_starting(chat_id: str) -> None:
  """Removes a chat_id from the starting set.  Call from send_message's
  error handler if the caller fails before scheduling run_chat — otherwise
  the chat_id leaks and the chat is stuck 'starting' until process restart."""
  _starting.discard(chat_id)


async def stop_chat(chat_id: str | None = None, db: Session = None) -> bool:
  """Kills the active subprocess for a chat, bumps its generation, and
  clears its pending queue so a queued continuation cannot auto-start
  after Stop. Session_id is preserved so the next message resumes."""
  killed = False
  if chat_id:
    targets = [chat_id]
  else:
    # Global stop must reach chats with NO proc yet (in _starting) or
    # only a live broadcast (queued continuation between turns). Union
    # all three lifecycle sources.
    from app.broadcast import _broadcasts
    targets = list({
      *_active_procs.keys(),
      *_starting,
      *(cid for cid, bc in _broadcasts.items() if bc.running),
    })
  for cid in targets:
    # Bump generation BEFORE killing so the dying run_chat's finally
    # detects ownership change and skips _promote_pending_messages /
    # continuation scheduling. Without this, Stop would still drain
    # the queue and start the next turn.
    _run_generation[cid] = _run_generation.get(cid, 0) + 1
    if db is not None:
      try:
        chat = db.query(models.Chat).filter(models.Chat.id == cid).first()
        if chat and chat.pending_messages:
          chat.pending_messages = []
          db.commit()
      except Exception:
        db.rollback()
    proc = _active_procs.pop(cid, None)
    if proc and proc.returncode is None:
      proc.kill()
      killed = True
      bc = get_broadcast(cid)
      if bc and bc.running:
        bc.mark_completed()
    _starting.discard(cid)
  return killed


async def stop_chat_for(chat_id: str, db: Session = None) -> bool:
  """Kills the agent subprocess for a specific chat.

  Bumps the generation counter so any in-flight run_chat task for
  the old generation aborts before spawning. Clears the pending
  queue so a queued continuation cannot auto-start after Stop.
  Waits for the process to die with a bounded timeout.
  """
  gen = _run_generation.get(chat_id, 0) + 1
  _run_generation[chat_id] = gen

  if db is not None:
    try:
      chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
      if chat and chat.pending_messages:
        chat.pending_messages = []
        db.commit()
    except Exception:
      db.rollback()

  proc = _active_procs.get(chat_id)
  if proc and proc.returncode is None:
    proc.kill()
    try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      # Proc didn't die — keep it tracked so it's not orphaned.
      return False
    # Wait succeeded — now safe to remove.
    _active_procs.pop(chat_id, None)

  bc = get_broadcast(chat_id)
  if bc and bc.running:
    bc.mark_completed()

  _starting.discard(chat_id)
  return True


def filter_post_question(event_type: str, suppress_text: bool) -> tuple[bool, bool]:
  """Decides whether a parsed event should be broadcast to SSE clients.

  Returns (publish, new_suppress_text). After a question event,
  suppresses text, tool_output, and tool_end (Claude's auto-answer
  fallback). TODO: unnecessary with SDK canUseTool approach.
  """
  if event_type == "question":
    return True, True
  if suppress_text and event_type in ("text", "tool_output", "tool_end"):
    return False, True
  return True, suppress_text


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


def _promote_pending_messages_locked(
  db: Session,
  chat_id: str,
) -> tuple[list[schemas.ChatMessage], dict | None, str | None]:
  """Inner promote logic. PRECONDITION: caller holds the per-chat queue
  lock. This sync variant exists so the finally block in _run_chat_impl
  can do its 'late-drain + release _starting' critical section atomically
  under a single lock acquisition without needing re-entrant locks.

  Returns (next_messages, first_pending, session_id) on success.
  Returns ([], None, session_id) when the pending queue is empty or
  when next_messages construction fails (malformed transcript entry).
  """
  if not chat_id:
    return [], None, None
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  if not chat:
    return [], None, None
  # Refresh inside the lock so we see commits from any append or
  # cancel that completed while we waited.
  db.refresh(chat)
  pending = list(chat.pending_messages or [])
  if not pending:
    return [], None, chat.session_id

  existing = list(chat.messages or [])
  first_pending = pending[0]
  # Build next_messages BEFORE committing so a malformed transcript
  # entry can't silently consume a pending turn. If construction
  # raises, log and leave the queue intact for retry.
  try:
    next_messages = [
      schemas.ChatMessage(
        role=m.get("role", "user"),
        content=m.get("content", "") or "",
      )
      for m in existing
    ]
    next_messages.append(
      schemas.ChatMessage(
        role=first_pending.get("role", "user"),
        content=first_pending.get("content", "") or "",
      )
    )
  except Exception:
    _get_logger().exception(
      "promote: next_messages construction failed chat_id=%s — "
      "leaving pending queue intact", chat_id,
    )
    return [], None, chat.session_id

  chat.messages = existing + [first_pending]
  chat.pending_messages = pending[1:]
  chat.updated_at = datetime.now(UTC)
  db.commit()

  return next_messages, first_pending, chat.session_id


async def _promote_pending_messages(
  db: Session,
  chat_id: str,
) -> tuple[list[schemas.ChatMessage], dict | None, str | None]:
  """Atomically promotes the head of the pending queue into the transcript.

  Held under the per-chat queue lock so the read-modify-write on
  pending_messages doesn't race with append (POST /messages) or
  cancel (DELETE /pending/{ts}).

  This function does NOT claim _starting — the caller is responsible
  for ensuring exclusive promotion (e.g., via mark_starting before
  call in stale-pending path, or by virtue of being the only finally
  block for a given run in the turn-end path). Adding mark_starting
  here was a round-7 over-engineering that broke the finally path:
  _starting still contains the original send's claim when the finally
  fires, so the in-promote mark_starting always returned False and
  no queued turn ever got promoted in production.
  """
  if not chat_id:
    return [], None, None
  async with get_queue_lock(chat_id):
    return _promote_pending_messages_locked(db, chat_id)


def _clear_pending_queue(db: Session, chat_id: str) -> None:
  """Empties the pending_messages queue for a chat. Used on terminal
  setup errors (no owner, missing auth) so queued messages don't pile
  up repeating the same error."""
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
    next_gen = current_run_generation(chat_id) + 1
    _run_generation[chat_id] = next_gen
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
    _starting.discard(chat_id)


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


async def _drain_and_release(
  db: Session,
  chat_id: str,
  we_own_gen: bool,
) -> tuple[dict | None, list, str | None]:
  """End-of-turn queue drain. Returns (next_user, next_messages,
  next_session_id) for the caller to publish + schedule.

  Under the per-chat queue lock:
    - Promotes the head of pending_messages (if any).
    - If nothing to promote AND we_own_gen, releases _starting so any
      subsequent POST sees is_chat_running=False and starts a fresh run.

  Doing this in a single locked critical section closes the race
  between the run_chat finally and a POST that arrives in the window
  after the subprocess exits but before _starting is released. Both
  ends serialize on the same lock; whichever side wins ordering, the
  message is either promoted here or POST takes the start path.

  When we_own_gen is False (Stop bumped the gen), we must not promote
  or release _starting — the newer owner (Stop, or the continuation
  it scheduled) is responsible for those.
  """
  if not we_own_gen:
    return None, [], None
  async with get_queue_lock(chat_id):
    next_messages, first_pending, next_session_id = (
      _promote_pending_messages_locked(db, chat_id)
    )
    if first_pending is None:
      _starting.discard(chat_id)
    return first_pending, next_messages, next_session_id


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
  """Runs the provider CLI as a subprocess and publishes events to the
  chat's ChatBroadcast.  Caller must create the broadcast before calling.

  The entire body is wrapped in a top-level try/finally so the
  `_starting` guard is released even if setup code raises before we
  reach the subprocess.  Without that, a crash during setup leaves the
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
    if run_gen is None or _run_generation.get(chat_id, 0) == run_gen:
      _starting.discard(chat_id)


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
  if run_gen is not None and _run_generation.get(chat_id, 0) != run_gen:
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
      meta = (
        "The <agent_experience> block below is a snapshot of "
        "/data/shared/agent-experience.md — see 'About this file' "
        "inside for how to read and update it."
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

  # Get the provider first — needed for auth check.
  provider = get_provider(provider_id)

  # Pre-flight: check that provider credentials exist before spawning
  # the CLI. Without this, the CLI fails with a cryptic error.
  auth_error = provider.check_auth(settings.data_dir)
  if auth_error:
    bc.publish({"type": "error", "message": auth_error})
    _clear_pending_queue(db, chat_id)
    bc.publish({"type": "done"})
    set_active_broadcast(None)
    bc.mark_completed()
    db.close()
    return
  result = provider.build(
    user_message=user_message,
    session_id=session_id,
    base_env=base_env,
    data_dir=settings.data_dir,
    chat_id=chat_id,
  )

  data_dir = Path(settings.data_dir)
  cwd = str(data_dir) if data_dir.exists() else str(Path.cwd())

  log.info(
    "chat start chat_id=%s provider=%s session=%s msg_len=%d",
    chat_id, provider.name, session_id or "new", len(user_message),
  )
  proc = None
  try:
    proc = await asyncio.create_subprocess_exec(
      *result.cmd,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      cwd=cwd,
      env=result.env,
      # 1 MB limit — protects against runaway tool output flooding
      # the SSE queue.  Normal CLI lines are well under 100 KB.
      limit=1024 * 1024,
    )
    if chat_id:
      _active_procs[chat_id] = proc

    stderr_task = asyncio.ensure_future(_drain(proc.stderr))
    # Ordered blocks list — preserves interleaved text/tool order.
    assistant_blocks = []
    session_captured = False
    last_save_time = 0.0
    suppress_text = False
    _DB_SAVE_INTERVAL = 1.0  # seconds between incremental DB saves

    # Default 1 hour, clamped to [30, 7200].
    _MAX_RUNTIME_SECS = max(
      30,
      min(int(os.environ.get("CHAT_TIMEOUT_SECS", "3600")), 7200),
    )
    try:
      async with asyncio.timeout(_MAX_RUNTIME_SECS):
        async for raw in proc.stdout:
          line = raw.decode("utf-8", errors="replace").strip()
          if not line:
            continue

          parsed = provider.parse_line(line)
          if parsed is None:
            log.debug("skipped: %.200s", line)
            continue

          # parse_line may return a single dict or a list.
          events = (
            parsed if isinstance(parsed, list) else [parsed]
          )

          # Capture session_id from provider-normalized event.
          if not session_captured:
            for evt in events:
              if evt.get("type") == "session_init":
                sid = evt.get("session_id")
                if sid and chat_id:
                  from app.models import Chat
                  chat_obj = (
                    db.query(Chat)
                    .filter(Chat.id == chat_id)
                    .first()
                  )
                  if chat_obj:
                    chat_obj.session_id = sid
                    db.commit()
                session_captured = True
                break

          for event in events:
            if event.get("type") == "session_init":
              continue  # internal event, don't broadcast
            event_type = event.get("type")
            log.debug("event type=%s", event_type)

            if event_type == "done":
              log.info(
                "chat done chat_id=%s cost_usd=%.4f",
                chat_id, event.get("cost_usd", 0),
              )
              break
            elif event_type == "error":
              log.error(
                "provider error: %s", event.get("message"),
              )

            # AskUserQuestion auto-answers with is_error in -p mode.
            # Suppress Claude's fallback text and the synthetic tool
            # result. TODO: with the Agent SDK's canUseTool callback
            # the auto-answer never fires and this is unnecessary.
            publish, suppress_text = filter_post_question(
              event_type, suppress_text,
            )
            if not publish:
              continue

            bc.publish(event)

            # Accumulate blocks and throttle DB saves.
            save_needed = process_event(
              event, assistant_blocks,
            )
            if save_needed and chat_id:
              now = time.monotonic()
              if (now - last_save_time >= _DB_SAVE_INTERVAL
                  or event_type in (
                    "tool_start", "tool_end", "error",
                    "question",
                  )):
                last_save_time = now
                _update_last_assistant_message(
                  db, chat_id,
                  build_assistant_message(assistant_blocks),
                )
          else:
            continue
          break  # break outer loop when inner breaks on "done"
        else:
          # stdout exhausted without "done" — CLI exited early.
          log.warning("CLI exited without done event")
    except asyncio.TimeoutError:
      log.warning(
        "chat timeout after %ds, killing subprocess",
        _MAX_RUNTIME_SECS,
      )
      proc.kill()
      await asyncio.shield(proc.wait())
      bc.publish({
        "type": "error",
        "message": (
          f"Agent timed out after {_MAX_RUNTIME_SECS} seconds."
          " Use the stop button and try again."
        ),
      })

    finally:
      _finalize_response(db, chat_id, assistant_blocks)
      if _active_procs.get(chat_id) is proc:
        _active_procs.pop(chat_id, None)
      set_active_broadcast(None)
      # Only drain the queue if we still own this generation. A Stop
      # bumps the generation and clears pending_messages — we must not
      # promote/continue after Stop.
      we_own_gen = (
        run_gen is None or _run_generation.get(chat_id, 0) == run_gen
      )
      # `_drain_and_release` takes the per-chat queue lock with no
      # internal timeout — if another coroutine holds it (e.g. a
      # concurrent POST appending a queued message), the finally
      # block hangs HERE, before bc.publish(done) + mark_completed.
      # Result: `_active_procs` is empty (proc already popped above)
      # but broadcast stays `running=True` forever. Zombie chat.
      # Cap the wait so any contention surfaces as a logged event
      # and we still publish `done` + complete the broadcast.
      try:
        next_user, next_messages, next_session_id = await asyncio.wait_for(
          _drain_and_release(db, chat_id, we_own_gen), timeout=5.0,
        )
      except asyncio.TimeoutError:
        log.error(
          "queue drain timed out chat_id=%s; completing broadcast", chat_id,
        )
        _starting.discard(chat_id)
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
      stderr_task.cancel()
      try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
      except asyncio.TimeoutError:
        log.warning("subprocess did not exit cleanly, killing")
        proc.kill()

  except Exception as exc:
    if proc is not None and _active_procs.get(chat_id) is proc:
      _active_procs.pop(chat_id, None)
    log.exception("run_chat failed chat_id=%s: %s", chat_id, exc)
    _finalize_response(db, chat_id, assistant_blocks)
    bc.publish({"type": "error", "message": str(exc)})
    set_active_broadcast(None)
    # Even on error, drain the queue so queued messages aren't stranded.
    # The user's next turn shouldn't be silently dropped because the
    # previous turn crashed (e.g. transient network/CLI issue).
    we_own_gen = (
      run_gen is None or _run_generation.get(chat_id, 0) == run_gen
    )
    # Same drain-timeout guard as the success path — see comment above.
    try:
      next_user, next_messages, next_session_id = await asyncio.wait_for(
        _drain_and_release(db, chat_id, we_own_gen), timeout=5.0,
      )
    except asyncio.TimeoutError:
      log.error(
        "queue drain timed out (error path) chat_id=%s; completing broadcast",
        chat_id,
      )
      _starting.discard(chat_id)
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
  finally:
    # Close agent-browser session exactly once, regardless of which
    # code path completed/errored.  _close_browser_session is a no-op
    # when agent-browser isn't installed (local dev).
    await _close_browser_session(chat_id)
    db.close()
