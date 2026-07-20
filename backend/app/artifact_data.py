"""Strict, capped filesystem primitives for shared artifact JSON values."""

import json
import os
import re
import stat
from pathlib import Path

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ARTIFACT_KEY_RE = re.compile(r"^[a-z0-9._-]{1,64}$")

MAX_ARTIFACT_VALUE_BYTES = 64 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 1024 * 1024
MAX_ARTIFACT_KEYS = 100
MAX_ARTIFACT_READ_BYTES = 1024 * 1024
# Enumeration stops after this many directory entries. Comfortably above the
# 100-key cap so a legitimate artifact always lists in full, while bounding the
# work an unauthenticated published-site listing can cost.
MAX_ARTIFACT_SCAN_ENTRIES = 1000


class ArtifactDataError(ValueError):
  """Artifact storage is invalid, unsafe, missing, or over a read cap."""


def validate_artifact_id(artifact_id: str) -> bool:
  return bool(_ARTIFACT_ID_RE.fullmatch(artifact_id or ""))


def validate_artifact_key(key: str) -> bool:
  return bool(_ARTIFACT_KEY_RE.fullmatch(key or ""))


def canonical_json(value) -> bytes:
  try:
    return json.dumps(
      value,
      ensure_ascii=False,
      sort_keys=True,
      separators=(",", ":"),
      allow_nan=False,
    ).encode("utf-8")
  except (TypeError, ValueError) as exc:
    raise ArtifactDataError("Value must be canonical JSON.") from exc


def parse_json(raw: bytes):
  def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")

  try:
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
  except (UnicodeDecodeError, ValueError) as exc:
    raise ArtifactDataError("Value must be valid JSON.") from exc
  except RecursionError as exc:
    # Deeply nested input exhausts the decoder's stack. RecursionError is a
    # RuntimeError, so without this it escapes as an unhandled 500 instead of
    # the 400 every other malformed body gets.
    raise ArtifactDataError("Value is nested too deeply.") from exc


def artifact_dir_path(settings, app_id: int, artifact_id: str) -> Path:
  """Return the artifact's directory after literal confinement.

  App-controlled storage may never redirect any literal component through a
  symlink.  The strict one-segment identifiers also make browser/path
  normalization unable to change the target artifact before validation.
  """
  if type(app_id) is not int or app_id <= 0:
    raise ArtifactDataError("Invalid app id.")
  if not validate_artifact_id(artifact_id):
    raise ArtifactDataError("Invalid artifact id.")
  data_root = Path(settings.data_dir)
  apps_root = data_root / "apps"
  app_root = apps_root / str(app_id)
  artifact_data_root = app_root / "artifact-data"
  artifact_root = artifact_data_root / artifact_id
  for component in (apps_root, app_root, artifact_data_root, artifact_root):
    if component.is_symlink():
      raise ArtifactDataError("Symlinks are not allowed in artifact storage.")
  return artifact_root


def artifact_file_path(
  settings,
  app_id: int,
  artifact_id: str,
  key: str,
) -> tuple[Path, Path]:
  """Return the artifact directory and key file after literal confinement."""
  if not validate_artifact_key(key):
    raise ArtifactDataError("Invalid artifact id or key.")
  artifact_root = artifact_dir_path(settings, app_id, artifact_id)
  file_path = artifact_root / f"{key}.json"
  if file_path.is_symlink():
    raise ArtifactDataError("Symlinks are not allowed in artifact storage.")
  expected = artifact_root.resolve()
  resolved = file_path.resolve()
  if expected != resolved.parent:
    raise ArtifactDataError("Artifact storage path escaped its scope.")
  return artifact_root, file_path


def read_json_file(file_path: Path, cap: int = MAX_ARTIFACT_READ_BYTES):
  flags = os.O_RDONLY
  if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
  try:
    fd = os.open(file_path, flags)
  except OSError as exc:
    raise ArtifactDataError("Artifact value not found.") from exc
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
      raise ArtifactDataError("Artifact value not found.")
    raw = os.read(fd, cap + 1)
    if len(raw) != info.st_size or len(raw) > cap:
      raise ArtifactDataError("Artifact value not found.")
  finally:
    os.close(fd)
  return parse_json(raw)


def list_artifact_keys(artifact_root: Path) -> list[str]:
  """Return the artifact's stored keys, derived from the directory itself.

  Enumeration belongs here rather than in a client-maintained index file: two
  browser tabs writing different keys would each read the old index, write their
  own value, and the second index write would drop the first key — leaving a
  value that exists but cannot be discovered. The directory is the only thing
  that cannot disagree with itself.

  Applies the same no-symlink rule as every other read of this tree, and hides
  anything that is not a validly-named ``.json`` value (including any legacy
  index file an older app version left behind, which callers filter by name).
  """
  if not artifact_root.exists():
    return []
  if artifact_root.is_symlink() or not artifact_root.is_dir():
    raise ArtifactDataError("Invalid artifact storage directory.")
  keys = []
  examined = 0
  try:
    # Iterate lazily and stop early: this is reachable UNAUTHENTICATED through a
    # published site, and the per-artifact key cap is only enforced by the
    # artifact-data route — the generic app-storage API can drop arbitrarily
    # many .json files into this same directory. Materializing the whole scandir
    # (and the response) would hand any published page an unbounded scan.
    with os.scandir(artifact_root) as entries:
      for entry in entries:
        examined += 1
        if examined > MAX_ARTIFACT_SCAN_ENTRIES:
          break
        if len(keys) >= MAX_ARTIFACT_KEYS:
          break
        try:
          if entry.is_symlink():
            continue
          info = entry.stat(follow_symlinks=False)
        except OSError as exc:
          # Do NOT skip silently: returning 200 with a quietly incomplete list
          # is the same "stored but unlistable" failure this enumeration exists
          # to remove. Surface it and let the caller fail closed.
          raise ArtifactDataError("Artifact storage changed while scanning.") from exc
        if not stat.S_ISREG(info.st_mode):
          continue
        # A value larger than the read cap cannot be served by the per-key read,
        # so listing it would advertise a key that always 404s.
        if info.st_size > MAX_ARTIFACT_READ_BYTES:
          continue
        if not entry.name.endswith(".json"):
          continue
        name = entry.name[:-5]
        if validate_artifact_key(name):
          keys.append(name)
  except OSError as exc:
    raise ArtifactDataError("Artifact storage is unreadable.") from exc
  return sorted(keys)


def artifact_usage(artifact_root: Path) -> tuple[int, int]:
  """Return regular-file bytes and JSON-key count without following links."""
  if not artifact_root.exists():
    return 0, 0
  if artifact_root.is_symlink() or not artifact_root.is_dir():
    raise ArtifactDataError("Invalid artifact storage directory.")
  total = 0
  keys = 0
  try:
    entries = list(os.scandir(artifact_root))
  except OSError as exc:
    raise ArtifactDataError("Artifact storage is unreadable.") from exc
  for entry in entries:
    try:
      if entry.is_symlink():
        raise ArtifactDataError("Symlinks are not allowed in artifact storage.")
      info = entry.stat(follow_symlinks=False)
    except OSError as exc:
      raise ArtifactDataError("Artifact storage changed while scanning.") from exc
    if not stat.S_ISREG(info.st_mode):
      raise ArtifactDataError("Artifact storage contains a non-file entry.")
    total += info.st_size
    if entry.name.endswith(".json") and validate_artifact_key(entry.name[:-5]):
      keys += 1
  return total, keys
