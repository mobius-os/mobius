"""Durable ownership records for published site snapshots.

``published-meta`` is outside every app tree so uninstall cleanup cannot erase
the authority that reserves a public token.  Token files inside app storage are
only migration hints; every registered serve resolves through this module.
"""

import ctypes
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.storage_io import atomic_write
from app.timeutil import now_naive_utc

_TOKEN_RE = re.compile(r"^[a-f0-9]{16,64}$")
_PUBLISH_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_APP_GEN_RE = re.compile(r"^[a-f0-9]{32}$")

_REGISTRY_KEYS = {
  "app_id", "app_gen", "project_id", "state", "created_at",
}
_REGISTRY_STATES = {"staged", "active", "revoked"}
_REGISTRY_READ_MAX = 4096


class InvalidPublicationRegistry(ValueError):
  """A token has a registry reservation, but its record is not trustworthy."""


class PublicationReservationConflict(ValueError):
  """A token is already permanently reserved by another registry record."""


@dataclass(frozen=True)
class PublicationRecord:
  token: str
  app_id: int
  app_gen: str
  project_id: str | None
  state: str
  created_at: str

  def binding(self) -> tuple[int, str, str | None]:
    return self.app_id, self.app_gen, self.project_id

  def as_json(self) -> dict:
    return {
      "app_id": self.app_id,
      "app_gen": self.app_gen,
      "project_id": self.project_id,
      "state": self.state,
      "created_at": self.created_at,
    }


def new_publication_record(
  token: str,
  app_id: int,
  app_gen: str,
  project_id: str | None,
  state: str = "staged",
) -> PublicationRecord:
  return PublicationRecord(
    token=token,
    app_id=app_id,
    app_gen=app_gen,
    project_id=project_id,
    state=state,
    created_at=now_naive_utc().isoformat(timespec="microseconds") + "Z",
  )


def published_root(settings) -> Path:
  return Path(settings.data_dir) / "published"


def registry_root(settings) -> Path:
  return Path(settings.data_dir) / "published-meta"


def registry_path(settings, token: str) -> Path:
  if not _TOKEN_RE.fullmatch(token or ""):
    raise InvalidPublicationRegistry("invalid publication token")
  return registry_root(settings) / f"{token}.json"


def _decode_record(token: str, raw: bytes) -> PublicationRecord:
  def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")

  try:
    value = json.loads(
      raw.decode("utf-8"),
      parse_constant=_reject_constant,
    )
  except (UnicodeDecodeError, ValueError) as exc:
    raise InvalidPublicationRegistry("invalid registry JSON") from exc
  except RecursionError as exc:
    # RecursionError is a RuntimeError, so deeply nested input would escape the
    # invalid-registry handling and surface as an unhandled 500 instead of
    # failing closed like every other unreadable record.
    raise InvalidPublicationRegistry("registry JSON is nested too deeply") from exc
  if not isinstance(value, dict) or set(value) != _REGISTRY_KEYS:
    raise InvalidPublicationRegistry("invalid registry schema")
  app_id = value["app_id"]
  app_gen = value["app_gen"]
  project_id = value["project_id"]
  state_value = value["state"]
  created_at = value["created_at"]
  if type(app_id) is not int or app_id <= 0:
    raise InvalidPublicationRegistry("invalid registry app_id")
  if not isinstance(app_gen, str) or not _APP_GEN_RE.fullmatch(app_gen):
    raise InvalidPublicationRegistry("invalid registry app_gen")
  if project_id is not None and (
    not isinstance(project_id, str)
    or not _PUBLISH_PROJECT_RE.fullmatch(project_id)
  ):
    raise InvalidPublicationRegistry("invalid registry project_id")
  if type(state_value) is not str or state_value not in _REGISTRY_STATES:
    raise InvalidPublicationRegistry("invalid registry state")
  if type(created_at) is not str or len(created_at) > 64:
    raise InvalidPublicationRegistry("invalid registry created_at")
  try:
    datetime.fromisoformat(created_at.replace("Z", "+00:00"))
  except ValueError as exc:
    raise InvalidPublicationRegistry("invalid registry created_at") from exc
  return PublicationRecord(
    token=token,
    app_id=app_id,
    app_gen=app_gen,
    project_id=project_id,
    state=state_value,
    created_at=created_at,
  )


def read_publication_record(settings, token: str) -> PublicationRecord | None:
  """Read one strict, capped, non-symlink registry record.

  ``None`` means there is no reservation and is the only state eligible for
  legacy static serving.  An invalid or symlink record remains a reservation
  and fails closed instead of being treated as reclaimable.
  """
  path = registry_path(settings, token)
  root = path.parent
  if root.is_symlink():
    raise InvalidPublicationRegistry("registry root is a symlink")
  flags = os.O_RDONLY
  if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
  try:
    fd = os.open(path, flags)
  except FileNotFoundError:
    return None
  except OSError as exc:
    raise InvalidPublicationRegistry("registry record is unreadable") from exc
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _REGISTRY_READ_MAX:
      raise InvalidPublicationRegistry("registry record has invalid size/type")
    raw = os.read(fd, _REGISTRY_READ_MAX + 1)
    if len(raw) != info.st_size or len(raw) > _REGISTRY_READ_MAX:
      raise InvalidPublicationRegistry("registry record changed while reading")
  finally:
    os.close(fd)
  return _decode_record(token, raw)


def _record_bytes(record: PublicationRecord) -> bytes:
  if record.state not in _REGISTRY_STATES:
    raise InvalidPublicationRegistry("invalid registry state")
  # Round-trip through the strict decoder before a security record becomes
  # durable; callers cannot accidentally persist a coercive/partial schema.
  raw = json.dumps(
    record.as_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")
  _decode_record(record.token, raw)
  return raw


def create_publication_record(settings, record: PublicationRecord) -> None:
  """Atomically reserve a never-before-seen token without overwriting it."""
  root = registry_root(settings)
  if root.is_symlink():
    raise InvalidPublicationRegistry("registry root is a symlink")
  root.mkdir(parents=True, exist_ok=True)
  staging = root / ".staging"
  if staging.is_symlink():
    raise InvalidPublicationRegistry("registry staging root is a symlink")
  staging.mkdir(parents=True, exist_ok=True)
  temp = staging / f"{record.token}-{os.getpid()}-{id(record):x}.json"
  atomic_write(temp, _record_bytes(record))
  try:
    # link() is an atomic create-if-absent.  Unlike os.replace(), it can never
    # overwrite another app's active or revoked ownership reservation.
    os.link(temp, registry_path(settings, record.token), follow_symlinks=False)
  except FileExistsError as exc:
    raise PublicationReservationConflict(record.token) from exc
  finally:
    try:
      temp.unlink()
    except OSError:
      pass


def replace_publication_record(
  settings,
  current: PublicationRecord,
  state: str,
) -> PublicationRecord:
  """Change state only when the durable binding still exactly matches."""
  live = read_publication_record(settings, current.token)
  if live is None or live.binding() != current.binding():
    raise PublicationReservationConflict(current.token)
  if live.state == "revoked" and state != "revoked":
    raise PublicationReservationConflict(current.token)
  updated = replace(live, state=state)
  atomic_write(registry_path(settings, current.token), _record_bytes(updated))
  return updated


def resolve_active_publication(
  db: Session,
  settings,
  token: str,
) -> PublicationRecord | None:
  """Resolve a public capability only when its live app generation matches."""
  try:
    record = read_publication_record(settings, token)
  except InvalidPublicationRegistry:
    return None
  if record is None or record.state != "active":
    return None
  app = (
    db.query(models.App)
    .populate_existing()
    .filter(
      models.App.id == record.app_id,
      models.App.deleted_at.is_(None),
    )
    .first()
  )
  if app is None or app.token_nonce != record.app_gen:
    return None
  return record


def atomic_promote_directory(stage: Path, destination: Path) -> None:
  """Promote a complete directory without exposing a partial generation."""
  destination.parent.mkdir(parents=True, exist_ok=True)
  if destination.is_symlink():
    raise OSError("published destination is a symlink")
  if not destination.exists():
    os.replace(stage, destination)
    return
  if not destination.is_dir():
    raise OSError("published destination is not a directory")

  # Linux renameat2(RENAME_EXCHANGE) swaps two non-empty directories in one
  # filesystem transaction.  The deployed platform is Linux.  A platform
  # without that primitive must fail while preserving the old generation;
  # a two-rename fallback would reopen the crash window this invariant closes.
  libc = ctypes.CDLL(None, use_errno=True)
  renameat2 = getattr(libc, "renameat2", None)
  if renameat2 is not None:
    renameat2.argtypes = [
      ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
      ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
      -100, os.fsencode(stage), -100, os.fsencode(destination), 2,
    )
    if result == 0:
      return
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
  raise OSError("atomic directory exchange is unavailable")
