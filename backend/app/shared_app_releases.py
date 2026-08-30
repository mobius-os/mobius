"""Immutable, fsynced build releases owned by shared-app database pointers."""

from __future__ import annotations

import mimetypes
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from weakref import WeakValueDictionary

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.path_utils import validate_path_within_base
from app.shared_app_retention import owned_snapshot_root, remove_snapshot_root
from app.storage_io import sync_directory, sync_file_tree
from app.timeutil import now_naive_utc


_RELEASES_DIR = "releases"
_STAGING_PREFIX = ".release-staging-"
SNAPSHOT_BYTES_MAX = 100 * 1024 * 1024
_locks: "WeakValueDictionary[str, threading.RLock]" = WeakValueDictionary()
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class SharedAppRelease:
  release_id: str
  snapshot_path: str
  entry_path: str
  name: str
  published_at: str


def release_lock(instance_id: str) -> threading.RLock:
  with _locks_guard:
    lock = _locks.get(instance_id)
    if lock is None:
      lock = threading.RLock()
      _locks[instance_id] = lock
    return lock


def _instance_root(instance_id: str, snapshot_path: str) -> Path:
  root = owned_snapshot_root(instance_id, snapshot_path)
  if root is None or root.is_symlink():
    raise HTTPException(500, "Shared app storage is misconfigured.")
  return root


def _snapshot_release_id(instance_id: str, snapshot_path: str) -> str:
  root = _instance_root(instance_id, snapshot_path)
  data_root = Path(get_settings().data_dir).resolve()
  stored = Path(snapshot_path)
  target = stored if stored.is_absolute() else data_root / stored
  try:
    relative = target.absolute().relative_to(root.absolute())
  except ValueError as exc:
    raise HTTPException(500, "Shared app release metadata is invalid.") from exc
  if relative.parts == ("build",):
    return "legacy"
  if len(relative.parts) != 2 or relative.parts[0] != _RELEASES_DIR:
    raise HTTPException(500, "Shared app release metadata is invalid.")
  try:
    release_id = str(uuid.UUID(relative.parts[1]))
  except (ValueError, AttributeError) as exc:
    raise HTTPException(500, "Shared app release metadata is invalid.") from exc
  if release_id != relative.parts[1]:
    raise HTTPException(500, "Shared app release metadata is invalid.")
  return release_id


def _release_root(instance_id: str, snapshot_path: str) -> Path:
  root = _instance_root(instance_id, snapshot_path)
  data_root = Path(get_settings().data_dir).resolve()
  stored = Path(snapshot_path)
  target = stored if stored.is_absolute() else data_root / stored
  target = target.absolute()
  _snapshot_release_id(instance_id, snapshot_path)
  try:
    relative = target.relative_to(root.absolute())
  except ValueError as exc:
    raise HTTPException(500, "Shared app release storage is unavailable.") from exc
  current = root
  for part in relative.parts:
    current = current / part
    if current.is_symlink():
      raise HTTPException(500, "Shared app release storage is unavailable.")
  if not target.is_dir():
    raise HTTPException(500, "Shared app release storage is unavailable.")
  return target


def current_release(row: models.SharedAppInstance) -> SharedAppRelease:
  release_id = _snapshot_release_id(str(row.id), str(row.snapshot_path))
  root = _release_root(str(row.id), str(row.snapshot_path))
  try:
    entry = validate_path_within_base(str(row.entry_path), root)
  except HTTPException as exc:
    raise HTTPException(500, "Shared app release metadata is invalid.") from exc
  if entry.is_symlink() or not entry.is_file():
    raise HTTPException(500, "Shared app release entry is unavailable.")
  updated_at = row.updated_at or row.created_at or now_naive_utc()
  return SharedAppRelease(
    release_id=release_id,
    snapshot_path=str(row.snapshot_path),
    entry_path=str(row.entry_path),
    name=str(row.name),
    published_at=updated_at.isoformat() + "Z",
  )


def _remove_owned_path(path: Path) -> None:
  if path.is_symlink():
    path.unlink(missing_ok=True)
  elif path.exists():
    remove_snapshot_root(path)
  if path.parent.is_dir():
    sync_directory(path.parent)


def _ensure_durable_directory(path: Path) -> None:
  """Create one directory and durably link it from its existing parent."""
  if path.is_symlink():
    raise HTTPException(500, "Shared app release storage is misconfigured.")
  if path.exists():
    if not path.is_dir():
      raise HTTPException(500, "Shared app release storage is misconfigured.")
    return
  if not path.parent.is_dir() or path.parent.is_symlink():
    raise HTTPException(500, "Shared app release storage is misconfigured.")
  path.mkdir()
  sync_directory(path.parent)


def _install_immutable_release(instance_id: str, output_root: Path) -> SharedAppRelease:
  data_root = Path(get_settings().data_dir).resolve()
  shared = data_root / "shared"
  instances = shared / "app-instances"
  root = instances / instance_id
  release_id = str(uuid.uuid4())
  staging = root / f"{_STAGING_PREFIX}{release_id}"
  releases = root / _RELEASES_DIR
  target = releases / release_id
  for directory in (shared, instances, root, releases):
    _ensure_durable_directory(directory)
  if root.is_symlink() or releases.is_symlink() or staging.is_symlink() or target.exists():
    raise HTTPException(500, "Shared app release storage is misconfigured.")
  try:
    # Preserve source symlinks as symlinks so ``sync_file_tree`` rejects them;
    # never follow a path that appeared after the artifact's validation walk.
    shutil.copytree(output_root, staging, symlinks=True)
    total = 0
    for candidate in staging.rglob("*"):
      if candidate.is_symlink():
        raise HTTPException(422, "Shared builds cannot contain symbolic links.")
      if candidate.is_file():
        total += candidate.stat().st_size
        if total > SNAPSHOT_BYTES_MAX:
          raise HTTPException(413, "This build is too large to share as an app.")
    sync_file_tree(staging)
    staging.rename(target)
    sync_directory(releases)
  except BaseException:
    if staging.exists() or staging.is_symlink():
      _remove_owned_path(staging)
    raise
  snapshot_path = target.relative_to(data_root).as_posix()
  return SharedAppRelease(
    release_id=release_id,
    snapshot_path=snapshot_path,
    entry_path="",
    name="",
    published_at=now_naive_utc().isoformat() + "Z",
  )


def create_initial_release(
  *,
  instance_id: str,
  output_root: Path,
  entry_path: str,
  name: str,
) -> SharedAppRelease:
  with release_lock(instance_id):
    installed = _install_immutable_release(instance_id, output_root)
    release = SharedAppRelease(
      release_id=installed.release_id,
      snapshot_path=installed.snapshot_path,
      entry_path=entry_path,
      name=name,
      published_at=installed.published_at,
    )
    root = _release_root(instance_id, release.snapshot_path)
    try:
      entry = validate_path_within_base(entry_path, root)
    except HTTPException as exc:
      _remove_owned_path(root)
      raise HTTPException(500, "Shared app release metadata is invalid.") from exc
    if entry.is_symlink() or not entry.is_file():
      _remove_owned_path(root)
      raise HTTPException(500, "Shared app release entry is unavailable.")
    return release


def _cleanup_instance_releases_locked(row: models.SharedAppInstance) -> None:
  root = _instance_root(str(row.id), str(row.snapshot_path))
  keep = {str(row.snapshot_path)}
  if row.previous_snapshot_path:
    _snapshot_release_id(str(row.id), str(row.previous_snapshot_path))
    keep.add(str(row.previous_snapshot_path))
  data_root = Path(get_settings().data_dir).resolve()
  keep_paths = {
    (Path(path) if Path(path).is_absolute() else data_root / path).absolute()
    for path in keep
  }
  for child in root.iterdir() if root.is_dir() else ():
    if (
      child.name.startswith(_STAGING_PREFIX)
      or child.name.startswith(".build.next-")
      or child.name.startswith(".build.previous-")
    ):
      _remove_owned_path(child)
  legacy = root / "build"
  if legacy.absolute() not in keep_paths and (legacy.exists() or legacy.is_symlink()):
    _remove_owned_path(legacy)
  releases = root / _RELEASES_DIR
  if releases.is_dir() and not releases.is_symlink():
    for child in releases.iterdir():
      if child.absolute() not in keep_paths:
        _remove_owned_path(child)


def cleanup_instance_releases(row: models.SharedAppInstance) -> None:
  with release_lock(str(row.id)):
    _cleanup_instance_releases_locked(row)


def publish_release(
  db: Session,
  row: models.SharedAppInstance,
  *,
  output_root: Path,
  entry_path: str,
  name: str,
) -> SharedAppRelease:
  """Install, fsync, and transactionally point at one immutable release."""
  with release_lock(str(row.id)):
    old_snapshot = str(row.snapshot_path)
    installed = _install_immutable_release(str(row.id), output_root)
    row.previous_snapshot_path = old_snapshot
    row.snapshot_path = installed.snapshot_path
    row.entry_path = entry_path
    row.name = name
    row.updated_at = now_naive_utc()
    try:
      # ``current_release`` validates the new tree before its pointer commits.
      release = current_release(row)
      db.commit()
    except Exception:
      db.rollback()
      try:
        persisted_snapshot = db.query(models.SharedAppInstance.snapshot_path).filter(
          models.SharedAppInstance.id == row.id,
        ).scalar()
      except Exception:
        # The commit outcome is unknowable while the database is unavailable.
        # Keep the fsynced immutable tree; startup reconciliation removes it
        # only if no durable pointer references it.
        raise
      if persisted_snapshot != installed.snapshot_path:
        data_root = Path(get_settings().data_dir).resolve()
        target = Path(installed.snapshot_path)
        target = target if target.is_absolute() else data_root / target
        _remove_owned_path(target)
      raise
    _cleanup_instance_releases_locked(row)
    return release


def _snapshot_for_token(row: models.SharedAppInstance, release_id: str) -> str:
  current_id = _snapshot_release_id(str(row.id), str(row.snapshot_path))
  if release_id == current_id:
    return str(row.snapshot_path)
  if row.previous_snapshot_path:
    previous_id = _snapshot_release_id(str(row.id), str(row.previous_snapshot_path))
    if release_id == previous_id:
      return str(row.previous_snapshot_path)
  raise HTTPException(404, "Shared app release not found.")


def open_release_file(
  row: models.SharedAppInstance, release_id: str, path: str,
) -> tuple[BufferedReader, str]:
  """Open one authorized release body before publication ownership is released."""
  with release_lock(str(row.id)):
    snapshot_path = _snapshot_for_token(row, release_id)
    root = _release_root(str(row.id), snapshot_path)
    rel = (path or "").lstrip("/")
    target = validate_path_within_base(rel, root)
    if target.is_symlink() or not target.is_file():
      raise HTTPException(404, "Shared app file not found.")
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return open(fd, "rb", closefd=True), (
      mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    )


def reconcile_release_storage(db: Session) -> None:
  """Remove only release paths that no durable instance pointer can reach."""
  rows = db.query(models.SharedAppInstance).all()
  live_ids = {str(row.id) for row in rows}
  for row in rows:
    cleanup_instance_releases(row)
  instances = Path(get_settings().data_dir).resolve() / "shared" / "app-instances"
  if not instances.is_dir() or instances.is_symlink():
    return
  for child in instances.iterdir():
    try:
      instance_id = str(uuid.UUID(child.name))
    except (ValueError, AttributeError):
      continue
    if instance_id not in live_ids:
      _remove_owned_path(child)
