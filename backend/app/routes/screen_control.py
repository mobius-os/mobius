"""Owner-started, exact-chat live browser control relay."""

from __future__ import annotations

import json
import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import (
  Principal,
  authorize_current_owner_detached,
  get_agent_run_principal,
  get_current_owner,
  reject_cross_site,
)
from app.screen_control import registry


router = APIRouter(prefix="/api/screen-control", tags=["screen-control"])

_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_KEEPALIVE_SECONDS = 15
_PRESS_KEYS = frozenset({
  "Enter", "Escape", "Tab", "Backspace", "Delete", " ",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
  "PageUp", "PageDown",
})


class SessionStartBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  appId: int = Field(gt=0)
  chatId: str = Field(min_length=1, max_length=128)
  route: str = Field(default="/", max_length=512)
  viewport: dict[str, float] = Field(default_factory=dict)

  @model_validator(mode="after")
  def validate_viewport(self) -> "SessionStartBody":
    allowed = {"width", "height", "pixelRatio"}
    if any(key not in allowed for key in self.viewport):
      raise ValueError("viewport contains an unknown field")
    for key, value in self.viewport.items():
      ceiling = 8 if key == "pixelRatio" else 10000
      if not math.isfinite(value) or value < 1 or value > ceiling:
        raise ValueError(f"viewport {key} is outside the supported range")
    return self


class BrowserResponseBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  commandId: str = Field(min_length=1, max_length=128)
  ok: bool
  result: dict[str, Any] | list[Any] | str | int | float | bool | None = None
  error: str | None = Field(default=None, max_length=1000)


class AgentCommandBody(BaseModel):
  """Closed control vocabulary; arbitrary browser script is not a command."""

  model_config = ConfigDict(extra="forbid")

  action: Literal["snapshot", "screenshot", "click", "type", "scroll", "press"]
  ref: str | None = Field(default=None, max_length=160)
  x: float | None = None
  y: float | None = None
  text: str | None = Field(default=None, max_length=20_000)
  replace: bool | None = None
  deltaX: float | None = None
  deltaY: float | None = None
  key: str | None = Field(default=None, max_length=24)

  @model_validator(mode="after")
  def confine_action_fields(self) -> "AgentCommandBody":
    coordinates = self.x is not None or self.y is not None
    if coordinates:
      if self.x is None or self.y is None:
        raise ValueError("x and y must be supplied together")
      if not all(math.isfinite(value) and -1000 <= value <= 20_000 for value in (self.x, self.y)):
        raise ValueError("coordinates are outside the supported range")
    if self.action in {"snapshot", "screenshot"}:
      if any(value is not None for value in (
        self.ref, self.x, self.y, self.text, self.replace,
        self.deltaX, self.deltaY, self.key,
      )):
        raise ValueError(f"{self.action} does not accept a target or value")
    elif self.action == "click":
      if not self.ref and not coordinates:
        raise ValueError("click requires ref or x/y")
      if any(value is not None for value in (
        self.text, self.replace, self.deltaX, self.deltaY, self.key,
      )):
        raise ValueError("click accepts only ref or x/y")
    elif self.action == "type":
      if self.text is None:
        raise ValueError("type requires text")
      if any(value is not None for value in (self.deltaX, self.deltaY, self.key)):
        raise ValueError("type does not accept scroll or key fields")
    elif self.action == "scroll":
      if self.deltaX is None and self.deltaY is None:
        raise ValueError("scroll requires deltaX or deltaY")
      for value in (self.deltaX, self.deltaY):
        if value is not None and (not math.isfinite(value) or abs(value) > 20_000):
          raise ValueError("scroll delta is outside the supported range")
      if any(value is not None for value in (
        self.ref, self.text, self.replace, self.key,
      )):
        raise ValueError("scroll accepts only deltas and an optional x/y point")
    elif self.action == "press":
      if self.key not in _PRESS_KEYS:
        raise ValueError("press key is not in the supported set")
      if any(value is not None for value in (
        self.ref, self.x, self.y, self.text, self.replace,
        self.deltaX, self.deltaY,
      )):
        raise ValueError("press accepts only key")
    return self


def _agent_for_chat(chat_id: str, principal: Principal) -> None:
  if principal.chat_id != chat_id:
    raise HTTPException(
      status_code=403,
      detail="The agent token is bound to a different chat.",
    )


def _session_wire(session) -> dict[str, Any]:
  return {
    "active": session.active,
    "sessionId": session.id,
    "appId": session.app_id,
    "chatId": session.chat_id,
    "route": session.route,
    "viewport": session.viewport,
    "connected": session.browser_connections > 0,
    "expiresAt": int(session.expires_at * 1000),
  }


@router.post("/sessions", dependencies=[Depends(reject_cross_site)])
async def start_session(
  body: SessionStartBody,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  chat = db.query(models.Chat).filter(
    models.Chat.id == body.chatId,
    models.Chat.created_by_app_id == body.appId,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None:
    raise HTTPException(status_code=404, detail="Chat not found.")
  session = await registry.start(
    owner_username=owner.username,
    app_id=body.appId,
    chat_id=body.chatId,
    route=body.route,
    viewport=body.viewport,
  )
  return _session_wire(session)


@router.get(
  "/sessions/{session_id}/events",
)
async def browser_events(
  session_id: str,
  request: Request,
  owner_username: str = Depends(authorize_current_owner_detached),
):
  # One-owner product today. Authentication is detached before streaming so a
  # live screen does not pin a database connection for its whole 15-minute
  # consent window; the unguessable session id is additionally bound at start.
  session = await registry.get_for_browser(session_id, owner_username)
  if session is None:
    raise HTTPException(status_code=404, detail="Shared-screen session not found.")

  async def generate():
    connected = await registry.connect_browser(session_id, owner_username)
    if connected is None:
      return
    try:
      yield f"data: {json.dumps({'type': 'screen-control-open'})}\n\n"
      while True:
        if await request.is_disconnected():
          break
        try:
          command = await registry.next_browser_command(
            session, keepalive_seconds=_KEEPALIVE_SECONDS,
          )
        except TimeoutError:
          yield ": keepalive\n\n"
          continue
        if command is None:
          yield f"data: {json.dumps({'type': 'screen-control-stop'})}\n\n"
          break
        yield f"data: {json.dumps({'type': 'screen-control-command', **command})}\n\n"
    finally:
      await registry.disconnect_browser(session.id)

  return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )


@router.post(
  "/sessions/{session_id}/responses",
  dependencies=[Depends(reject_cross_site)],
)
async def browser_response(
  session_id: str,
  body: BrowserResponseBody,
  owner: models.Owner = Depends(get_current_owner),
):
  session = await registry.get_for_browser(session_id, owner.username)
  if session is None:
    raise HTTPException(status_code=404, detail="Shared-screen session not found.")
  encoded = json.dumps(body.result, ensure_ascii=True, separators=(",", ":"))
  if len(encoded.encode("utf-8")) > _RESPONSE_MAX_BYTES:
    raise HTTPException(status_code=413, detail="Screen-control response is too large.")
  accepted = await registry.answer(session, body.commandId, {
    "ok": body.ok,
    "result": body.result,
    "error": body.error,
  })
  if not accepted:
    raise HTTPException(status_code=409, detail="Command is no longer pending.")
  return {"accepted": True}


@router.delete(
  "/sessions/{session_id}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def stop_browser_session(
  session_id: str,
  owner: models.Owner = Depends(get_current_owner),
):
  session = await registry.get_for_browser(session_id, owner.username)
  if session is not None:
    await registry.stop(session)


@router.get("/chats/{chat_id}")
async def agent_session_status(
  chat_id: str,
  principal: Principal = Depends(get_agent_run_principal),
):
  _agent_for_chat(chat_id, principal)
  session = await registry.get_for_chat(chat_id, principal.owner.username)
  return _session_wire(session) if session is not None else {"active": False}


@router.post(
  "/chats/{chat_id}/commands",
  dependencies=[Depends(reject_cross_site)],
)
async def agent_command(
  chat_id: str,
  body: AgentCommandBody,
  principal: Principal = Depends(get_agent_run_principal),
):
  _agent_for_chat(chat_id, principal)
  session = await registry.get_for_chat(chat_id, principal.owner.username)
  if session is None:
    raise HTTPException(status_code=409, detail="No active shared-screen session.")
  outcome = await registry.issue_command(
    session,
    body.model_dump(exclude_none=True, exclude_defaults=True),
  )
  if not outcome.get("ok"):
    raise HTTPException(status_code=409, detail=outcome.get("error") or "Command failed.")
  return outcome.get("result")


@router.delete(
  "/chats/{chat_id}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def agent_stop_session(
  chat_id: str,
  principal: Principal = Depends(get_agent_run_principal),
):
  _agent_for_chat(chat_id, principal)
  session = await registry.get_for_chat(chat_id, principal.owner.username)
  if session is not None:
    await registry.stop(session, "The agent released screen control.")
