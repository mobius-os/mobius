"""Durable delegation submit/attach, status, history, and cancellation API."""

from __future__ import annotations

import re
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import models, providers
from app.chat_start import start_programmatic_chat_turn
from app.database import get_db
from app.config import get_settings
from app.delegations import (
  DelegationIntent,
  cancel_delegation_execution,
  create_or_attach_delegation,
  derived_status,
  MAX_DELEGATION_DEPTH,
  normalize_cwd,
  parent_root_run_id,
  serialize_delegation,
)
from app.deps import Principal, get_delegation_principal, reject_cross_site
from app.resource_access import get_active_chat_or_404


router = APIRouter(prefix="/api/delegations", tags=["delegations"])
_TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DelegationSubmit(BaseModel):
  app_id: int = Field(gt=0)
  parent_chat_id: str = Field(min_length=1, max_length=64)
  task_key: str = Field(min_length=1, max_length=128)
  prompt: str = Field(min_length=1, max_length=200_000)
  provider: str
  model: str | None = Field(default=None, max_length=256)
  effort: str | None = Field(default=None, max_length=32)
  scope: str
  cwd: str | None = Field(default=None, max_length=1024)
  # Wake the parent chat with the result when the child settles. Defaults on for
  # the owner-agent subagent path; a pure-poll caller can pass False.
  notify_parent_on_complete: bool = True

  @field_validator("task_key")
  @classmethod
  def _valid_task_key(cls, value: str) -> str:
    value = value.strip()
    if not _TASK_KEY_RE.fullmatch(value):
      raise ValueError(
        "task_key must start with a letter/number and use only . _ or -"
      )
    return value

  @field_validator("prompt")
  @classmethod
  def _clean_prompt(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("prompt must not be empty")
    return value

  @field_validator("provider")
  @classmethod
  def _valid_provider(cls, value: str) -> str:
    if value not in ("claude", "codex"):
      raise ValueError("provider must be claude or codex")
    return value

  @field_validator("scope")
  @classmethod
  def _valid_scope(cls, value: str) -> str:
    if value not in ("read", "write"):
      raise ValueError("scope must be read or write")
    return value


def _require_submitter(
  db: Session, principal: Principal, body: DelegationSubmit,
) -> models.Delegation | None:
  if principal.scope == "owner" and principal.app_id is None:
    return None
  if principal.scope not in {"app", "delegation"} or principal.app_id != body.app_id:
    raise HTTPException(
      status_code=403,
      detail="Only the owner agent or an attached delegated agent may submit work.",
    )
  parent = db.query(models.Delegation).filter(
    models.Delegation.child_chat_id == body.parent_chat_id,
    models.Delegation.app_id == principal.app_id,
  ).first()
  if parent is None:
    raise HTTPException(status_code=403, detail="Delegated work must stay under its parent child chat.")
  if parent.scope == "read" and body.scope != "read":
    raise HTTPException(
      status_code=403,
      detail="A read-only delegated owner cannot create write-capable children.",
    )
  if principal.delegation_id is not None and (
    principal.delegation_id != parent.id
    or principal.chat_id != body.parent_chat_id
  ):
    raise HTTPException(
      status_code=403,
      detail="Delegation token may only create direct children.",
    )
  from app.delegations import delegation_depth
  if delegation_depth(db, parent) >= MAX_DELEGATION_DEPTH:
    raise HTTPException(status_code=409, detail=f"Delegation depth reached the maximum ({MAX_DELEGATION_DEPTH}).")
  return parent


def _row_for_principal(
  db: Session, delegation_id: str, principal: Principal,
) -> models.Delegation:
  query = db.query(models.Delegation).filter(
    models.Delegation.id == delegation_id,
  )
  if principal.delegation_id is not None:
    query = query.filter(models.Delegation.parent_chat_id == principal.chat_id)
  elif principal.app_id is not None:
    query = query.filter(models.Delegation.app_id == principal.app_id)
  row = query.first()
  if row is None:
    raise HTTPException(status_code=404, detail="Delegation not found.")
  return row


async def _ensure_started(
  db: Session, row: models.Delegation, prompt: str,
) -> None:
  if (
    db.query(models.ChatRun.id)
    .filter(models.ChatRun.chat_id == row.child_chat_id)
    .first()
  ) is not None:
    return
  started = await start_programmatic_chat_turn(
    chat_id=row.child_chat_id,
    title=f"Delegation · {row.task_key}",
    content=prompt,
    provider=row.provider,
    initiated_by_app_id=row.app_id,
  )
  if not started:
    # Another identical submit already owns the transient start claim. Return
    # the same durable identity in "starting" state; the helper polls it rather
    # than treating ordinary idempotent contention as a failed delegation.
    db.rollback()
    db.expire_all()
    return
  # The request Session queried the child before the writer actor committed its
  # StartTurn on a separate connection. End that read snapshot so derived
  # status observes the just-created ChatRun instead of reporting "starting".
  db.rollback()
  db.expire_all()


@router.post("", status_code=201, dependencies=[Depends(reject_cross_site)])
async def submit_or_attach(
  body: DelegationSubmit,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Create once per (parent logical run, task key), otherwise attach."""
  _require_submitter(db, principal, body)
  parent = get_active_chat_or_404(db, body.parent_chat_id)
  root_id = parent_root_run_id(db, parent.id, require_active=True)
  if root_id is None:
    raise HTTPException(
      status_code=409,
      detail="Delegation requires an active parent chat run.",
    )
  app = db.query(models.App).filter(
    models.App.id == body.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(status_code=404, detail="Delegation owner app not found.")
  if body.model and providers._model_belongs_to_other_provider(
    body.model, body.provider,
  ):
    raise HTTPException(
      status_code=422,
      detail="The selected model does not belong to that provider.",
    )
  try:
    cwd = normalize_cwd(body.cwd)
  except ValueError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc

  from app import chat_queue
  async with (
    chat_queue.get_transition_lock(f"app-lifecycle:{body.app_id}"),
    chat_queue.get_transition_lock(parent.id),
  ):
    # App/chat deletion uses these same gates. End the authentication/read
    # snapshot and re-establish every admission fact under the locks so a
    # child cannot start after either owner has begun tombstoning.
    db.rollback()
    app = db.query(models.App).filter(
      models.App.id == body.app_id,
      models.App.deleted_at.is_(None),
    ).first()
    if app is None:
      raise HTTPException(
        status_code=404, detail="Delegation owner app not found.",
      )
    parent = get_active_chat_or_404(db, body.parent_chat_id)
    root_id = parent_root_run_id(db, parent.id, require_active=True)
    if root_id is None:
      raise HTTPException(
        status_code=409,
        detail="Delegation requires an active parent chat run.",
      )
    intent = DelegationIntent(
      app_id=body.app_id,
      parent_chat_id=parent.id,
      parent_root_run_id=root_id,
      task_key=body.task_key,
      prompt=body.prompt,
      provider=body.provider,
      model=body.model,
      effort=body.effort,
      scope=body.scope,
      cwd=cwd,
      max_budget_usd=5.0 if body.provider == "claude" else None,
      notify_parent_on_complete=body.notify_parent_on_complete,
    )
    try:
      row, attached = create_or_attach_delegation(db, intent)
    except ValueError as exc:
      raise HTTPException(
        status_code=409,
        detail=(
          "That task key is already attached to different immutable work. "
          "Reuse the original prompt/policy or choose a new task key."
        ),
      ) from exc

    await _ensure_started(db, row, body.prompt)
    from app.goal_plans import publish_plan_for_delegation
    publish_plan_for_delegation(db, row)
  payload = serialize_delegation(db, row)
  payload["attached"] = attached
  return payload


@router.get("/capabilities")
async def delegation_capabilities(
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Read-only Subagents configuration for a confined delegated owner."""
  if principal.delegation_id is None or principal.app_id is None:
    raise HTTPException(status_code=403, detail="Delegated child token required.")
  app = db.query(models.App).filter(
    models.App.id == principal.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(status_code=403, detail="Delegation owner app is unavailable.")

  def read_json(name: str) -> dict:
    path = Path(get_settings().data_dir) / "apps" / str(app.id) / name
    try:
      value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      return {}
    return value if isinstance(value, dict) else {}

  connections = {}
  for provider_id, provider in providers.PROVIDERS.items():
    error = provider.check_auth(get_settings().data_dir)
    connections[provider_id] = {
      "configured": error is None,
      "authenticated": error is None,
      "error": error,
    }
  registry = await providers.list_models(get_settings().data_dir)
  models_by_provider = {
    provider_id: [
      {"id": entry["id"], "name": entry["label"]}
      for entry in entries
    ]
    for provider_id, entries in registry.items()
  }
  return {
    "app_id": app.id,
    "config": read_json("config.json"),
    "runtime": read_json("status.json"),
    "connections": connections,
    "models": models_by_provider,
  }


@router.get("")
def list_delegations(
  app_id: int | None = Query(default=None, gt=0),
  parent_chat_id: str | None = Query(default=None, min_length=1, max_length=64),
  limit: int = Query(default=100, ge=1, le=500),
  offset: int = Query(default=0, ge=0),
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  query = db.query(models.Delegation)
  if principal.delegation_id is not None:
    query = query.filter(models.Delegation.parent_chat_id == principal.chat_id)
  elif principal.app_id is not None:
    query = query.filter(models.Delegation.app_id == principal.app_id)
  elif app_id is not None:
    query = query.filter(models.Delegation.app_id == app_id)
  if parent_chat_id is not None:
    query = query.filter(models.Delegation.parent_chat_id == parent_chat_id)
  rows = query.order_by(models.Delegation.created_at.desc()).offset(offset).limit(limit).all()
  return {
    "items": [
      serialize_delegation(db, row, include_result=False) for row in rows
    ]
  }


@router.get("/{delegation_id}")
def get_delegation(
  delegation_id: str,
  include_history: bool = Query(default=False),
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  row = _row_for_principal(db, delegation_id, principal)
  payload = serialize_delegation(db, row)
  if include_history:
    child = db.query(models.Chat).filter(models.Chat.id == row.child_chat_id).first()
    payload["history"] = list(child.messages or []) if child is not None else []
  return payload


@router.post(
  "/{delegation_id}/cancel",
  dependencies=[Depends(reject_cross_site)],
)
async def cancel_delegation(
  delegation_id: str,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  row = _row_for_principal(db, delegation_id, principal)
  status, _, _ = derived_status(db, row)
  if status in ("running", "resuming", "paused", "starting"):
    if not await cancel_delegation_execution(row.id):
      raise HTTPException(
        status_code=409,
        detail="The child is still stopping; retry cancellation shortly.",
      )
    db.rollback()
    row = _row_for_principal(db, delegation_id, principal)
  payload = serialize_delegation(db, row)
  from app.goal_plans import publish_plan_for_delegation
  publish_plan_for_delegation(db, row)
  return payload


async def cancel_active_for_parent(db: Session, parent_chat_id: str) -> list[str]:
  """Cascade an explicit parent Stop without affecting restart draining."""
  rows = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == parent_chat_id,
    models.Delegation.cancelled_at.is_(None),
  ).all()
  cancelled: list[str] = []
  for row in rows:
    status, _, _ = derived_status(db, row)
    if status not in ("running", "resuming", "paused", "starting"):
      continue
    if await cancel_delegation_execution(row.id):
      cancelled.append(row.id)
  db.rollback()
  return cancelled
