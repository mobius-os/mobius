"""Chat route: stop the active agent subprocess."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.chat import stop_chat, stop_chat_with_receipt
from app.database import get_db
from app.deps import (
  Principal, get_owner_or_chat_embed_principal, reject_cross_site, is_owner_input_principal,
  require_chat_embed_operation, require_nondelegated_owner_control,
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

  A confirmed per-chat Stop also returns `stop_receipt: {chat_id, run_id}`,
  the chat's latest run at Stop's serialized boundary (`None` if it never
  ran). An app binds its cancellation to that run, not to a status read after
  this response, which can already show a successor the owner resumed. The
  global sweep and an unconfirmed Stop return `stop_receipt: None`.
  """
  require_nondelegated_owner_control(principal)
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:stop")
  if principal.scope == "chat_embed" and not body.chat_id:
    raise HTTPException(status_code=403, detail="Embedded chat id is required.")
  if body.chat_id:
    require_active_chat_access(db, body.chat_id, principal)
  actor = "owner" if is_owner_input_principal(principal) else "agent"
  actor_id = principal.run_id or principal.chat_id or principal.embed_session_id
  stop_receipt = None
  if body.chat_id:
    outcome = await stop_chat_with_receipt(
      body.chat_id, db=db, actor=actor, actor_id=actor_id,
    )
    stopped, cleared_pending_cids = outcome.stopped, outcome.cleared_pending_cids
    if stopped:
      stop_receipt = {"chat_id": body.chat_id, "run_id": outcome.run_id}
  else:
    stopped, cleared_pending_cids = await stop_chat(
      None, db=db, actor=actor, actor_id=actor_id,
    )
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
    "stop_receipt": stop_receipt,
  }
