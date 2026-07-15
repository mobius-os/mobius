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

from fastapi import APIRouter, Depends, HTTPException, Request
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
from app.deps import (
  Principal, chat_embed_session_is_active, get_owner_or_chat_embed_principal,
  get_current_owner, reject_cross_site,
  require_chat_embed_operation,
)
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

# Catch-up-unsafe events: they ride the system broadcast alone and are NEVER
# fanned out to per-chat broadcasts, because a chat reconnect replaying an old
# copy from its event log would fire a spurious shell apply (or a stale
# failure signal). SystemBroadcast has no replay, so one delivery per client —
# no frontend dedup needed. app_build_failed's live producer
# (app_watcher._publish_app_build_failed) already publishes system-bus-only and
# never hits this route, but it is listed here so a hypothetical POST stays
# consistent with that classification (the frontend also no longer recognizes
# it on a chat stream).
_SYSTEM_BUS_ONLY_EVENTS = frozenset({
  "shell_rebuilding",
  "shell_rebuilt",
  "shell_apply_now",
  "shell_rebuild_failed",
  "app_build_failed",
  "app_created",
})


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

  # Fan out to running per-chat broadcasts ONLY for catch-up-SAFE list/theme
  # refreshes (theme_updated, app_updated), where a chat reconnect's replay is
  # a genuine backstop for a dropped system stream. The shell-rebuild lifecycle
  # events (shell_rebuilding/rebuilt/apply_now/rebuild_failed) are
  # catch-up-UNSAFE: a replayed `shell_rebuilt` tells the Shell a fresh build
  # just landed and triggers a spurious apply. They ride the system broadcast
  # alone (no replay), so single-bus delivery is exactly one hit and the
  # frontend needs no dedup.
  if body.type not in _SYSTEM_BUS_ONLY_EVENTS:
    targets = get_all_active_broadcasts()
    if not targets:
      bc = get_active_broadcast()
      if bc is not None:
        targets = [bc]
    for bc in targets:
      bc.publish(event)

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
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
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
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:stream")
  embedded_chat_id = principal.chat_id if principal.scope == "chat_embed" else None
  embed_session_id = (
    principal.embed_session_id if principal.scope == "chat_embed" else None
  )
  db.close()
  queue = get_system_broadcast().subscribe()

  async def generate():
    last_embed_auth_check = 0.0

    def embed_session_active() -> bool:
      nonlocal last_embed_auth_check
      if embed_session_id is None:
        return True
      now_mono = time.monotonic()
      if now_mono - last_embed_auth_check < 1.0:
        return True
      last_embed_auth_check = now_mono
      return chat_embed_session_is_active(embed_session_id)

    try:
      # Hello so the client knows the connection is live before any
      # real event arrives. EventSource clients ignore unknown types
      # but the message still flushes Caddy / nginx buffers.
      yield f"data: {json.dumps({'type': 'system_stream_open'})}\n\n"
      while True:
        if not embed_session_active():
          return
        if await request.is_disconnected():
          break
        try:
          event = await asyncio.wait_for(
            queue.get(), timeout=_KEEPALIVE_INTERVAL,
          )
        except asyncio.TimeoutError:
          yield ": keepalive\n\n"
          continue
        if embedded_chat_id is not None and str(event.get("chatId") or "") != embedded_chat_id:
          continue
        yield f"data: {json.dumps(event)}\n\n"
    finally:
      get_system_broadcast().unsubscribe(queue)

  return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
