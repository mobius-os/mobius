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

  if not mark_starting(chat_id):
    raise HTTPException(status_code=409, detail="Agent is already running.")

  # From here until create_task, any exception must discard the
  # starting guard — otherwise the chat_id stays in the set forever and
  # the chat is stuck "starting" until process restart.  run_chat's
  # outer finally only fires after the task is scheduled.
  try:
    # Build the full message history for the agent.
    msgs = [schemas.ChatMessage(role=m["role"], content=m.get("content", ""))
            for m in (chat.messages or [])]

    # Append a file notification when the chat has uploads so the
    # agent knows what files are available in this session.  Validate
    # each stored path against data_dir before injecting it — a
    # tampered DB record could otherwise point the agent at
    # credentials or other sensitive files.
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

    msgs.append(schemas.ChatMessage(role="user", content=content))

    # Save the user message to the DB immediately so the chat list
    # reflects it before the background task starts.  This also sets
    # the title from the first message.
    user_msg = {
      "role": "user",
      "content": content,
      "ts": int(time.time() * 1000),
    }
    if body.attachments:
      user_msg["attachments"] = body.attachments
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

    asyncio.create_task(
      run_chat(
        msgs, chat_id=chat_id, session_id=chat.session_id,
        attachments=body.attachments, timezone=body.timezone,
        viewport=body.viewport,
      )
    )
  except Exception:
    discard_starting(chat_id)
    raise

  return JSONResponse(status_code=202, content={"status": "started"})


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
