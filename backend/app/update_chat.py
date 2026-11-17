"""Frozen agent choice and completion filing for owner-started update jobs."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import background_agents, models, providers
from app.config import get_settings


class UpdateAgentChoice(BaseModel):
  model_config = ConfigDict(extra="forbid")
  provider: str = Field(min_length=1, max_length=32)
  model: str = Field(min_length=1, max_length=256)
  effort: str | None = Field(default=None, max_length=32)


_BATCH_KEY = "_app_update_resolver_batch"


def resolver_batch_settings(settings: dict, apps: list[models.App]) -> dict:
  """Freeze the exact members of this resolver, not just its live bindings."""
  return {**settings, _BATCH_KEY: {
    str(app.id): {"upstream_commit": app.upstream_commit, "completed": None}
    for app in apps
  }}


def record_resolver_member_outcome(
  db: Session, chat_id: str | None, app_id: int,
  upstream_commit: str | None, *, completed: bool,
) -> bool:
  """Persist a member's outcome with binding removal, under the drawer lock.

  A missing/older batch identity cannot certify an all-success batch. Failed
  members remain in the frozen manifest even after their binding disappears.
  """
  if not chat_id:
    return False
  chat = db.get(models.Chat, chat_id)
  if chat is None:
    return False
  settings = chat.agent_settings_json or {}
  batch = settings.get(_BATCH_KEY) if isinstance(settings, dict) else None
  if not isinstance(batch, dict):
    return False
  member = batch.get(str(app_id))
  if not isinstance(member, dict) or member.get("upstream_commit") != upstream_commit:
    return False
  outcome = False if member.get("completed") is False else completed
  if member.get("completed") is not outcome:
    batch = {**batch, str(app_id): {**member, "completed": outcome}}
    chat.agent_settings_json = {**settings, _BATCH_KEY: batch}
  return outcome and bool(batch) and all(
    isinstance(entry, dict) and entry.get("completed") is True
    for entry in batch.values()
  )


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
  """Stage filing when no other app in the batch still binds this resolver.

  Caller holds the drawer lock through commit and publishes afterward.
  """
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
    from app.timeutil import now_naive_utc
    chat.archived_at = now_naive_utc()
    chat.pinned_at = None
    return True
  return False
