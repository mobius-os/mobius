"""Run-bound listing and calls for tools contributed by installed apps."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import app_services, app_tools
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, reject_cross_site


router = APIRouter(prefix="/api/agent/app-tools", tags=["app-tools"])


class AppToolCall(BaseModel):
  model_config = ConfigDict(extra="forbid")

  name: str = Field(min_length=1, max_length=128)
  arguments: dict[str, Any] = Field(default_factory=dict)
  # The MCP request's `_meta`, verbatim. Only the provider's own call id is
  # read from it (see app_tools.PROVIDER_CALL_ID_KEYS).
  meta: dict[str, Any] = Field(default_factory=dict)


@router.get("/")
def list_app_tools(
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
) -> dict[str, Any]:
  return {"tools": [tool.listing() for tool in app_tools.live_app_tools(db)]}


@router.post("/call", dependencies=[Depends(reject_cross_site)])
async def call_app_tool(
  body: AppToolCall,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
) -> dict[str, Any]:
  moment = app_tools.call_moment(
    db, chat_id=principal.chat_id, run_id=principal.run_id, meta=body.meta,
  )
  result, is_error = await app_tools.call_app_tool(
    db,
    principal.owner,
    exposed_name=body.name,
    arguments=body.arguments,
    moment=moment,
    actor=app_services.request_actor(db, principal),
  )
  return {"result": result, "is_error": is_error}
