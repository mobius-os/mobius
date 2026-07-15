"""Chat route: stop the active agent subprocess."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.chat import stop_chat
from app.database import get_db
from app.deps import (
  Principal, get_owner_or_chat_embed_principal, reject_cross_site,
  require_chat_embed_operation,
)
from app.resource_access import get_active_chat_for_principal

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/stop", dependencies=[Depends(reject_cross_site)])
async def chat_stop(
  body: schemas.ChatStopRequest,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
):
  """Stops the agent subprocess and clears its session.

  `cleared_pending_cids` is the stable `cid` of the queued messages this Stop
  actually removed; the frontend resends only those so a queued message the
  turn-end drain already promoted into a continuation isn't double-sent
  (PM 115).
  """
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:stop")
  if principal.scope == "chat_embed" and not body.chat_id:
    raise HTTPException(status_code=403, detail="Embedded chat id is required.")
  if body.chat_id:
    get_active_chat_for_principal(db, body.chat_id, principal)
  stopped, cleared_pending_cids = await stop_chat(body.chat_id or None, db=db)
  return {"stopped": stopped, "cleared_pending_cids": cleared_pending_cids}
