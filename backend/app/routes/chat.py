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
from app.resource_access import require_active_chat_access

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
    require_active_chat_access(db, body.chat_id, principal)
  from app.gauntlets import active_gauntlet_ids_for_chat, stop_gauntlet
  gauntlet_ids = (
    active_gauntlet_ids_for_chat(db, body.chat_id)
    if body.chat_id
    else [row[0] for row in db.query(models.GauntletRun.id).filter(
      models.GauntletRun.status.in_(("running", "stopping")),
    ).all()]
  )
  for gauntlet_id in gauntlet_ids:
    await stop_gauntlet(gauntlet_id)
  stopped, cleared_pending_cids = await stop_chat(body.chat_id or None, db=db)
  cancelled_delegations = []
  if body.chat_id and stopped:
    # This route is the owner's explicit Stop. Planned restart draining bypasses
    # it and therefore leaves durable child tasks parked/resumable.
    from app.routes.delegations import cancel_active_for_parent
    cancelled_delegations = await cancel_active_for_parent(db, body.chat_id)
  elif not body.chat_id and stopped:
    from app.routes.delegations import cancel_active_for_parent
    parent_ids = {
      row[0] for row in db.query(models.Delegation.parent_chat_id).distinct()
    }
    for parent_chat_id in parent_ids:
      cancelled_delegations.extend(
        await cancel_active_for_parent(db, parent_chat_id)
      )
  return {
    "stopped": stopped,
    "cleared_pending_cids": cleared_pending_cids,
    "cancelled_delegations": cancelled_delegations,
    "cancelled_gauntlets": gauntlet_ids,
  }
