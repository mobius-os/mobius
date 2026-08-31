"""Path-based JSON storage and reconnect cursors for shared app instances."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
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
  sync_directory,
  sync_file_tree,
)
from app.timeutil import now_naive_utc


STATE_BYTES_MAX = 512 * 1024
CHANGE_LIMIT = 1000
_SAFE_PATH = re.compile(r"^[\w._/@+ -]+$")
_JOURNAL_NAME = "state-journal.json"
# Each image is already JSON text. Encoding it inside the journal can escape
# every quote/backslash once more, so reserve twice each image's byte bound.
_JOURNAL_BYTES_MAX = (STATE_BYTES_MAX * 4) + (16 * 1024)
_JOURNAL_FIELDS = {
  "operation_id", "instance_id", "actor_key", "display_name", "kind", "path",
  "before_present", "before_encoded", "before_mtime_ns",
  "after_present", "after_encoded",
}
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


def _journal_path(row: models.SharedAppInstance) -> Path:
  data = state_root(row)
  journal = data.parent / _JOURNAL_NAME
  if journal.is_symlink():
    raise HTTPException(500, "Shared app state journal is misconfigured.")
  return journal


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
    _reconcile_journal(db, row)
    state = read_state(row)
    state["cursor"] = _latest_change_cursor(db, row)
    return state


def _append_change(
  db: Session,
  row: models.SharedAppInstance,
  *,
  operation_id: str,
  actor_key: str,
  display_name: str,
  kind: str,
  path: str,
  version: str | None,
) -> models.SharedAppChange:
  change = models.SharedAppChange(
    operation_id=operation_id,
    instance_id=row.id,
    kind=kind,
    path=path,
    version=version,
    actor_key=actor_key,
    display_name=display_name,
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


def _load_journal(row: models.SharedAppInstance) -> dict | None:
  journal_path = _journal_path(row)
  try:
    if journal_path.stat().st_size > _JOURNAL_BYTES_MAX:
      raise HTTPException(500, "Shared app state recovery journal is invalid.")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return None
  except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise HTTPException(500, "Shared app state recovery journal is invalid.") from exc
  if not isinstance(journal, dict):
    raise HTTPException(500, "Shared app state recovery journal is invalid.")
  try:
    operation_id = str(uuid.UUID(str(journal.get("operation_id"))))
  except (ValueError, AttributeError, TypeError) as exc:
    raise HTTPException(500, "Shared app state recovery journal is invalid.") from exc
  if operation_id != journal.get("operation_id"):
    raise HTTPException(500, "Shared app state recovery journal is invalid.")
  if (
    set(journal) != _JOURNAL_FIELDS
    or journal.get("instance_id") != str(row.id)
    or journal.get("kind") not in {"set", "delete"}
    or not isinstance(journal.get("path"), str)
    or not isinstance(journal.get("actor_key"), str)
    or not isinstance(journal.get("display_name"), str)
    or type(journal.get("before_present")) is not bool
    or type(journal.get("after_present")) is not bool
    or journal.get("before_present") != isinstance(journal.get("before_encoded"), str)
    or journal.get("after_present") != isinstance(journal.get("after_encoded"), str)
    or (not journal.get("before_present") and journal.get("before_encoded") is not None)
    or (not journal.get("after_present") and journal.get("after_encoded") is not None)
    or journal.get("before_present") != (type(journal.get("before_mtime_ns")) is int)
    or journal.get("after_present") != (journal.get("kind") == "set")
  ):
    raise HTTPException(500, "Shared app state recovery journal is invalid.")
  try:
    validate_state_path(journal["path"])
    for key in ("before_encoded", "after_encoded"):
      encoded = journal.get(key)
      if isinstance(encoded, str):
        if len(encoded.encode("utf-8")) > STATE_BYTES_MAX:
          raise ValueError("journal image exceeds the state bound")
        json.loads(encoded)
  except (HTTPException, UnicodeError, ValueError) as exc:
    raise HTTPException(500, "Shared app state recovery journal is invalid.") from exc
  return journal


def _write_journal(row: models.SharedAppInstance, journal: dict) -> None:
  encoded = json.dumps(journal, ensure_ascii=False, separators=(",", ":"))
  if len(encoded.encode("utf-8")) > _JOURNAL_BYTES_MAX:
    raise HTTPException(500, "Shared app state recovery journal is too large.")
  atomic_write(_journal_path(row), encoded)
  sync_directory(_journal_path(row).parent)


def _remove_journal(row: models.SharedAppInstance) -> None:
  journal_path = _journal_path(row)
  journal_path.unlink(missing_ok=True)
  sync_directory(journal_path.parent)


def _apply_journal_image(
  row: models.SharedAppInstance, journal: dict, image: str,
) -> str | None:
  target = _target(row, journal["path"])
  if target.exists() and not target.is_file():
    raise HTTPException(400, "Shared app data paths must identify files.")
  present = journal[f"{image}_present"]
  if not present:
    target.unlink(missing_ok=True)
    root = state_root(row)
    if root.is_dir():
      sync_file_tree(root)
    return None
  encoded = journal[f"{image}_encoded"].encode("utf-8")
  if not target.is_file() or target.read_bytes() != encoded:
    atomic_write(target, encoded)
  if image == "before" and journal.get("before_mtime_ns") is not None:
    mtime_ns = journal["before_mtime_ns"]
    os.utime(target, ns=(mtime_ns, mtime_ns))
  sync_file_tree(state_root(row))
  return file_version_token(target)


def _reconcile_journal(
  db: Session, row: models.SharedAppInstance,
) -> models.SharedAppChange | None:
  """Commit only a recorded operation; otherwise restore its exact before image."""
  journal = _load_journal(row)
  if journal is None:
    return None
  change = db.query(models.SharedAppChange).filter(
    models.SharedAppChange.operation_id == journal["operation_id"],
    models.SharedAppChange.instance_id == row.id,
  ).first()
  if change is None:
    _apply_journal_image(row, journal, "before")
  else:
    if (
      change.kind != journal["kind"]
      or change.path != journal["path"]
      or change.actor_key != journal["actor_key"]
      or change.display_name != journal["display_name"]
    ):
      raise HTTPException(500, "Shared app state recovery journal is invalid.")
    version = _apply_journal_image(row, journal, "after")
    if change.version != version:
      change.version = version
      try:
        db.commit()
      except Exception:
        db.rollback()
        raise
  _remove_journal(row)
  return change


def reconcile_all_journals(db: Session) -> None:
  """Resolve every interrupted state operation before the server accepts work."""
  rows = db.query(models.SharedAppInstance).all()
  for row in rows:
    with state_lock(str(row.id)):
      _reconcile_journal(db, row)


def _latest_change_cursor(db: Session, row: models.SharedAppInstance) -> int:
  return db.query(func.max(models.SharedAppChange.id)).filter(
    models.SharedAppChange.instance_id == row.id,
  ).scalar() or 0


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
    _reconcile_journal(db, row)
    target = _target(row, path)
    if target.exists() and not target.is_file():
      raise HTTPException(400, "Shared app data paths must identify files.")
    current_version = file_version_token(target) if target.is_file() else None
    if current_version != expected_version:
      raise HTTPException(409, {"version": current_version})
    encoded = None if delete else json.dumps(
      value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    root = state_root(row)
    previous = target.read_bytes() if target.is_file() else None
    previous_stat = target.stat() if target.is_file() else None
    previous_size = len(previous) if previous is not None else 0
    projected = app_dir_usage(root) - previous_size
    projected += len(encoded) if encoded is not None else 0
    if projected > STATE_BYTES_MAX:
      raise HTTPException(413, "Shared app data is full.")
    journal = {
      "operation_id": str(uuid.uuid4()),
      "instance_id": str(row.id),
      "actor_key": "owner" if principal.is_owner else f"member:{principal.member_id}",
      "display_name": principal.display_name or principal.owner.username or "Collaborator",
      "kind": "delete" if delete else "set",
      "path": path,
      "before_present": previous is not None,
      "before_encoded": None if previous is None else previous.decode("utf-8"),
      "before_mtime_ns": previous_stat.st_mtime_ns if previous_stat is not None else None,
      "after_present": encoded is not None,
      "after_encoded": None if encoded is None else encoded.decode("utf-8"),
    }
    _write_journal(row, journal)
    try:
      version = _apply_journal_image(row, journal, "after")
      change = _append_change(
        db,
        row,
        operation_id=journal["operation_id"],
        actor_key=journal["actor_key"],
        display_name=journal["display_name"],
        kind=journal["kind"],
        path=journal["path"],
        version=version,
      )
      row.updated_at = now_naive_utc()
      db.commit()
    except Exception:
      db.rollback()
      # A database driver can report an ambiguous commit outcome. Re-read the
      # operation identity after rollback: committed means keep ``after``;
      # absent means restore the exact before image.
      _reconcile_journal(db, row)
      raise
    _remove_journal(row)
    return {
      "version": change.version,
      "change_id": change.id,
      "value": None if delete else value,
    }


def list_changes(db: Session, row: models.SharedAppInstance, after: int | None) -> dict:
  with state_lock(str(row.id)):
    _reconcile_journal(db, row)
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
