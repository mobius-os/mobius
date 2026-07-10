"""Shared source-directory rules for installable mini-apps."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

SourceDirKind = Literal["app"]

# Installable apps own their editable source under /data/apps/<slug>. The App
# Store itself is bootstrapped separately; no catalog app is platform-owned.
CORE_APP_SLUGS = frozenset()


def apps_root(data_dir: str | Path) -> Path:
  return (Path(data_dir) / "apps").resolve()


def platform_core_root(data_dir: str | Path) -> Path:
  """Legacy location used only to recognize old rows during migration."""
  return (Path(data_dir) / "platform" / "core-apps").resolve()


def is_core_app_slug(slug: str | None) -> bool:
  """Compatibility helper: catalog apps are no longer platform-core apps."""
  return bool(slug) and slug in CORE_APP_SLUGS


def core_source_dir(data_dir: str | Path, slug: str) -> Path:
  """Legacy platform-core source path for old-row migration checks."""
  return platform_core_root(data_dir) / slug


def source_dir_kind(
  source_dir: str | Path | None, data_dir: str | Path,
) -> SourceDirKind | None:
  """Classify an absolute source dir under the approved app source root."""
  if not source_dir:
    return None
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    return None

  root = apps_root(data_dir)
  if resolved.parent == root and not resolved.name.isdigit():
    return "app"

  return None


def is_platform_core_source_dir(
  source_dir: str | Path | None, data_dir: str | Path,
) -> bool:
  return False


def source_dir_for_changed_path(
  path: str | Path, data_dir: str | Path,
) -> Path | None:
  """Return the owning source dir for a source-like file change.

  The owner is the immediate child of /data/apps. The caller is responsible for
  extension and ignored-subdir filtering before calling this helper.
  """
  p = Path(path)
  try:
    resolved = p.resolve()
  except (OSError, RuntimeError):
    return None

  root = apps_root(data_dir)
  try:
    rel = resolved.relative_to(root)
  except ValueError:
    return None
  if len(rel.parts) < 2:
    return None
  slug = rel.parts[0]
  if slug.isdigit():
    return None
  return root / slug
