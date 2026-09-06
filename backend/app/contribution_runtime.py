"""Server-private durable paths for contribution publication recovery.

Contribution records and their reviewed diffs belong to an app's ordinary
storage.  Recovery capabilities do not: app and owner storage routes can read
and mutate every path below ``/data/apps/<id>``.  Keep attempt receipts and
retained request bytes in one separate runtime namespace whose paths are built
only from validated identifiers.
"""

import re
from pathlib import Path

from app.config import get_settings


_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ARTIFACT_FILENAMES = {
  "personal_attempt": "personal-submit.json",
  "relay_claim": "relay-claim.json",
  "relay_request": "relay-request.json",
}
_PRIVATE_DIR_MODE = 0o700


def _validated_app_id(app_id: int) -> str:
  if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
    raise ValueError("Contribution runtime app_id must be a positive integer.")
  return str(app_id)


def _validated_record_id(record_id: str) -> str:
  if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
    raise ValueError("Contribution runtime record_id is invalid.")
  return record_id


def _validated_artifact(artifact: str) -> str:
  try:
    return _ARTIFACT_FILENAMES[artifact]
  except (KeyError, TypeError) as exc:
    raise ValueError("Contribution runtime artifact is invalid.") from exc


def _assert_private_directory(path: Path) -> None:
  if path.is_symlink():
    raise OSError(f"Contribution runtime directory is a symlink: {path}")
  if path.exists() and not path.is_dir():
    raise OSError(f"Contribution runtime path is not a directory: {path}")


def _prepare_private_directory(path: Path) -> None:
  _assert_private_directory(path)
  path.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
  _assert_private_directory(path)
  path.chmod(_PRIVATE_DIR_MODE)


def contribution_artifact_path(
  app_id: int,
  record_id: str,
  artifact: str,
  *,
  create_parent: bool = False,
) -> Path:
  """Return one validated private artifact path.

  Reads use the default and never create directories.  A caller about to make
  an atomic write passes ``create_parent=True``; only the fixed runtime root and
  the exact app/record directories are then created, each mode 0700.  Existing
  symlinks at any runtime component or at the artifact itself are rejected.
  """
  safe_app_id = _validated_app_id(app_id)
  safe_record_id = _validated_record_id(record_id)
  filename = _validated_artifact(artifact)

  data_root = Path(get_settings().data_dir).resolve()
  runtime_root = data_root / ".contribution-runtime"
  app_root = runtime_root / safe_app_id
  record_root = app_root / safe_record_id
  directories = (runtime_root, app_root, record_root)

  if create_parent:
    for directory in directories:
      _prepare_private_directory(directory)
  else:
    for directory in directories:
      _assert_private_directory(directory)

  path = record_root / filename
  if path.is_symlink():
    raise OSError(f"Contribution runtime artifact is a symlink: {path}")
  if path.exists() and not path.is_file():
    raise OSError(f"Contribution runtime artifact is not a file: {path}")
  return path


def personal_attempt_path(
  app_id: int, record_id: str, *, create_parent: bool = False,
) -> Path:
  return contribution_artifact_path(
    app_id, record_id, "personal_attempt", create_parent=create_parent,
  )


def relay_claim_path(
  app_id: int, record_id: str, *, create_parent: bool = False,
) -> Path:
  return contribution_artifact_path(
    app_id, record_id, "relay_claim", create_parent=create_parent,
  )


def relay_request_path(
  app_id: int, record_id: str, *, create_parent: bool = False,
) -> Path:
  return contribution_artifact_path(
    app_id, record_id, "relay_request", create_parent=create_parent,
  )


def cleanup_empty_runtime_dirs(app_id: int, record_id: str) -> None:
  """Best-effort removal of empty record/app/runtime directories."""
  try:
    path = contribution_artifact_path(app_id, record_id, "relay_request")
  except (OSError, ValueError):
    return
  for directory in (path.parent, path.parent.parent, path.parent.parent.parent):
    try:
      _assert_private_directory(directory)
      directory.rmdir()
    except FileNotFoundError:
      continue
    except OSError:
      return
