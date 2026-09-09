"""Retention and filesystem ownership for pinned shared-app builds."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.project_retention import PROJECT_LIFECYCLE_LOCK
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


log = logging.getLogger(__name__)


def stage_project_shared_app_delete(db: Session, project_id: str, deleted_at) -> None:
  """Make a Project and only its currently-live shared apps one recovery unit."""
  instance_ids = [
    str(instance_id) for (instance_id,) in db.query(models.SharedAppInstance.id).filter(
      models.SharedAppInstance.project_id == project_id,
      models.SharedAppInstance.deleted_at.is_(None),
    ).all()
  ]
  if not instance_ids:
    return
  db.query(models.SharedAppInstance).filter(
    models.SharedAppInstance.id.in_(instance_ids),
  ).update({models.SharedAppInstance.deleted_at: deleted_at}, synchronize_session=False)
  db.query(models.SharedAppInvite).filter(
    models.SharedAppInvite.instance_id.in_(instance_ids),
    models.SharedAppInvite.revoked_at.is_(None),
  ).update({models.SharedAppInvite.revoked_at: deleted_at}, synchronize_session=False)
  db.query(models.SharedAppMember).filter(
    models.SharedAppMember.instance_id.in_(instance_ids),
    models.SharedAppMember.revoked_at.is_(None),
  ).update({
    models.SharedAppMember.revoked_at: deleted_at,
    models.SharedAppMember.token_epoch: models.SharedAppMember.token_epoch + 1,
  }, synchronize_session=False)


def stage_project_shared_app_recovery(db: Session, project_id: str, deleted_at) -> None:
  """Recover instances deleted with a Project; revoked access stays revoked."""
  db.query(models.SharedAppInstance).filter(
    models.SharedAppInstance.project_id == project_id,
    models.SharedAppInstance.deleted_at == deleted_at,
  ).update({models.SharedAppInstance.deleted_at: None}, synchronize_session=False)


def owned_snapshot_root(instance_id: str, snapshot_path: str) -> Path | None:
  data_root = Path(get_settings().data_dir).resolve()
  instances_root = data_root / "shared" / "app-instances"
  expected = instances_root / str(instance_id)
  stored = Path(snapshot_path)
  lexical = stored if stored.is_absolute() else data_root / stored
  try:
    if lexical.absolute() != (expected / "build").absolute():
      return None
    instances_root.resolve().relative_to(data_root)
  except (OSError, ValueError):
    return None
  return expected


def remove_snapshot_root(root: Path) -> None:
  if root.is_symlink():
    root.unlink(missing_ok=True)
  elif root.exists():
    shutil.rmtree(root)


def _sweep_orphaned_snapshot_roots(db: Session) -> None:
  instances_root = Path(get_settings().data_dir).resolve() / "shared" / "app-instances"
  if not instances_root.is_dir() or instances_root.is_symlink():
    return
  live_ids = {str(instance_id) for (instance_id,) in db.query(models.SharedAppInstance.id).all()}
  for child in instances_root.iterdir():
    try:
      uuid.UUID(child.name)
    except (ValueError, AttributeError):
      continue
    if child.name in live_ids:
      continue
    try:
      remove_snapshot_root(child)
    except OSError:
      log.exception("Could not remove orphaned shared-app snapshot %s", child)


def purge_expired_shared_apps(db: Session) -> list[str]:
  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  with PROJECT_LIFECYCLE_LOCK:
    rows = db.query(models.SharedAppInstance).filter(
      models.SharedAppInstance.deleted_at.isnot(None),
      models.SharedAppInstance.deleted_at < cutoff,
    ).all()
    ids = [str(row.id) for row in rows]
    roots = [
      root for row in rows
      if (root := owned_snapshot_root(str(row.id), row.snapshot_path)) is not None
    ]
    if ids:
      db.query(models.SharedAppInstance).filter(
        models.SharedAppInstance.id.in_(ids),
        models.SharedAppInstance.deleted_at.isnot(None),
        models.SharedAppInstance.deleted_at < cutoff,
      ).delete(synchronize_session=False)
      db.commit()
      db.expire_all()
      for root in roots:
        try:
          remove_snapshot_root(root)
        except OSError:
          log.exception("Could not remove expired shared-app snapshot %s", root)
    _sweep_orphaned_snapshot_roots(db)
    return ids


def delete_project_shared_apps(db: Session, project_ids: list[str]) -> list[Path]:
  """Delete instance rows in the caller's transaction and return owned roots."""
  if not project_ids:
    return []
  rows = db.query(models.SharedAppInstance).filter(
    models.SharedAppInstance.project_id.in_(project_ids),
  ).all()
  roots = [
    root for row in rows
    if (root := owned_snapshot_root(str(row.id), row.snapshot_path)) is not None
  ]
  if rows:
    db.query(models.SharedAppInstance).filter(
      models.SharedAppInstance.id.in_([row.id for row in rows]),
    ).delete(synchronize_session=False)
  return roots
