"""One-time ownership split for app icons stored before package/override separation.

Historically ``App.icon_png`` held whichever artwork was effective: either the
accepted manifest icon or an owner upload.  The split model gives those writers
separate columns, but the old bytes have no provenance flag.  This module
classifies them once against the exact accepted Git revision when possible and
otherwise preserves them conservatively as an owner override.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from app import app_git, icon_assets
from app.manifest_contract import (
  ICON_MAX_BYTES,
  ManifestContractError,
  validate_repo_relative_path,
)


@dataclass(frozen=True)
class IconOwnershipTransition:
  """Visible outcome of attempting the one-time ownership split."""

  changed: bool
  warning: str | None = None


@dataclass(frozen=True)
class _AcceptedPackageIcon:
  known: bool
  content: bytes | None = None
  warning: str | None = None


def _accepted_package_icon(app) -> _AcceptedPackageIcon:
  """Read normalized package artwork from the app's accepted Git revision."""
  if not app.source_dir or not app.source_commit:
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_icon_source_unavailable",
    )
  try:
    raw_manifest = app_git.read_blob(
      app.source_dir, app.source_commit, "mobius.json",
    )
  except (OSError, subprocess.SubprocessError):
    raw_manifest = None
  if raw_manifest is None:
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_manifest_unavailable",
    )
  try:
    manifest = json.loads(raw_manifest)
  except (UnicodeDecodeError, json.JSONDecodeError):
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_manifest_invalid",
    )
  if not isinstance(manifest, Mapping):
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_manifest_invalid",
    )

  relative = manifest.get("icon")
  if relative is None:
    return _AcceptedPackageIcon(known=True, content=None)
  try:
    validate_repo_relative_path(relative, "icon")
  except ManifestContractError:
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_icon_declaration_invalid",
    )
  try:
    raw_icon = app_git.read_blob(
      app.source_dir, app.source_commit, relative,
    )
  except (OSError, subprocess.SubprocessError):
    raw_icon = None
  if raw_icon is None:
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_icon_unavailable",
    )
  if len(raw_icon) > ICON_MAX_BYTES:
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_icon_invalid",
    )
  try:
    package_icon = icon_assets.normalize_icon(raw_icon)
  except icon_assets.InvalidIcon:
    return _AcceptedPackageIcon(
      known=False,
      warning="accepted_icon_invalid",
    )
  return _AcceptedPackageIcon(known=True, content=package_icon)


def split_legacy_icon_ownership(app) -> IconOwnershipTransition:
  """Classify a pre-split effective icon without risking owner artwork.

  A byte-identical accepted manifest icon is package artwork.  Different legacy
  bytes are an owner override layered over that accepted package icon.  When
  immutable accepted provenance cannot be read, the legacy bytes become an
  override and the package slot is cleared; a later accepted update can then
  repopulate package artwork without ever replacing the preserved owner choice.
  """
  if app.icon_ownership_split:
    return IconOwnershipTransition(changed=False)

  legacy_effective = (
    app.icon_override_png
    if app.icon_override_png is not None
    else app.icon_png
  )
  accepted = _accepted_package_icon(app)
  if accepted.known:
    if (
      app.icon_override_png is None
      and legacy_effective is not None
      and legacy_effective != accepted.content
    ):
      app.icon_override_png = legacy_effective
    app.icon_png = accepted.content
  else:
    if app.icon_override_png is None:
      app.icon_override_png = legacy_effective
    # Unknown historical bytes must not remain in the package-owned slot:
    # the next accepted apply/update is allowed to replace that slot.
    app.icon_png = None

  app.icon_ownership_split = True
  return IconOwnershipTransition(changed=True, warning=accepted.warning)
