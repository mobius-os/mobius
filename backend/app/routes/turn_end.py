"""Codex's post-tool rendezvous for a turn-ending result.

A helper's recorded question to its parent (`ask_parent`) and a saved key card
(`request_key`, routes/key_requests.py) end the turn. Codex learns of the
receipt in a post-tool hook (`scripts/codex_owner_card_hook.py`)
and calls this route, which interrupts the turn before the completed tool
result resumes the model. Owner-input requests never end a turn: they are
recorded through `owner_input` and shown when the turn ends by itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.chat_event_sink import get_active_sink
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, reject_cross_site
from app.resource_access import get_active_chat_for_principal

router = APIRouter(prefix="/api/chats", tags=["turn-end"])


class TurnEndRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  receipt_id: str = Field(min_length=1, max_length=64)


@router.post("/{chat_id}/turn-end", dependencies=[Depends(reject_cross_site)])
async def end_codex_turn(
  chat_id: str,
  body: TurnEndRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Post-tool rendezvous: interrupt before the completed tool resumes Codex.

  Only a question this exact run recorded, or a key card it saved, ends it.
  """
  from app.runner_registry import RunnerKind, registry

  if principal.chat_id != chat_id:
    raise HTTPException(403, "Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal, load_fields=())
  sink = get_active_sink(chat_id)
  handle = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  if (sink is None or sink.run_token != principal.run_id
      or not sink.ends_turn(body.receipt_id) or handle is None):
    raise HTTPException(409, "This turn did not produce that turn-ending result.")
  db.close()  # No database connection is needed while waiting on the provider.
  interrupt = handle.begin_finish_after_owner_card()
  try:
    if interrupt is not None:
      await interrupt
    else:
      await handle.wait_for_owner_card_end()
  except Exception as exc:
    raise HTTPException(503, "Could not end the turn.") from exc
  return {"ended": handle.owner_card_requested}
