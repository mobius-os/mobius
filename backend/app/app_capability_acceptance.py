"""Review and accept locally declared runtime capabilities for Store apps.

Store installs retain their reviewed package contract when local source is
applied.  This module owns the narrower exception: after an owner reviews the
normalized runtime declaration in that source, it can replace only the
contract's ``runtime`` field.  The acceptance digest covers the entire
resulting contract, so a Store update that changes the reviewed baseline
between review and acceptance rejects instead of silently re-granting.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from sqlalchemy.orm import Session

from app import models, source_dirs
from app.app_capabilities import (
  capability_digest,
  contract_with_runtime_capabilities,
  diff_contracts,
)
from app.broadcast import get_system_broadcast
from app.config import get_settings
from app.manifest_contract import MANIFEST_MAX_BYTES


class CapabilityAcceptanceError(ValueError):
  """A review or acceptance rejection safe to return to an owner caller."""

  def __init__(self, message: str, *, status_code: int = 422):
    super().__init__(message)
    self.status_code = status_code


def _manifest_for(app: models.App) -> dict:
  """Read this App's manifest without following source or manifest symlinks."""
  if not app.source_dir:
    raise CapabilityAcceptanceError("App has no local source directory.")
  root = source_dirs.apps_root(get_settings().data_dir)
  # Resolving the stored path would turn a symlink to a sibling app into an
  # apparently valid direct child and lose the Store app's source identity,
  # so this stays lexical where source_dirs.source_dir_kind resolves.
  source_dir = Path(os.path.abspath(app.source_dir))
  if source_dir.parent != root or source_dir.name.isdigit():
    raise CapabilityAcceptanceError(
      "App source directory is outside the reviewed apps root."
    )

  root_descriptor = None
  source_descriptor = None
  try:
    directory_flags = (
      os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
      | getattr(os, "O_CLOEXEC", 0)
    )
    root_descriptor = os.open(root, directory_flags)
    try:
      source_descriptor = os.open(
        source_dir.name,
        directory_flags,
        dir_fd=root_descriptor,
      )
    except OSError as exc:
      raise CapabilityAcceptanceError(
        "App source directory must be a direct, regular directory under the apps root."
      ) from exc
    try:
      descriptor = os.open(
        "mobius.json",
        os.O_RDONLY | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
        dir_fd=source_descriptor,
      )
    except OSError as exc:
      raise CapabilityAcceptanceError(
        "mobius.json must be a regular file inside the app source."
      ) from exc
    with os.fdopen(descriptor, "rb") as manifest_file:
      if not stat.S_ISREG(os.fstat(manifest_file.fileno()).st_mode):
        raise CapabilityAcceptanceError(
          "mobius.json must be a regular file inside the app source."
        )
      raw = manifest_file.read(MANIFEST_MAX_BYTES + 1)
      if len(raw) > MANIFEST_MAX_BYTES:
        raise CapabilityAcceptanceError(
          f"mobius.json exceeds the {MANIFEST_MAX_BYTES}-byte limit."
        )
      value = json.loads(raw.decode("utf-8"))
  except CapabilityAcceptanceError:
    raise
  except (OSError, UnicodeDecodeError, ValueError) as exc:
    raise CapabilityAcceptanceError(
      f"Could not read a valid mobius.json: {exc}"
    ) from exc
  finally:
    if source_descriptor is not None:
      os.close(source_descriptor)
    if root_descriptor is not None:
      os.close(root_descriptor)

  if not isinstance(value, dict):
    raise CapabilityAcceptanceError("mobius.json must contain an object.")
  return value


def _review(
  db: Session,
  app_id: int,
) -> tuple[models.App, dict, dict]:
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .one_or_none()
  )
  if app is None:
    raise CapabilityAcceptanceError("App not found.", status_code=404)
  if app.manifest_url is None:
    raise CapabilityAcceptanceError(
      "This operation is only for Store-installed apps."
    )

  manifest = _manifest_for(app)
  try:
    candidate = contract_with_runtime_capabilities(
      app.capability_contract,
      manifest,
    )
  except ValueError as exc:
    raise CapabilityAcceptanceError(str(exc)) from exc
  if candidate is None:
    raise CapabilityAcceptanceError(
      "The installed app has no reviewed capability contract."
    )
  report = {
    "app_id": app.id,
    "app": app.name,
    "current_runtime": app.capability_contract.get("runtime", {}),
    "candidate_runtime": candidate.get("runtime", {}),
    "diff": diff_contracts(app.capability_contract, candidate),
    # The digest covers the whole contract this acceptance would store, not
    # just the runtime projection.  A Store update that lands between review
    # and acceptance changes the baseline the owner compared against, so the
    # digest must stop matching and send them back to a fresh review.
    "accept_digest": capability_digest(candidate),
  }
  return app, candidate, report


def review_local_runtime_capabilities(db: Session, app_id: int) -> dict:
  """Return the current, normalized local runtime capability review."""
  _, _, report = _review(db, app_id)
  return {**report, "status": "review"}


def accept_local_runtime_capabilities(
  db: Session,
  app_id: int,
  accept_digest: str,
) -> dict:
  """Commit a reviewed runtime declaration and notify live app consumers.

  The digest is rebuilt from the current App row, so any change to the
  reviewed contract since the owner looked at it rejects here.  The event is
  emitted only after the replacement is durable, so a shell refetch can never
  observe a notification for state that never committed.
  """
  app, candidate, report = _review(db, app_id)
  if accept_digest != report["accept_digest"]:
    raise CapabilityAcceptanceError(
      "Acceptance digest does not match the current local runtime "
      "capability declaration; review again.",
      status_code=409,
    )

  app.capability_contract = candidate
  db.commit()

  get_system_broadcast().publish({
    "type": "app_updated",
    "appId": str(app.id),
  })
  return {**report, "status": "accepted"}
