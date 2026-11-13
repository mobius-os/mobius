"""Frozen agent choice and completion filing for owner-started update jobs."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import background_agents, chat_archive, models, providers
from app.config import get_settings


class UpdateAgentChoice(BaseModel):
  model_config = ConfigDict(extra="forbid")
  provider: str = Field(min_length=1, max_length=32)
  model: str = Field(min_length=1, max_length=256)
  effort: str | None = Field(default=None, max_length=32)


def resolve_update_chat_choice(db: Session, requested: UpdateAgentChoice | None) -> dict:
  """A requested choice is exact; omission retains the background default."""
  data_dir = get_settings().data_dir
  if requested is None:
    return background_agents.resolve_background_chat_choice(data_dir, db)
  if not providers.provider_selectable(data_dir, requested.provider):
    raise HTTPException(422, "The selected provider is unavailable.")
  unavailable = providers.PROVIDERS[requested.provider].check_auth(data_dir)
  if unavailable is not None:
    raise HTTPException(409, "The selected provider is unavailable.")
  try:
    settings = providers.snapshot_chat_agent_settings(
      data_dir, requested.provider, model=requested.model,
      effort=requested.effort, fallback_model=requested.model,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  if settings is None or settings.get("model") != requested.model.strip():
    raise HTTPException(422, "The selected model could not be saved.")
  return {"provider": requested.provider, "agent_settings": settings}


def archive_completed_app_resolver(db: Session, chat_id: str | None) -> bool:
  """File only when no other app in the batch still binds this resolver."""
  if not chat_id:
    return False
  remaining = db.query(models.App.id).filter(
    models.App.conflict_resolver_chat_id == chat_id,
  ).first()
  if remaining is not None:
    return False
  chat = db.get(models.Chat, chat_id)
  if (chat is not None and chat.deleted_at is None
      and chat.created_by_app_id is None and chat.archived_at is None):
    chat_archive.archive_chat(db, chat)
    return True
  return False
