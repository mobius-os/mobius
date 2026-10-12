"""Live rooms over a WebSocket: ``/api/apps/{app_id}/live/{room}``.

The browser WebSocket API cannot set headers, so the app's bearer rides the
``token`` query parameter, as it does for the app module route. Two bearers
open a room: the app token of a signed-in session, and the public-app token of
a guest on the app's public page (the app has to be published for that token
to exist, which is the app's opt-in). Either must match ``app_id``.

Wire protocol (JSON text frames):
  server -> client  {"t":"welcome","you":{id,name},"members":[...],"max":n}
                    {"t":"presence","members":[{id,name}]}
                    {"t":"msg","from":id,"data":<any>}
  client -> server  {"t":"send","data":<any>,"to":[id,...]?}   (to omitted = everyone else)
                    {"t":"sync"}                                 (ask for presence)
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app import auth, deps
from app.database import SessionLocal
from app.live_rooms import MAX_MESSAGE_BYTES, LiveError, hub

router = APIRouter(tags=["live-rooms"])
_ROOM = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _authorize(token: str, app_id: int) -> bool:
  payload = auth.decode_access_token(token) if token else None
  if not payload:
    return False
  scope = payload.get("scope")
  db = SessionLocal()
  try:
    if scope == "public_app":
      deps.resolve_public_app_token(token, db, expected_app_id=app_id)
      return True
    if scope == "app" and payload.get("app_id") == app_id:
      deps.resolve_owner_or_app(token, db)
      return True
  except HTTPException:
    return False
  finally:
    db.close()
  return False


async def _writer(ws: WebSocket, member) -> None:
  while True:
    frame = await member.outbox.get()
    await ws.send_text(frame)


@router.websocket("/api/apps/{app_id}/live/{room}")
async def live_room(ws: WebSocket, app_id: int, room: str):
  token = ws.query_params.get("token", "")
  await ws.accept()
  if not _ROOM.match(room) or not await asyncio.to_thread(_authorize, token, app_id):
    await ws.close(code=4401)
    return
  try:
    cap = int(ws.query_params.get("max", "0") or 0) or None
  except ValueError:
    cap = None
  try:
    member = hub.join(app_id, room, ws.query_params.get("name", ""), cap)
  except LiveError as exc:
    await ws.close(code=4403 if exc.code == "full" else 4429)
    return
  writer = asyncio.create_task(_writer(ws, member))
  try:
    while True:
      raw = await ws.receive_text()
      if len(raw) > MAX_MESSAGE_BYTES:
        await ws.close(code=4413)
        return
      if not member.allow():
        continue   # over budget: shed, never queue
      try:
        msg = json.loads(raw)
      except ValueError:
        continue
      kind = msg.get("t") if isinstance(msg, dict) else None
      if kind == "send":
        to = msg.get("to")
        hub.relay(member, msg.get("data"),
                  [t for t in to if isinstance(t, str)][:16] if isinstance(to, list) else None)
      elif kind == "sync":
        hub.roster(member)
  except WebSocketDisconnect:
    pass
  except RuntimeError:
    pass
  finally:
    writer.cancel()
    hub.leave(member)
