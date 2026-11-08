"""Coordinated retention for soft-deleted first-class projects."""

from __future__ import annotations

import logging
import shutil
import threading
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def serialize_project_lifecycle(project_id: str | None):
  """Keep a project's and its children's deletion/recovery cleanup ordered.

  Acquisition order is project async gate, then any chat async gate, then
  PROJECT_LIFECYCLE_LOCK and drawer commit locks. Never await under the latter
  two. Child lifecycle callers enter here before Stop (which takes its chat
  gate); independent chats have no project gate. Follower delivery happens
  only after lifecycle ownership is released.
  """
  if project_id is None:
    yield
    return
  from app.chat_queue import get_transition_lock
  async with get_transition_lock(f"project-lifecycle:{project_id}"):
    yield


def projects_using_app_files(db: Session, app: models.App) -> list[models.Project]:
  """Find projects whose source would disappear if this app were purged.

  A template provider is not necessarily the source owner. Linked builder
  outputs may live in another app's source or in Pages' numeric storage, so
  ownership follows the root rather than the provider foreign key. Include
  tombstones: their files are still needed for Project recovery.
  """
  data_root = Path(get_settings().data_dir).resolve()
  source = Path(app.source_dir)
  app_roots = (
    (source if source.is_absolute() else data_root / source).resolve(),
    (data_root / "apps" / str(app.id)).resolve(),
  )
  dependent = []
  for project in db.query(models.Project).all():
    stored = Path(project.root_path)
    root = (stored if stored.is_absolute() else data_root / stored).resolve()
    if any(root.is_relative_to(app_root) for app_root in app_roots):
      dependent.append(project)
  return dependent


def _owned_native_root(project_id: str, root_path: str) -> Path | None:
  """Return a root only when it is exactly Möbius' native project directory."""
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


RETIRED_ROOT_PREFIX = ".purging-"


def retire_owned_root(root: Path) -> None:
  """Delete an owned root whose database row has already committed away.

  The root is renamed to a ``.purging-`` sibling before deletion, so a delete
  that fails or is interrupted leaves an explicitly marked leftover for
  ``sweep_retired_roots`` to finish. Leftover cleanup never infers ownership
  from a missing database row: an empty, restored, or misdirected database
  would otherwise erase every owner folder it does not know about. If the
  rename itself fails, the folder keeps its name and stays on disk.
  """
  if root.is_symlink():
    root.unlink(missing_ok=True)
    return
  if not root.exists():
    return
  retired = root.with_name(RETIRED_ROOT_PREFIX + root.name)
  if retired.exists():
    shutil.rmtree(retired)
  root.rename(retired)
  shutil.rmtree(retired)


def sweep_retired_roots(parent: Path) -> None:
  """Finish deletions that ``retire_owned_root`` marked but did not complete."""
  if not parent.is_dir() or parent.is_symlink():
    return
  for child in parent.iterdir():
    if not child.name.startswith(RETIRED_ROOT_PREFIX):
      continue
    try:
      if child.is_symlink():
        child.unlink(missing_ok=True)
      else:
        shutil.rmtree(child)
    except OSError:
      log.exception("Could not finish removing retired root %s", child)


def purge_expired_project_tombstones(db: Session) -> list[str]:
  """Release expired project/chat pairs, preserving externally owned roots.

  The project rows commit away before any filesystem deletion. Thus a failed
  transaction leaves the project fully recoverable. A failed or interrupted
  delete after that commit leaves a marked ``.purging-`` leftover under
  ``data/projects`` that the next pass finishes. Linked roots outside that
  namespace are never passed to filesystem cleanup.
  """
  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  with PROJECT_LIFECYCLE_LOCK:
    from app.shared_app_retention import (
      delete_project_shared_apps,
      purge_expired_shared_apps,
    )
    purge_expired_shared_apps(db)
    # Source-copy links expire independently of the original project. Drop the
    # potentially large immutable payload during the existing retention pass.
    db.query(models.ProjectSourceCopy).filter(
      models.ProjectSourceCopy.expires_at <= now_naive_utc(),
      models.ProjectSourceCopy.package_json.is_not(None),
    ).update({"package_json": None}, synchronize_session=False)
    db.commit()
    rows = db.query(models.Project).filter(
      models.Project.deleted_at.isnot(None),
      models.Project.deleted_at < cutoff,
    ).all()
    project_ids = [str(project.id) for project in rows]
    roots = [
      root for project in rows
      if (root := _owned_native_root(
        str(project.id), project.root_path,
      )) is not None
    ]
    shared_app_roots = delete_project_shared_apps(db, project_ids)
    if project_ids:
      db.query(models.ProjectSourceCopy).filter(
        models.ProjectSourceCopy.project_id.in_(project_ids),
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
          retire_owned_root(root)
        except OSError:
          log.exception("Could not remove expired native project root %s", root)
      for root in shared_app_roots:
        try:
          retire_owned_root(root)
        except OSError:
          log.exception("Could not remove project-owned shared-app snapshot %s", root)
    sweep_retired_roots(Path(get_settings().data_dir).resolve() / "projects")
    return project_ids
