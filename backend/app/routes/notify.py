"""Lightweight event notification endpoint + shell-level SSE stream.

POST /api/notify lets the agent emit system events (theme, app,
shell rebuild). They land on both the SystemBroadcast (Shell-level
listener, always live) and any active per-chat broadcasts (so the
chat catch-up replay stays coherent).

GET /api/events/system is the Shell's persistent SSE subscription.
Independent of any chat — survives navigation so app_updated /
theme_updated reach Shell even when the user is on the canvas or
settings view.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app import models
from app.broadcast import (
  get_active_broadcast,
  get_all_active_broadcasts,
  get_broadcast,
  get_system_broadcast,
)
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.events import SYSTEM_EVENT_TYPES

router = APIRouter(tags=["notify"])
log = logging.getLogger(__name__)

# Keepalive cadence for the shell-level SSE — same value used in
# chats_stream so proxies behave consistently.
_KEEPALIVE_INTERVAL = 30

class NotifyBody(BaseModel):
  type: str
  appId: str | None = None
  error: str | None = None

  @field_validator("type")
  @classmethod
  def validate_type(cls, value: str) -> str:
    """Reject unknown system-event types at request-deserialize time."""
    if value not in SYSTEM_EVENT_TYPES:
      raise ValueError(f"unknown event type: {value}")
    return value


def publish_app_built_to_owning_chat(db: Session, app_id_str: str) -> None:
  """Emit a chat-scoped `app_built` on the broadcast of the chat that
  built this app, if that chat is still streaming.

  The "Open app" CTA must appear ONLY in the chat whose turn built (or
  updated) the app — never in an unrelated chat the user happens to have
  open. The naturally-scoped signal is the app row's `chat_id`, which
  `register_app.py` stamps from the `CHAT_ID` env of the running turn.
  We look it up and, when a broadcast for that chat is live, publish
  `app_built` onto only that chat's stream. ChatView (keyed by chat id)
  reads `app_built` off its OWN stream and sets the CTA — so the CTA
  cannot leak across chats. The global `app_updated` stays list-refresh-
  only on the frontend.

  Silently no-ops when the app has no `chat_id` (e.g. App Store install
  or a manual create outside a turn) or that chat has no live broadcast
  (the turn already ended) — in those cases there is no chat whose turn
  owns the build, so no CTA is appropriate.
  """
  try:
    app_id = int(app_id_str)
  except (TypeError, ValueError):
    return
  app = db.query(models.App).filter(models.App.id == app_id).first()
  if app is None or not app.chat_id:
    return
  bc = get_broadcast(str(app.chat_id))
  if bc is None or not bc.running:
    return
  bc.publish({"type": "app_built", "appId": app_id_str})


@router.post(
  "/api/notify", status_code=204, dependencies=[Depends(reject_cross_site)],
)
def notify(
  body: NotifyBody,
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Publish a system event to the active chat broadcast.

  Requires a valid JWT.  If no broadcast is active (no agent running),
  the event is silently dropped — nobody is listening.
  """
  event: dict = {"type": body.type}
  if body.appId is not None:
    event["appId"] = body.appId
  if body.error is not None:
    event["error"] = body.error

  # ALWAYS publish to the system broadcast — Shell subscribes to it
  # for system events regardless of which view the user is on.
  # Without this, an app_updated emitted after the chat finished
  # streaming (or while the user is on the canvas / settings) would
  # have nowhere to land: chat broadcasts close shortly after the
  # turn ends, and the canvas view never had a subscription.
  get_system_broadcast().publish(event)

  # Also publish to running per-chat broadcasts so any currently
  # active chat catch-up replay includes the event in order. New
  # subscribers connecting to a stale event log get the event too,
  # which keeps existing chat-level UI invariants.
  targets = get_all_active_broadcasts()
  if not targets:
    bc = get_active_broadcast()
    if bc is not None:
      targets = [bc]
  for bc in targets:
    bc.publish(event)

  # Chat-scoped CTA signal: when an app was built/updated, fire a
  # separate `app_built` event onto ONLY the broadcast of the chat that
  # owns the app. This is what plants the "Open app" CTA; the global
  # `app_updated` above is list-refresh-only on the frontend, so the CTA
  # can no longer leak into an unrelated chat (the activeView-gate stopgap
  # is no longer the load-bearing scoping mechanism — this is).
  if body.type == "app_updated" and body.appId is not None:
    publish_app_built_to_owning_chat(db, body.appId)


@router.get("/api/events/system")
async def stream_system_events(
  request: Request,
  _owner: models.Owner = Depends(get_current_owner),
):
  """Shell-level SSE: streams system events for the lifetime of the
  Shell, regardless of which view (chat / canvas / settings) is
  mounted. The Shell subscribes once on mount and keeps the
  connection open until logout / unmount.

  Keepalive cadence matches the chat stream so reverse proxies see
  consistent traffic patterns.
  """
  queue = get_system_broadcast().subscribe()

  async def generate():
    try:
      # Hello so the client knows the connection is live before any
      # real event arrives. EventSource clients ignore unknown types
      # but the message still flushes Caddy / nginx buffers.
      yield f"data: {json.dumps({'type': 'system_stream_open'})}\n\n"
      while True:
        if await request.is_disconnected():
          break
        try:
          event = await asyncio.wait_for(
            queue.get(), timeout=_KEEPALIVE_INTERVAL,
          )
        except asyncio.TimeoutError:
          yield ": keepalive\n\n"
          continue
        yield f"data: {json.dumps(event)}\n\n"
    finally:
      get_system_broadcast().unsubscribe(queue)

  return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
