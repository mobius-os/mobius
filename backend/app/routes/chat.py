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
  """Stops the agent subprocess and clears its session."""
  stopped = await stop_chat(body.chat_id or None, db=db)
  return {"stopped": stopped}
