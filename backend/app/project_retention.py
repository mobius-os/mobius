"""Coordinated retention for soft-deleted first-class projects."""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


log = logging.getLogger(__name__)

# Project creation and TTL purge both mutate the owner-managed ``data/projects``
# namespace. Serializing them prevents an orphan sweep from observing a freshly
# created root in the narrow interval before its database row commits. Recovery
# uses the same lock so a project cannot cross the TTL boundary in both paths.
PROJECT_LIFECYCLE_LOCK = threading.RLock()


def _owned_native_root(project_id: str, root_path: str, legacy: object) -> Path | None:
  """Return a root only when it is exactly Möbius' native project directory."""
  if legacy is not None:
    return None
  data_root = Path(get_settings().data_dir).resolve()
  projects_root = data_root / "projects"
  expected = projects_root / str(project_id)
  stored = Path(root_path)
  lexical = stored if stored.is_absolute() else data_root / stored
  # Compare lexical absolute paths first. Resolving a hostile symlink and then
  # deleting that target would turn confinement into an escape primitive.
  try:
    if lexical.absolute() != expected.absolute():
      return None
    projects_root.resolve().relative_to(data_root)
  except (OSError, ValueError):
    return None
  return expected


def _remove_owned_root(root: Path) -> None:
  if root.is_symlink():
    root.unlink(missing_ok=True)
  elif root.exists():
    shutil.rmtree(root)


def _sweep_orphaned_native_roots(db: Session) -> None:
  """Retry cleanup left after a crash between DB commit and filesystem delete."""
  data_root = Path(get_settings().data_dir).resolve()
  projects_root = data_root / "projects"
  if not projects_root.is_dir() or projects_root.is_symlink():
    return
  live_ids = {
    str(project_id)
    for (project_id,) in db.query(models.Project.id).all()
  }
  for child in projects_root.iterdir():
    try:
      uuid.UUID(child.name)
    except (ValueError, AttributeError):
      continue
    if child.name in live_ids:
      continue
    try:
      _remove_owned_root(child)
    except OSError:
      log.exception("Could not remove orphaned native project root %s", child)


def purge_expired_project_tombstones(db: Session) -> list[str]:
  """Release expired project/chat pairs, preserving imported legacy storage.

  The project rows commit away before any filesystem deletion. Thus a failed
  transaction leaves the project fully recoverable. A crash after that commit
  may leave only an owner-managed orphan under ``data/projects``; the next sweep
  recognizes that exact UUID namespace and retries it. Legacy imports point into
  app storage and are never passed to filesystem cleanup.
  """
  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  with PROJECT_LIFECYCLE_LOCK:
    from app.shared_app_retention import (
      delete_project_shared_apps,
      purge_expired_shared_apps,
      remove_snapshot_root,
    )
    purge_expired_shared_apps(db)
    rows = db.query(models.Project).filter(
      models.Project.deleted_at.isnot(None),
      models.Project.deleted_at < cutoff,
    ).all()
    project_ids = [str(project.id) for project in rows]
    roots = [
      root for project in rows
      if (root := _owned_native_root(
        str(project.id), project.root_path, project.legacy_source_json,
      )) is not None
    ]
    shared_app_roots = delete_project_shared_apps(db, project_ids)
    if project_ids:
      # ProjectAgentMessage predates cascade ownership on some local builds.
      # Clear it explicitly so an upgraded database and a fresh database have
      # the same final-delete behavior.
      db.query(models.ProjectAgentMessage).filter(
        models.ProjectAgentMessage.project_id.in_(project_ids),
      ).delete(synchronize_session=False)
      db.query(models.Project).filter(
        models.Project.id.in_(project_ids),
        models.Project.deleted_at.isnot(None),
        models.Project.deleted_at < cutoff,
      ).delete(synchronize_session=False)
      # This is the point of no return. Do not touch roots before it succeeds.
      db.commit()
      db.expire_all()
      for root in roots:
        try:
          _remove_owned_root(root)
        except OSError:
          # The row is already gone, so the UUID orphan sweep can retry safely.
          log.exception("Could not remove expired native project root %s", root)
      for root in shared_app_roots:
        try:
          remove_snapshot_root(root)
        except OSError:
          log.exception("Could not remove project-owned shared-app snapshot %s", root)
    _sweep_orphaned_native_roots(db)
    return project_ids
