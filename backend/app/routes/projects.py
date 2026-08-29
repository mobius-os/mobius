"""Projects — a light, general "named project workspaces" registry.

This is a SHARED platform primitive, not a website builder. It owns only the
*list* of an app's projects ({id, name, created_at, updated_at}); an app keeps
its project files in its own storage (conventionally under a `projects/<id>/`
prefix) and does its own building/publishing/previewing. Any app can use
`window.mobius.projects` to offer named workspaces without hand-rolling a
registry.

Deliberately NOT here: files, build, publish, templates, preview. Those are
app concerns (or existing platform features like storage / run-job / publish).
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site
from app.source_dirs import apps_root

router = APIRouter(prefix="/api/projects", tags=["projects"])

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProjectCreate(BaseModel):
  name: str = Field(default="Untitled", max_length=200)
  id: str | None = Field(default=None, max_length=64)


class ProjectRename(BaseModel):
  name: str = Field(min_length=1, max_length=200)


def _require_app(
  db: Session, principal: Principal, app_id_hint: int | None = None,
) -> models.App:
  """Resolve the owning app. The app runtime token names its own app; the owner
  token is trusted and may name it via `app_id_hint`."""
  if principal.app_id is not None:
    app_id = principal.app_id
  elif app_id_hint is not None:
    app_id = app_id_hint
  else:
    raise HTTPException(
      status_code=403,
      detail="Projects require an app runtime token (or owner + app_id).",
    )
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


def _registry_path(app: models.App) -> Path:
  return apps_root(get_settings().data_dir) / str(app.id) / "projects.json"


def _read(app: models.App) -> list[dict]:
  try:
    data = json.loads(_registry_path(app).read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return []
  return [p for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []


def _write(app: models.App, rows: list[dict]) -> None:
  path = _registry_path(app)
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
  tmp.replace(path)


def _now() -> str:
  return datetime.now(timezone.utc).isoformat()


@router.get("")
def list_projects(
  app_id: int | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  app = _require_app(db, principal, app_id_hint=app_id)
  rows = _read(app)
  rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
  return rows


@router.post("", status_code=201, dependencies=[Depends(reject_cross_site)])
def create_project(
  body: ProjectCreate,
  app_id: int | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  app = _require_app(db, principal, app_id_hint=app_id)
  rows = _read(app)

  pid = (body.id or "").strip() or secrets.token_hex(6)
  if not _ID_RE.match(pid):
    raise HTTPException(status_code=422, detail="Invalid project id.")
  existing = next((r for r in rows if r.get("id") == pid), None)
  if existing is not None:
    # Idempotent for a caller-supplied id (e.g. the app's fixed "default").
    return existing

  name = (body.name or "").strip() or "Untitled"
  row = {"id": pid, "name": name, "created_at": _now(), "updated_at": _now()}
  rows.append(row)
  _write(app, rows)
  return row


@router.patch("/{project_id}", dependencies=[Depends(reject_cross_site)])
def rename_project(
  project_id: str,
  body: ProjectRename,
  app_id: int | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  app = _require_app(db, principal, app_id_hint=app_id)
  rows = _read(app)
  row = next((r for r in rows if r.get("id") == project_id), None)
  if row is None:
    raise HTTPException(status_code=404, detail="Project not found.")
  row["name"] = body.name.strip() or row.get("name") or "Untitled"
  row["updated_at"] = _now()
  _write(app, rows)
  return row


@router.delete("/{project_id}", dependencies=[Depends(reject_cross_site)])
def remove_project(
  project_id: str,
  app_id: int | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Remove the registry entry only. The app owns (and clears) the project's
  files through storage — the primitive never touches app file trees."""
  app = _require_app(db, principal, app_id_hint=app_id)
  rows = _read(app)
  kept = [r for r in rows if r.get("id") != project_id]
  if len(kept) == len(rows):
    raise HTTPException(status_code=404, detail="Project not found.")
  _write(app, kept)
  return {"removed": True, "id": project_id}
