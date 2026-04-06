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
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.broadcast import ChatBroadcast, create_broadcast, get_broadcast, set_active_broadcast
from app.config import get_settings
from app.providers import get_provider


def _get_logger() -> logging.Logger:
  """Returns a logger that writes to the data/logs/chat.log file."""
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
  logger.setLevel(logging.DEBUG)
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


def is_chat_running(chat_id: str) -> bool:
  """Returns True if an agent subprocess is running for this chat."""
  proc = _active_procs.get(chat_id)
  if proc is not None and proc.returncode is None:
    return True
  # Also check the broadcast — it may be running even if proc was cleaned up.
  bc = get_broadcast(chat_id)
  return bc is not None and bc.running


async def stop_chat(chat_id: str | None = None, db: Session = None) -> bool:
  """Kills the active subprocess for a chat and clears its session_id
  so the next message starts a fresh session with full history."""
  killed = False
  targets = [chat_id] if chat_id else list(_active_procs.keys())
  for cid in targets:
    proc = _active_procs.pop(cid, None)
    if proc and proc.returncode is None:
      proc.kill()
      killed = True
      # Keep session_id so the next message resumes with context.
      # The CLI's --resume flag will pick up where we left off.
      # Mark the broadcast completed so subscribers unblock.
      bc = get_broadcast(cid)
      if bc and bc.running:
        bc.mark_completed()
  return killed


async def stop_chat_for(chat_id: str, db: Session = None) -> bool:
  """Kills the agent subprocess for a specific chat."""
  return await stop_chat(chat_id, db=db)


def _process_event(event: dict, assistant_blocks: list) -> bool:
  """Accumulates a parsed event into the assistant blocks list.

  Updates assistant_blocks in place with text content, tool starts,
  tool input/output, and tool completion markers.  Returns True if the
  blocks changed and a DB save may be warranted.
  """
  event_type = event.get("type")

  if event_type == "text":
    content = event.get("content", "")
    # Append to last text block or create new one.
    if (assistant_blocks
        and assistant_blocks[-1].get("type") == "text"):
      assistant_blocks[-1]["content"] += content
    else:
      assistant_blocks.append(
        {"type": "text", "content": content}
      )
    return True

  if event_type == "tool_start":
    assistant_blocks.append({
      "type": "tool",
      "tool": event.get("tool", ""),
      "input": event.get("input", ""),
      "output": "",
      "status": "running",
    })
    return True

  if event_type == "tool_input":
    # Backfill input summary from the assistant event (arrives after
    # content_block_start which created the tool block).  Match the
    # earliest tool block without input — the assistant event lists
    # tools in order, matching creation order.
    for blk in assistant_blocks:
      if blk.get("type") == "tool" and not blk.get("input"):
        blk["input"] = event.get("input", "")
        break
    return True

  if event_type == "tool_output":
    for blk in reversed(assistant_blocks):
      if (blk.get("type") == "tool"
          and blk.get("status") != "done"):
        blk["output"] = event.get("content", "")
        break
    return True

  if event_type == "tool_end":
    for blk in reversed(assistant_blocks):
      if (blk.get("type") == "tool"
          and blk.get("status") != "done"):
        blk["status"] = "done"
        break
    return True

  return False


def _build_assistant_message(
  assistant_blocks: list,
) -> dict:
  """Converts accumulated blocks into a message dict for DB storage."""
  all_text = "".join(
    b["content"] for b in assistant_blocks
    if b.get("type") == "text"
  )
  return {
    "role": "assistant",
    "content": all_text,
    "blocks": assistant_blocks,
  }


def _finalize_response(
  db: Session,
  chat_id: str,
  assistant_blocks: list,
) -> None:
  """End-of-response cleanup: force-complete tool blocks and save."""
  if not assistant_blocks:
    return
  # Any tool block still marked 'running' at this point means its
  # tool_end event was missed (timing, reconnect, or early exit).
  # Force them to 'done' so the UI doesn't show a permanent spinner.
  for blk in assistant_blocks:
    if (blk.get("type") == "tool"
        and blk.get("status") == "running"):
      blk["status"] = "done"
  _update_last_assistant_message(
    db, chat_id, _build_assistant_message(assistant_blocks),
  )


async def run_chat(
  messages: list[schemas.ChatMessage],
  chat_id: str = "",
  session_id: str | None = None,
  attachments: list[dict] | None = None,
  timezone: str | None = None,
) -> None:
  """Runs the provider CLI as a subprocess and publishes events to the
  chat's ChatBroadcast.  Caller must create the broadcast before calling."""
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
    # Dynamic fields go at the end for cache efficiency.
    tz_line = f"\nTimezone: {timezone}" if timezone else ""
    if ctx or tz_line:
      user_message = (
        f"<agent_context>\n{ctx}{tz_line}\n</agent_context>"
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

  # Pre-flight: check that provider credentials exist before spawning
  # the CLI. Without this, the CLI fails with a cryptic error.
  creds_path = (
    Path(settings.data_dir)
    / "cli-auth" / "claude" / ".credentials.json"
  )
  if not creds_path.exists():
    bc.publish({
      "type": "error",
      "message": (
        "Not signed in. Open Settings and connect "
        "under AI provider."
      ),
    })
    bc.publish({"type": "done"})
    set_active_broadcast(None)
    bc.mark_completed()
    db.close()
    return

  # Get the active provider and build its command.
  provider = get_provider()
  result = provider.build(
    user_message=user_message,
    session_id=session_id,
    base_env=base_env,
    data_dir=settings.data_dir,
  )

  data_dir = Path(settings.data_dir)
  cwd = str(data_dir) if data_dir.exists() else str(Path.cwd())

  log.info(
    "chat start provider=%s session=%s msg_len=%d",
    provider.name, session_id or "new", len(user_message),
  )
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
    _DB_SAVE_INTERVAL = 1.0  # seconds between incremental DB saves

    # Clamped to [30, 3600] to prevent foot-guns: 0 kills every chat
    # instantly; multi-million values hang forever.
    _MAX_RUNTIME_SECS = max(
      30,
      min(int(os.environ.get("CHAT_TIMEOUT_SECS", "300")), 3600),
    )
    try:
      async with asyncio.timeout(_MAX_RUNTIME_SECS):
        async for raw in proc.stdout:
          line = raw.decode("utf-8", errors="replace").strip()
          if not line:
            continue

          # Capture session_id from the CLI init event.
          if not session_captured:
            try:
              raw_event = json.loads(line)
              if (raw_event.get("type") == "system"
                  and raw_event.get("subtype") == "init"):
                sid = raw_event.get("session_id")
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
            except json.JSONDecodeError:
              pass

          parsed = provider.parse_line(line)
          if parsed is None:
            log.debug("skipped: %.200s", line)
            continue

          # parse_line may return a single dict or a list.
          events = (
            parsed if isinstance(parsed, list) else [parsed]
          )
          for event in events:
            event_type = event.get("type")
            log.debug("event type=%s", event_type)

            if event_type == "done":
              log.info(
                "chat done cost_usd=%.4f",
                event.get("cost_usd", 0),
              )
              bc.publish({"type": "done"})
              break
            elif event_type == "error":
              log.error(
                "provider error: %s", event.get("message"),
              )

            bc.publish(event)

            # Accumulate blocks and throttle DB saves.
            save_needed = _process_event(
              event, assistant_blocks,
            )
            if save_needed and chat_id:
              now = time.monotonic()
              if (now - last_save_time >= _DB_SAVE_INTERVAL
                  or event_type in (
                    "tool_start", "tool_end", "error",
                  )):
                last_save_time = now
                _update_last_assistant_message(
                  db, chat_id,
                  _build_assistant_message(assistant_blocks),
                )
          else:
            continue
          break  # break outer loop when inner breaks on "done"
        else:
          # stdout exhausted without "done" — CLI exited early.
          log.warning("CLI exited without done event")
          bc.publish({"type": "done"})
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
      bc.publish({"type": "done"})

    finally:
      _finalize_response(db, chat_id, assistant_blocks)
      _active_procs.pop(chat_id, None)
      set_active_broadcast(None)
      bc.mark_completed()
      stderr_task.cancel()
      try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
      except asyncio.TimeoutError:
        log.warning("subprocess did not exit cleanly, killing")
        proc.kill()

  except Exception as exc:
    _active_procs.pop(chat_id, None)
    log.exception("run_chat failed: %s", exc)
    _finalize_response(db, chat_id, assistant_blocks)
    bc.publish({"type": "error", "message": str(exc)})
    bc.publish({"type": "done"})
    set_active_broadcast(None)
    bc.mark_completed()
  finally:
    db.close()
