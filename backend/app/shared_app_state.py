"""Path-based JSON storage and reconnect cursors for shared app instances."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from weakref import WeakValueDictionary

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.deps import SharedAppPrincipal
from app.shared_app_retention import owned_snapshot_root
from app.storage_io import (
  app_dir_usage,
  atomic_write,
  file_version_token,
  is_atomic_write_temp_name,
)
from app.timeutil import now_naive_utc


STATE_BYTES_MAX = 512 * 1024
CHANGE_LIMIT = 1000
_SAFE_PATH = re.compile(r"^[\w._/@+ -]+$")
_locks: "WeakValueDictionary[str, threading.RLock]" = WeakValueDictionary()
_locks_guard = threading.Lock()


def state_lock(instance_id: str) -> threading.RLock:
  with _locks_guard:
    lock = _locks.get(instance_id)
    if lock is None:
      lock = threading.RLock()
      _locks[instance_id] = lock
    return lock


def validate_state_path(path: str) -> str:
  parts = Path(path).parts
  if (
    not path or len(path) > 200 or path.startswith("/") or "\\" in path
    or ".." in parts or not _SAFE_PATH.fullmatch(path)
    or any(is_atomic_write_temp_name(part) for part in parts)
  ):
    raise HTTPException(400, "Invalid shared app data path.")
  return path


def state_root(row: models.SharedAppInstance) -> Path:
  root = owned_snapshot_root(str(row.id), row.snapshot_path)
  if root is None:
    raise HTTPException(500, "Shared app storage is misconfigured.")
  data = root / "data"
  if root.is_symlink() or data.is_symlink():
    raise HTTPException(500, "Shared app storage is misconfigured.")
  return data


def _target(row: models.SharedAppInstance, path: str) -> Path:
  base = state_root(row)
  target = base.joinpath(*Path(validate_state_path(path)).parts)
  current = base
  for part in target.relative_to(base).parts:
    current = current / part
    if current.is_symlink():
      raise HTTPException(400, "Shared app data cannot use symbolic links.")
  return target


def read_state(row: models.SharedAppInstance) -> dict:
  with state_lock(str(row.id)):
    root = state_root(row)
    values = {}
    versions = {}
    if root.is_dir() and not root.is_symlink():
      for target in root.rglob("*"):
        if not target.is_file() or target.is_symlink():
          continue
        # A process crash can strand ``atomic_write``'s same-directory temp
        # file. Its exact namespace is reserved by ``validate_state_path``;
        # never expose partial internal bytes as app state after restart.
        if is_atomic_write_temp_name(target.name):
          continue
        path = target.relative_to(root).as_posix()
        validate_state_path(path)
        try:
          values[path] = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
          raise HTTPException(500, "Shared app data could not be read.") from exc
        versions[path] = file_version_token(target)
    return {"values": values, "versions": versions}


def read_state_snapshot(db: Session, row: models.SharedAppInstance) -> dict:
  """Read files and their durable change cursor as one ordered snapshot."""
  with state_lock(str(row.id)):
    state = read_state(row)
    state["cursor"] = list_changes(db, row, None)["cursor"]
    return state


def _append_change(
  db: Session,
  row: models.SharedAppInstance,
  principal: SharedAppPrincipal,
  *,
  kind: str,
  path: str,
  version: str | None,
) -> models.SharedAppChange:
  change = models.SharedAppChange(
    instance_id=row.id,
    kind=kind,
    path=path,
    version=version,
    actor_key="owner" if principal.is_owner else f"member:{principal.member_id}",
    display_name=principal.display_name or principal.owner.username or "Collaborator",
  )
  db.add(change)
  db.flush()
  cutoff = db.query(models.SharedAppChange.id).filter(
    models.SharedAppChange.instance_id == row.id,
  ).order_by(models.SharedAppChange.id.desc()).offset(CHANGE_LIMIT - 1).limit(1).scalar()
  if cutoff is not None:
    db.query(models.SharedAppChange).filter(
      models.SharedAppChange.instance_id == row.id,
      models.SharedAppChange.id < cutoff,
    ).delete(synchronize_session=False)
  return change


def write_state(
  db: Session,
  row: models.SharedAppInstance,
  principal: SharedAppPrincipal,
  *,
  path: str,
  value,
  delete: bool,
  expected_version: str | None,
) -> dict:
  with state_lock(str(row.id)):
    target = _target(row, path)
    if target.exists() and not target.is_file():
      raise HTTPException(400, "Shared app data paths must identify files.")
    current_version = file_version_token(target) if target.is_file() else None
    if current_version != expected_version:
      raise HTTPException(409, {"version": current_version})
    previous = target.read_bytes() if target.is_file() else None
    encoded = None if delete else json.dumps(
      value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    root = state_root(row)
    projected = app_dir_usage(root) - (len(previous) if previous is not None else 0)
    projected += len(encoded) if encoded is not None else 0
    if projected > STATE_BYTES_MAX:
      raise HTTPException(413, "Shared app data is full.")
    try:
      if delete:
        target.unlink(missing_ok=True)
        version = None
      else:
        atomic_write(target, encoded)
        version = file_version_token(target)
      change = _append_change(
        db, row, principal,
        kind="delete" if delete else "set", path=path, version=version,
      )
      row.updated_at = now_naive_utc()
      db.commit()
    except Exception:
      db.rollback()
      if previous is None:
        target.unlink(missing_ok=True)
      else:
        atomic_write(target, previous)
      raise
    return {"version": version, "change_id": change.id, "value": None if delete else value}


def list_changes(db: Session, row: models.SharedAppInstance, after: int | None) -> dict:
  oldest, latest, retained = db.query(
    func.min(models.SharedAppChange.id),
    func.max(models.SharedAppChange.id),
    func.count(models.SharedAppChange.id),
  ).filter(models.SharedAppChange.instance_id == row.id).one()
  latest = latest or 0
  if after is None:
    return {"cursor": latest, "changes": [], "truncated": False}
  rows = db.query(models.SharedAppChange).filter(
    models.SharedAppChange.instance_id == row.id,
    models.SharedAppChange.id > after,
  ).order_by(models.SharedAppChange.id.asc()).limit(101).all()
  truncated = len(rows) > 100 or (
    retained >= CHANGE_LIMIT and oldest is not None and after < oldest
  )
  rows = rows[:100]
  cursor = rows[-1].id if rows else max(after, latest)
  return {
    "cursor": cursor,
    "truncated": truncated,
    "changes": [{
      "id": change.id,
      "kind": change.kind,
      "path": change.path,
      "version": change.version,
      "actor_key": change.actor_key,
      "display_name": change.display_name,
      "created_at": change.created_at.isoformat() if change.created_at else None,
    } for change in rows],
  }
