"""Routes for agent message sending and SSE streaming."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response
from sqlalchemy.orm import Session

from app import models, schemas
from app.broadcast import create_broadcast, get_broadcast
from app.chat import discard_starting, is_chat_running, mark_starting, run_chat
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner

router = APIRouter(prefix="/api/chats", tags=["chats"])

# Keepalive interval for the SSE stream to prevent proxy timeouts.
_KEEPALIVE_INTERVAL = 30  # seconds


def _safe_upload_path(path_str: str, data_dir: str) -> str | None:
  """Returns the resolved path if it lives within data_dir, None otherwise.

  Validates upload paths from the DB before passing them to the CLI so a
  tampered record cannot redirect the agent to read credentials or other
  sensitive files outside /data/.
  """
  try:
    p = FilePath(path_str).resolve()
    allowed = FilePath(data_dir).resolve()
    if not str(p).startswith(str(allowed) + '/'):
      logging.getLogger(__name__).warning(
        "Upload path outside data dir, skipping: %s", path_str
      )
      return None
    return str(p)
  except Exception:
    return None


def _sse(data: dict) -> str:
  """Formats a dict as a Server-Sent Events data line."""
  return f"data: {json.dumps(data)}\n\n"


def _content_with_uploads(chat: models.Chat, body: schemas.SendMessage) -> str:
  """Returns message content with the session upload notice appended."""
  settings = get_settings()
  content = body.content
  if chat.uploads:
    safe_entries = []
    for f in chat.uploads:
      safe = _safe_upload_path(f['path'], settings.data_dir)
      if safe is not None:
        safe_entries.append(
          f"- {f['name']} → {safe}"
          f" ({f.get('mime_type', 'unknown')}, {round(f['size'] / 1024)} KB)"
        )
    if safe_entries:
      lines = "\n".join(safe_entries)
      content += f"\n\n[Files in this session:\n{lines}]"
  return content


def _ensure_unique_ts(new_msg: dict, pending: list[dict]) -> None:
  """Bumps new_msg['ts'] so it's strictly greater than every ts in pending.

  Two sends inside the same millisecond would otherwise collide,
  producing duplicate React keys client-side and making DELETE-by-ts
  ambiguous (it would remove all matching entries). The id only needs
  to be unique within the queue, not globally — keeping it as an int
  millisecond timestamp preserves human-readable ordering.
  """
  if not pending:
    return
  max_ts = max((m.get("ts", 0) for m in pending), default=0)
  if new_msg.get("ts", 0) <= max_ts:
    new_msg["ts"] = max_ts + 1


async def _append_to_pending(
  chat: models.Chat, body: schemas.SendMessage, db: Session,
) -> dict:
  """Appends a queued message and commits. Returns the stored dict.

  Serialized per chat via the queue lock from app.chat — concurrent
  POSTs (and concurrent DELETE/promote) are made safe by refreshing
  the chat row from the DB inside the lock so each caller sees the
  committed state of the previous one.

  AskUserQuestion answers: when the body carries `answers` (user is
  submitting a hidden answer to a question), the answers are written
  into the LAST assistant message's question block inside this same
  lock + commit. Applying answers BEFORE the refresh would be
  overwritten by the refresh; applying them OUTSIDE the lock could
  race with a concurrent send. So they ride along with the queue
  append, atomic.
  """
  from app.chat import get_queue_lock
  async with get_queue_lock(chat.id):
    db.refresh(chat)
    # Apply answers AFTER refresh so we don't lose the write.
    _apply_answers_to_last_question(chat, body.answers)
    pending = list(chat.pending_messages or [])
    new_msg = _user_message_from_body(chat, body)
    _ensure_unique_ts(new_msg, pending)
    pending.append(new_msg)
    chat.pending_messages = pending
    chat.updated_at = datetime.now(UTC)
    db.commit()
  return new_msg


def _queued_response(new_msg: dict, position: int) -> JSONResponse:
  """Standard 202 response for a queued message."""
  return JSONResponse(
    status_code=202,
    content={
      "status": "queued",
      "position": position,
      "ts": new_msg["ts"],
    },
  )


def _user_message_from_body(
  chat: models.Chat,
  body: schemas.SendMessage,
) -> dict:
  """Builds the durable user message payload for a send request."""
  user_msg = {
    "role": "user",
    "content": _content_with_uploads(chat, body),
    "ts": int(time.time() * 1000),
  }
  if body.hidden:
    user_msg["hidden"] = True
  if body.attachments:
    user_msg["attachments"] = body.attachments
  if body.timezone:
    user_msg["timezone"] = body.timezone
  if body.viewport:
    user_msg["viewport"] = body.viewport
  return user_msg


def _apply_answers_to_last_question(
  chat: models.Chat, answers: dict | None,
) -> bool:
  """Writes `answers` into the LAST assistant message's question block.

  Atomic with the rest of the POST /messages transaction — when the
  user submits a question-card answer, the answers + the hidden user
  message + the new turn start happen in one DB commit, eliminating
  the race that used to leave answers missing on mid-stream remounts.

  Returns True if a question block was found and updated.
  """
  if not answers:
    return False
  msgs = list(chat.messages or [])
  for msg in reversed(msgs):
    if msg.get("role") != "assistant":
      continue
    for block in reversed(msg.get("blocks") or []):
      if block.get("type") == "question":
        block["answers"] = answers
        chat.messages = msgs  # rebind so SQLAlchemy detects JSON mutation
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(chat, "messages")
        return True
  return False


@router.post("/{chat_id}/messages", status_code=202)
async def send_message(
  body: schemas.SendMessage,
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Saves the user message, starts the agent as a background task,
  and returns 202 immediately.  The client streams via GET /stream."""
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found.")

  # Atomic answer persistence: when the user is submitting a hidden
  # answer to an AskUserQuestion (body.hidden=true with body.answers),
  # write those answers into the existing question block BEFORE any
  # branching. This eliminates the dual-request race (old flow: a
  # separate POST /question-answers wrote them, racing with the GET on
  # remount). The actual commit happens in whichever branch below runs
  # (queue path, stale-pending path, or fresh-start path) — they each
  # call db.commit() on the chat row.
  _apply_answers_to_last_question(chat, body.answers)

  # Queue path: agent is running OR stale pending exists from a
  # previous crash. Appending the new send at the END of pending
  # preserves chronological order. When pending was stale (server
  # crashed mid-turn), we additionally spawn a run that drains the
  # queue from the head, so the queued messages actually get answered
  # rather than sitting forever.
  if is_chat_running(chat_id) or chat.pending_messages:
    new_msg = await _append_to_pending(chat, body, db)

    if not is_chat_running(chat_id):
      # Stale pending — try to claim and drain. mark_starting prevents
      # a duplicate spawn if a concurrent request already started one
      # (e.g., two stale-pending POSTs racing).
      if mark_starting(chat_id):
        try:
          from app.chat import (
            _promote_pending_messages, _schedule_continuation,
          )
          next_messages, next_user, next_session_id = (
            await _promote_pending_messages(db, chat_id)
          )
          if next_user:
            _schedule_continuation(
              chat_id=chat_id,
              messages=next_messages,
              session_id=next_session_id,
              provider_id=chat.provider,
              next_user=next_user,
            )
          else:
            # Nothing to promote (queue race, malformed) — release.
            discard_starting(chat_id)
        except Exception:
          discard_starting(chat_id)
          raise

    # Re-read pending after the potential promote so the reported
    # position reflects the user-visible queue (excludes the message
    # that just became the active turn).
    db.refresh(chat)
    remaining = list(chat.pending_messages or [])
    try:
      position = [m.get("ts") for m in remaining].index(new_msg["ts"]) + 1
    except ValueError:
      # Edge case: the new message was somehow consumed (shouldn't
      # happen because promote takes the head, but defensive).
      position = 0
    return _queued_response(new_msg, position)

  if not mark_starting(chat_id):
    new_msg = await _append_to_pending(chat, body, db)
    return _queued_response(new_msg, len(chat.pending_messages))

  # From here until create_task, any exception must discard the
  # starting guard — otherwise the chat_id stays in the set forever and
  # the chat is stuck "starting" until process restart.  run_chat's
  # outer finally only fires after the task is scheduled.
  try:
    # Set provider on first message (new chat).
    if not chat.messages:
      owner = db.query(models.Owner).first()
      chat.provider = (owner.provider if owner else "claude") or "claude"

    # Build the full message history for the agent.
    msgs = [schemas.ChatMessage(role=m["role"], content=m.get("content", ""))
            for m in (chat.messages or [])]

    content = _content_with_uploads(chat, body)

    msgs.append(schemas.ChatMessage(role="user", content=content))

    # Save the user message to the DB immediately so the chat list
    # reflects it before the background task starts.  This also sets
    # the title from the first message.
    user_msg = _user_message_from_body(chat, body)
    existing = list(chat.messages or [])
    existing.append(user_msg)
    chat.messages = existing
    if len(existing) == 1:
      chat.title = body.content[:40] or "New chat"
    chat.updated_at = datetime.now(UTC)
    db.commit()

    # Create the broadcast before spawning the task so the stream
    # endpoint can subscribe immediately without a race.
    bc = create_broadcast(chat_id)  # noqa: F841 — registered in global registry

    from app.chat import current_run_generation
    gen = current_run_generation(chat_id)
    asyncio.create_task(
      run_chat(
        msgs, chat_id=chat_id, session_id=chat.session_id,
        provider_id=chat.provider, run_gen=gen,
        attachments=body.attachments, timezone=body.timezone,
        viewport=body.viewport,
      )
    )
  except Exception:
    discard_starting(chat_id)
    raise

  return JSONResponse(status_code=202, content={"status": "started"})


@router.delete("/{chat_id}/pending/{ts}", status_code=200)
async def cancel_pending_message(
  chat_id: str,
  ts: int,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Removes a queued (not-yet-started) user message from the pending
  queue. Identifies the message by its client-assigned timestamp.

  Returns the updated pending queue so the client can reconcile any
  drift (e.g. the backend promoted a message into the active turn
  between the user clicking X and the DELETE landing).
  """
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found.")

  # Same per-chat lock as POST queue append + promote. Without it, a
  # DELETE that races a concurrent POST/promote can read a stale
  # snapshot and commit, undoing the other operation. Serializing
  # here makes all three queue mutations pairwise atomic.
  from app.chat import get_queue_lock
  async with get_queue_lock(chat_id):
    db.refresh(chat)
    pending = list(chat.pending_messages or [])
    remaining = [m for m in pending if m.get("ts") != ts]
    if len(remaining) != len(pending):
      chat.pending_messages = remaining
      chat.updated_at = datetime.now(UTC)
      db.commit()

  return {"pending_messages": remaining}


@router.get("/{chat_id}/stream")
async def stream_chat(
  request: Request,
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
):
  """SSE endpoint: subscribes to the chat's broadcast and streams events.

  Sends a catch-up burst of all prior events, then streams live events
  until the broadcast is completed or the client disconnects.  Keepalive
  comments are sent every 30 s to prevent proxy timeouts.
  """
  bc = get_broadcast(chat_id)
  if bc is None:
    return Response(status_code=204)

  catch_up, queue = bc.subscribe()

  async def generate():
    try:
      # Send all events buffered before this client connected.
      has_done = False
      for event in catch_up:
        yield _sse(event)
        if event.get("type") == "done":
          has_done = True

      # Signal the client that catch-up is complete and live events follow.
      # The client uses this to switch from instant rendering to typewriter.
      yield _sse({"type": "catch_up_done"})

      # If the broadcast already finished and the catch-up included the
      # done event, we're done — no need to wait on the live queue.
      if not bc.running and has_done:
        return

      # If the broadcast already finished but the catch-up had no done
      # event, synthesise one so the client unblocks.
      if not bc.running and not has_done:
        yield _sse({"type": "done"})
        return

      # Stream live events from the queue.
      while True:
        if await request.is_disconnected():
          break

        try:
          event = await asyncio.wait_for(
            queue.get(), timeout=_KEEPALIVE_INTERVAL
          )
        except asyncio.TimeoutError:
          # Send a keepalive comment — invisible to EventSource clients
          # but keeps the TCP connection alive through proxies.
          yield ": keepalive\n\n"
          continue

        # None is the sentinel pushed by mark_completed().
        if event is None:
          break

        yield _sse(event)

        if event.get("type") == "done":
          break

    finally:
      bc.unsubscribe(queue)

  return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
