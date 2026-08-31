"""Durable, bounded project activity used by live collaboration cursors."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import models


PROJECT_CHANGE_LIMIT = 1000


def append_project_change(
  db: Session,
  *,
  project_id: str,
  kind: str,
  path: str | None,
  prior_path: str | None = None,
  revision: str | None = None,
  actor_key: str,
  display_name: str,
) -> models.ProjectChange:
  """Append one path-only event and deterministically retain the latest tail."""
  row = models.ProjectChange(
    project_id=project_id,
    kind=kind,
    path=path,
    prior_path=prior_path,
    revision=revision,
    actor_key=actor_key,
    display_name=display_name,
  )
  db.add(row)
  db.flush()
  cutoff = db.query(models.ProjectChange.id).filter(
    models.ProjectChange.project_id == project_id,
  ).order_by(models.ProjectChange.id.desc()).offset(
    PROJECT_CHANGE_LIMIT - 1,
  ).limit(1).scalar()
  if cutoff is not None:
    db.query(models.ProjectChange).filter(
      models.ProjectChange.project_id == project_id,
      models.ProjectChange.id < cutoff,
    ).delete(synchronize_session=False)
  return row


def project_change_view(row: models.ProjectChange) -> dict:
  """Return a JSON-native change view safe for HTTP and the raw system SSE."""
  return {
    "id": row.id,
    "kind": row.kind,
    "path": row.path,
    "prior_path": row.prior_path,
    "revision": row.revision,
    "actor_key": row.actor_key,
    "display_name": row.display_name,
    "created_at": row.created_at.isoformat() if row.created_at else None,
  }
