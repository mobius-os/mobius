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
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator, model_validator
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

# Characters kept from a build_phase label. The label is the one free-text
# field on this otherwise-closed schema and renders untrusted in the chat foot
# (and the polite live region), so a single POST cannot flood the rail with an
# unbounded string.
_LABEL_MAX = 80


class NotifyBody(BaseModel):
  type: str
  appId: str | None = None
  error: str | None = None
  # Milestone-rail fields, permitted ONLY on build_phase events (enforced
  # below) so the type whitelist stays the meaningful contract: `label` is
  # the free-text milestone, `chatId` names the building chat explicitly —
  # build_phase.py sends the $CHAT_ID of its own running turn.
  label: str | None = None
  chatId: str | None = None

  @field_validator("type")
  @classmethod
  def validate_type(cls, value: str) -> str:
    """Reject unknown system-event types at request-deserialize time."""
    if value not in SYSTEM_EVENT_TYPES:
      raise ValueError(f"unknown event type: {value}")
    return value

  @model_validator(mode="after")
  def confine_build_phase_fields(self) -> "NotifyBody":
    """Confine `label`/`chatId` to build_phase and bound the label.

    Either field on any other event type is rejected so the closed schema
    keeps its meaning; a build_phase label is truncated to `_LABEL_MAX` so
    one POST cannot render an unbounded string into the chat foot.
    """
    if self.type != "build_phase":
      if self.label is not None:
        raise ValueError("label is only valid for build_phase events")
      if self.chatId is not None:
        raise ValueError("chatId is only valid for build_phase events")
      return self
    if self.label is not None:
      self.label = self.label[:_LABEL_MAX]
    return self


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


def publish_build_phase_to_chat(chat_id: str | None, label: str) -> None:
  """Emit a chat-scoped `build_phase` on the named building chat's broadcast.

  The milestone rail belongs ONLY to the chat whose turn emitted the
  milestone, so the chat identity travels explicitly: `build_phase.py` sends
  the `$CHAT_ID` of its own running turn. Routing via the active-broadcast
  pointer was wrong — that pointer tracks whichever turn STARTED most
  recently, so with two concurrent builds chat B swallowed chat A's
  milestones, and B finishing first cleared the pointer and silently dropped
  A's later ones. `get_broadcast(chat_id)` addresses the named chat
  regardless of what else is running. No chat id, no live broadcast, or a
  finished turn means there is no rail to feed, and the signal is dropped
  (best-effort by design).

  Single-owner trusted-agent: the only checks are the NotifyBody type gate
  and a running broadcast for the named chat — deliberately no token-claim
  validation.

  The event carries the agent-authored label plus a millisecond timestamp
  the frontend dedupes on across catch-up replay.
  """
  if not chat_id:
    return
  bc = get_broadcast(chat_id)
  if bc is None or not bc.running:
    return
  bc.publish({
    "type": "build_phase",
    "label": label,
    "ts": int(time.time() * 1000),
  })


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
  # A build_phase is chat-scoped (see publish_build_phase_to_chat): it must
  # not fan out to the system broadcast or unrelated chats, so route it onto
  # the named building chat's broadcast alone and return before the general
  # publish path below (which is for events every view must hear).
  if body.type == "build_phase":
    publish_build_phase_to_chat(body.chatId, body.label or "")
    return

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

  # Agent affordance from design §1.5: the event still reaches clients through
  # the normal broadcast path above, and the watcher also publishes any dirty
  # warm staging immediately instead of waiting for the settle timer.
  if body.type == "shell_apply_now":
    try:
      from app.frontend_watcher import publish_now
      publish_now("shell_apply_now")
    except Exception:
      log.exception("shell_apply_now publish hook failed")


@router.get("/api/events/system")
async def stream_system_events(
  request: Request,
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Shell-level SSE: streams system events for the lifetime of the
  Shell, regardless of which view (chat / canvas / settings) is
  mounted. The Shell subscribes once on mount and keeps the
  connection open until logout / unmount.

  Keepalive cadence matches the chat stream so reverse proxies see
  consistent traffic patterns.
  """
  # Release the DB connection BEFORE entering the (potentially hours-long)
  # stream loop. FastAPI defers yield-dependency teardown for a
  # StreamingResponse until the body finishes streaming, so get_db's
  # `db.close()` would otherwise not run until the client disconnects —
  # pinning one pooled connection per open Shell tab. With a Postgres
  # QueuePool of 5+10, a handful of lingering EventSource connections
  # exhausted the pool and every DB-touching request began timing out
  # (QueuePool limit reached). `db` here is the SAME session get_current_owner
  # used (FastAPI caches the get_db sub-dependency within a request), so
  # auth has already completed against it; closing now returns the
  # connection immediately and get_db's own finally close is a no-op.
  db.close()
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
