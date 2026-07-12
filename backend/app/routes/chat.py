"""Chat route: stop the active agent subprocess."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models, schemas
from app.chat import stop_chat
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/stop", dependencies=[Depends(reject_cross_site)])
async def chat_stop(
  body: schemas.ChatStopRequest,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Stops the agent subprocess and clears its session.

  `cleared_pending_cids` is the stable `cid` of the queued messages this Stop
  actually removed; the frontend resends only those so a queued message the
  turn-end drain already promoted into a continuation isn't double-sent
  (PM 115).
  """
  stopped, cleared_pending_cids, cleared_pending_ts = await stop_chat(
    body.chat_id or None, db=db,
  )
  # `cleared_pending_ts` is a deploy-window bridge: a stale service-worker
  # bundle reads only that field, and without it the old client falls back to
  # resending its full queue snapshot — the exact duplicate PM-115 closed.
  return {
    "stopped": stopped,
    "cleared_pending_cids": cleared_pending_cids,
    "cleared_pending_ts": cleared_pending_ts,
  }
