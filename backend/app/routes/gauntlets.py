"""Gauntlet create, status, listing, and cancellation API."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import chat_queue, models, providers
from app.database import get_db
from app.delegations import normalize_cwd, parent_root_run_id
from app.deps import Principal, get_principal, reject_cross_site
from app.gauntlets import (
  ActiveGauntletControllerConflict,
  ActiveGauntletConflict,
  new_gauntlet_run,
  reconcile_gauntlet,
  serialize_gauntlet,
  stop_gauntlet,
)
from app.resource_access import get_active_chat_or_404


router = APIRouter(prefix="/api/gauntlets", tags=["gauntlets"])
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CriticRole(BaseModel):
  key: str = Field(min_length=1, max_length=64)
  focus: str = Field(min_length=1, max_length=1000)

  @field_validator("key")
  @classmethod
  def _valid_key(cls, value: str) -> str:
    value = value.strip()
    if not _ROLE_RE.fullmatch(value):
      raise ValueError("critic key must use letters, numbers, dot, _ or -")
    return value

  @field_validator("focus")
  @classmethod
  def _clean_focus(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("critic focus must not be empty")
    return value


def _default_roles() -> list[CriticRole]:
  return [
    CriticRole(
      key="visual-direction",
      focus=(
        "visual hierarchy, fidelity to any named references, readability, "
        "and finish"
      ),
    ),
    CriticRole(
      key="interaction-feel",
      focus="controls, feedback, pacing, game feel, and the core test",
    ),
    CriticRole(
      key="reliability-access",
      focus="responsive navigation, performance, errors, and accessibility",
    ),
  ]


class GauntletCreate(BaseModel):
  run_id: str = Field(min_length=1, max_length=64)
  app_id: int = Field(gt=0)
  parent_chat_id: str = Field(min_length=1, max_length=64)
  target: str = Field(min_length=1, max_length=256)
  target_path: str = Field(min_length=1, max_length=1024)
  references: list[str] = Field(default_factory=list, max_length=12)
  core_test: str = Field(min_length=1, max_length=4000)
  constraints: list[str] = Field(default_factory=list, max_length=20)
  critic_roles: list[CriticRole] = Field(
    default_factory=_default_roles, min_length=2, max_length=4,
  )
  provider: str
  model: str | None = Field(default=None, max_length=256)
  effort: str | None = Field(default=None, max_length=32)
  max_rounds: int = Field(default=3, ge=1, le=5)
  max_hours: float | None = Field(default=8.0, ge=0.01, le=72.0)
  # Claude's provider API accepts millidollar execution caps. Keep enough room
  # for the largest four-way critic barrier without manufacturing a per-task
  # minimum above the requested workflow ceiling.
  max_budget_usd: float | None = Field(default=None, ge=0.01, le=10_000)
  allow_replacement: bool = False

  @field_validator("run_id")
  @classmethod
  def _valid_run_id(cls, value: str) -> str:
    value = value.strip()
    if not _ID_RE.fullmatch(value):
      raise ValueError("run_id must use letters, numbers, dot, _ or -")
    return value

  @field_validator("target", "target_path", "core_test")
  @classmethod
  def _clean_required_text(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("field must not be empty")
    return value

  @field_validator("references")
  @classmethod
  def _clean_references(cls, values: list[str]) -> list[str]:
    cleaned = [value.strip() for value in values if value.strip()]
    if any(len(value) > 2000 for value in cleaned):
      raise ValueError("each reference must be at most 2000 characters")
    return cleaned

  @field_validator("constraints")
  @classmethod
  def _clean_constraints(cls, values: list[str]) -> list[str]:
    cleaned = [value.strip() for value in values if value.strip()]
    if any(len(value) > 2000 for value in cleaned):
      raise ValueError("each constraint must be at most 2000 characters")
    return cleaned

  @field_validator("critic_roles")
  @classmethod
  def _unique_roles(cls, values: list[CriticRole]) -> list[CriticRole]:
    keys = [role.key for role in values]
    if len(set(keys)) != len(keys):
      raise ValueError("critic role keys must be unique")
    return values

  @field_validator("provider")
  @classmethod
  def _valid_provider(cls, value: str) -> str:
    if value not in ("claude", "codex"):
      raise ValueError("provider must be claude or codex")
    return value


def _require_owner_create(principal: Principal) -> None:
  if principal.app_id is not None or principal.scope != "owner":
    raise HTTPException(
      status_code=403,
      detail="Only the owner agent may start a Gauntlet.",
    )


def _row_for_principal(
  db: Session, run_id: str, principal: Principal,
) -> models.GauntletRun:
  query = db.query(models.GauntletRun).filter(
    models.GauntletRun.id == run_id,
  )
  if principal.app_id is not None:
    query = query.filter(models.GauntletRun.app_id == principal.app_id)
  row = query.first()
  if row is None:
    # Cross-app reads are deliberately indistinguishable from absence.
    raise HTTPException(status_code=404, detail="Gauntlet not found.")
  return row


@router.post("", status_code=201, dependencies=[Depends(reject_cross_site)])
async def create_gauntlet(
  body: GauntletCreate,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  _require_owner_create(principal)
  parent = get_active_chat_or_404(db, body.parent_chat_id)
  if parent.created_by_app_id != body.app_id:
    raise HTTPException(
      status_code=409,
      detail="Gauntlet controller chat is not owned by the specified app.",
    )
  if (parent.provider or "claude") != body.provider:
    raise HTTPException(
      status_code=409,
      detail=(
        "Gauntlet provider must match the active controller provider. "
        "Create the controller with the selected provider first."
      ),
    )
  app = db.query(models.App).filter(
    models.App.id == body.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(status_code=404, detail="Gauntlet app not found.")
  if body.model and providers._model_belongs_to_other_provider(
    body.model, body.provider,
  ):
    raise HTTPException(
      status_code=422,
      detail="The selected model does not belong to that provider.",
    )
  try:
    target_path = normalize_cwd(body.target_path)
  except ValueError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc
  contract = {
    "target": body.target,
    "target_path": target_path,
    "references": body.references,
    "core_test": body.core_test,
    "constraints": body.constraints,
    "critic_roles": [role.model_dump() for role in body.critic_roles],
    "provider": body.provider,
    "model": body.model,
    "effort": body.effort,
    "max_rounds": body.max_rounds,
    "max_hours": body.max_hours,
    "max_budget_usd": body.max_budget_usd,
    "allow_replacement": body.allow_replacement,
  }
  # Serialize arming with ordinary controller sends. The send path rechecks
  # the active-Gauntlet fence under this same lock, so whichever wins fully
  # establishes the state observed by the other rather than allowing one last
  # unowned continuation to slip into the writer lineage.
  async with (
    chat_queue.get_transition_lock(f"app-lifecycle:{body.app_id}"),
    chat_queue.get_transition_lock(parent.id),
  ):
    db.rollback()
    existing = db.query(models.GauntletRun).filter(
      models.GauntletRun.id == body.run_id,
    ).first()
    app = db.query(models.App).filter(
      models.App.id == body.app_id,
      models.App.deleted_at.is_(None),
    ).first()
    if app is None:
      raise HTTPException(status_code=404, detail="Gauntlet app not found.")
    parent = get_active_chat_or_404(db, body.parent_chat_id)
    if parent.created_by_app_id != body.app_id:
      raise HTTPException(
        status_code=409,
        detail="Gauntlet controller chat is not owned by the specified app.",
      )
    if (parent.provider or "claude") != body.provider:
      raise HTTPException(
        status_code=409,
        detail=(
          "Gauntlet provider must match the active controller provider. "
          "Create the controller with the selected provider first."
        ),
      )
    root_id = (
      existing.parent_root_run_id
      if existing is not None
      else parent_root_run_id(db, parent.id, require_active=True)
    )
    if root_id is None:
      raise HTTPException(
        status_code=409,
        detail="A Gauntlet must be launched from an active controller run.",
      )
    try:
      row, attached = new_gauntlet_run(
        db,
        run_id=body.run_id,
        app_id=body.app_id,
        parent_chat_id=parent.id,
        parent_root_run_id=root_id,
        target_path=target_path,
        contract=contract,
        provider=body.provider,
        model=body.model,
        effort=body.effort,
        max_rounds=body.max_rounds,
        max_hours=body.max_hours,
        max_budget_usd=body.max_budget_usd,
      )
    except ActiveGauntletControllerConflict as exc:
      raise HTTPException(
        status_code=409,
        detail={
          "message": "That controller already has an active Gauntlet.",
          "active_run_id": exc.active_run_id,
        },
      ) from exc
    except ActiveGauntletConflict as exc:
      raise HTTPException(
        status_code=409,
        detail={
          "message": "That target already has an active Gauntlet.",
          "active_run_id": exc.active_run_id,
        },
      ) from exc
    except ValueError as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  payload = await reconcile_gauntlet(row.id)
  if payload is None:
    raise HTTPException(status_code=500, detail="Gauntlet did not persist.")
  payload["attached"] = attached
  return payload


@router.get("")
def list_gauntlets(
  app_id: int | None = Query(default=None, gt=0),
  status: str | None = Query(default=None, max_length=24),
  limit: int = Query(default=100, ge=1, le=500),
  offset: int = Query(default=0, ge=0),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  query = db.query(models.GauntletRun)
  if principal.app_id is not None:
    query = query.filter(models.GauntletRun.app_id == principal.app_id)
  elif app_id is not None:
    query = query.filter(models.GauntletRun.app_id == app_id)
  if status is not None:
    query = query.filter(models.GauntletRun.status == status)
  rows = query.order_by(
    models.GauntletRun.created_at.desc(), models.GauntletRun.id.desc(),
  ).offset(offset).limit(limit).all()
  return {
    "items": [serialize_gauntlet(db, row, include_results=False) for row in rows]
  }


@router.get("/{run_id}")
def get_gauntlet(
  run_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  row = _row_for_principal(db, run_id, principal)
  return serialize_gauntlet(db, row)


@router.post("/{run_id}/stop", dependencies=[Depends(reject_cross_site)])
async def stop_gauntlet_route(
  run_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  _row_for_principal(db, run_id, principal)
  payload = await stop_gauntlet(run_id)
  if payload is None:
    raise HTTPException(status_code=404, detail="Gauntlet not found.")
  return payload
