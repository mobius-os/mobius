"""Connector registry routes: owner-gated CRUD over external MCP services.

Add runs the MCP handshake BEFORE anything is saved — the owner sees the
tool list and token estimate (or a concrete failure) at review time, and a
service that can't be reached never becomes a row. Secrets are write-only:
requests carry them, responses never do (INV mirrors github.py's token
handling). Registry edits apply on the next agent turn — the runners read
enabled connectors fresh each turn, so there is nothing to restart.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import connectors as core
from app import models
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)

router = APIRouter(
  prefix="/api/connectors",
  tags=["connectors"],
  dependencies=[Depends(reject_cross_site)],
)

_MAX_CONNECTORS = 32


class ConnectorCreate(BaseModel):
  url: str = Field(min_length=8, max_length=2048)
  name: str = Field(default="", max_length=128)
  auth_header: str = Field(default="", max_length=64)
  auth_value: str = Field(default="", max_length=4096)


class ConnectorPatch(BaseModel):
  enabled: bool | None = None
  name: str | None = Field(default=None, min_length=1, max_length=128)


def _tool_preview(tools: list) -> list[dict]:
  """Name + first-sentence description only — the full schemas stay
  server-side; Settings needs the catalog, not the payload."""
  preview = []
  for tool in tools[:64]:
    if isinstance(tool, dict):
      description = str(tool.get("description") or "")
      preview.append({
        "name": str(tool.get("name") or ""),
        "description": description.split(". ")[0][:160],
      })
  return preview


def _public(row: models.Connector) -> dict:
  tools = row.tools_json if isinstance(row.tools_json, list) else []
  return {
    "id": row.id,
    "slug": row.slug,
    "name": row.name,
    "url": row.url,
    "enabled": row.enabled,
    "has_auth": bool(row.auth_header and row.auth_value_encrypted),
    "auth_header": row.auth_header,
    "tool_count": len(tools),
    "tools": _tool_preview(tools),
    "est_tokens": row.est_tokens,
    "status": row.status,
    "status_detail": row.status_detail,
    "last_checked_at": (
      row.last_checked_at.isoformat() if row.last_checked_at else None
    ),
  }


def _unique_slug(db: Session, base: str) -> str:
  slug = base
  suffix = 2
  while db.query(models.Connector).filter(models.Connector.slug == slug).first():
    slug = f"{base}_{suffix}"
    suffix += 1
  return slug


def _get_row(db: Session, connector_id: int) -> models.Connector:
  row = db.query(models.Connector).filter(
    models.Connector.id == connector_id
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="Connector not found.")
  return row


@router.get("")
async def list_connectors(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  rows = db.query(models.Connector).order_by(models.Connector.id).all()
  return {"connectors": [_public(r) for r in rows]}


@router.post("", status_code=201)
async def add_connector(
  body: ConnectorCreate,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  if db.query(models.Connector).count() >= _MAX_CONNECTORS:
    raise HTTPException(status_code=400, detail="Connector limit reached.")
  url = body.url.strip()
  if not url.startswith("https://") and not url.startswith("http://localhost"):
    raise HTTPException(
      status_code=400, detail="Connector URLs must use https."
    )
  auth_header = body.auth_header.strip() or (
    "Authorization" if body.auth_value.strip() else ""
  )
  auth_value = body.auth_value.strip()

  try:
    probe = await core.handshake(url, auth_header or None, auth_value or None)
  except core.ConnectorError as exc:
    raise HTTPException(status_code=422, detail=str(exc))

  name = body.name.strip() or probe["name"] or url.split("//", 1)[-1].split("/")[0]
  row = models.Connector(
    slug=_unique_slug(db, core.slugify(name)),
    name=name[:128],
    url=url,
    auth_header=auth_header or None,
    auth_value_encrypted=(
      core.encrypt_secret(auth_value) if auth_header and auth_value else None
    ),
    enabled=True,
    tools_json=probe["tools"],
    est_tokens=probe["est_tokens"],
    status="ok",
    status_detail=None,
    last_checked_at=now_naive_utc(),
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  log.info("connector added: %s (%d tools)", row.slug, len(probe["tools"]))
  return _public(row)


@router.patch("/{connector_id}")
async def patch_connector(
  connector_id: int,
  body: ConnectorPatch,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  row = _get_row(db, connector_id)
  if body.enabled is not None:
    row.enabled = body.enabled
  if body.name is not None:
    row.name = body.name.strip()[:128]
  db.commit()
  db.refresh(row)
  return _public(row)


@router.post("/{connector_id}/refresh")
async def refresh_connector(
  connector_id: int,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  row = _get_row(db, connector_id)
  secret = None
  if row.auth_header and row.auth_value_encrypted:
    try:
      secret = core.decrypt_secret(row.auth_value_encrypted)
    except core.ConnectorError as exc:
      raise HTTPException(status_code=409, detail=str(exc))
  try:
    probe = await core.handshake(row.url, row.auth_header, secret)
    row.tools_json = probe["tools"]
    row.est_tokens = probe["est_tokens"]
    row.status = "ok"
    row.status_detail = None
  except core.ConnectorError as exc:
    row.status = "error"
    row.status_detail = str(exc)
  row.last_checked_at = now_naive_utc()
  db.commit()
  db.refresh(row)
  return _public(row)


@router.delete("/{connector_id}")
async def delete_connector(
  connector_id: int,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  row = _get_row(db, connector_id)
  db.delete(row)
  db.commit()
  return {"ok": True}
