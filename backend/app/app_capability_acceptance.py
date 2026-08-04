"""Review and accept locally declared runtime capabilities for Store apps.

Store installs retain their reviewed package contract when local source is
applied.  This module owns the narrower exception: after an owner reviews the
normalized runtime declaration in that source, it can replace only the
contract's ``runtime`` field.  The acceptance digest identifies those
normalized runtime semantics, not unrelated manifest bytes or App fields.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from sqlalchemy.orm import Session

from app import models, timeutil
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
  data_dir = Path(get_settings().data_dir).resolve()
  apps_root = (data_dir / "apps").resolve()
  # Resolving the stored path would turn a symlink to a sibling app into an
  # apparently valid direct child and lose the Store app's source identity.
  source_dir = Path(os.path.abspath(app.source_dir))
  if source_dir.parent != apps_root or source_dir.name.isdigit():
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
    root_descriptor = os.open(apps_root, directory_flags)
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
    .populate_existing()
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
  candidate_runtime = candidate.get("runtime", {})
  report = {
    "app_id": app.id,
    "app": app.name,
    "current_runtime": app.capability_contract.get("runtime", {}),
    "candidate_runtime": candidate_runtime,
    "diff": diff_contracts(app.capability_contract, candidate),
    # Approval is intentionally scoped to the normalized declaration displayed
    # above. Formatting and unrelated package facts do not change its meaning.
    "accept_digest": capability_digest(candidate_runtime),
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

  The App-row revision compare-and-swap preserves any concurrent update.  The
  event is emitted only after the replacement is durable, so a shell refetch
  can never observe a notification for rolled-back state.
  """
  try:
    app, candidate, report = _review(db, app_id)
    if accept_digest != report["accept_digest"]:
      raise CapabilityAcceptanceError(
        "Acceptance digest does not match the current local runtime "
        "capability declaration; review again.",
        status_code=409,
      )

    snapshot_updated_at = app.updated_at
    current_updated_at = models.App.updated_at
    revision_match = (
      current_updated_at.is_(None)
      if snapshot_updated_at is None
      else current_updated_at == snapshot_updated_at
    )
    changed = (
      db.query(models.App)
      .filter(
        models.App.id == app.id,
        models.App.deleted_at.is_(None),
        revision_match,
      )
      .update({
        models.App.capability_contract: candidate,
        models.App.updated_at: timeutil.now_naive_utc(),
      }, synchronize_session=False)
    )
    if changed != 1:
      raise CapabilityAcceptanceError(
        "The app changed while capabilities were being accepted; review again.",
        status_code=409,
      )
    db.commit()
  except Exception:
    db.rollback()
    raise

  get_system_broadcast().publish({
    "type": "app_updated",
    "appId": str(app.id),
  })
  return {**report, "status": "accepted"}
