"""Shared source-directory rules for mini-apps and platform core apps."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

SourceDirKind = Literal["app", "platform_core"]

# Built-in apps whose editable source is owned by the platform repo when
# /data/platform is active. Ordinary user/store app source remains under
# /data/apps/<slug>.
CORE_APP_SLUGS = frozenset({"memory", "reflection", "beat-machine"})


def apps_root(data_dir: str | Path) -> Path:
  return (Path(data_dir) / "apps").resolve()


def platform_core_root(data_dir: str | Path) -> Path:
  return (Path(data_dir) / "platform" / "core-apps").resolve()


def is_core_app_slug(slug: str | None) -> bool:
  return bool(slug) and slug in CORE_APP_SLUGS


def core_source_dir(data_dir: str | Path, slug: str) -> Path:
  return platform_core_root(data_dir) / slug


def source_dir_kind(
  source_dir: str | Path | None, data_dir: str | Path,
) -> SourceDirKind | None:
  """Classify an absolute source dir under the two approved source roots."""
  if not source_dir:
    return None
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    return None

  root = apps_root(data_dir)
  if resolved.parent == root and not resolved.name.isdigit():
    return "app"

  core_root = platform_core_root(data_dir)
  if resolved.parent == core_root and is_core_app_slug(resolved.name):
    return "platform_core"

  return None


def is_platform_core_source_dir(
  source_dir: str | Path | None, data_dir: str | Path,
) -> bool:
  return source_dir_kind(source_dir, data_dir) == "platform_core"


def source_dir_for_changed_path(
  path: str | Path, data_dir: str | Path,
) -> Path | None:
  """Return the owning source dir for a source-like file change.

  The owner is the immediate child of either /data/apps or
  /data/platform/core-apps. The caller is responsible for extension and
  ignored-subdir filtering before calling this helper.
  """
  p = Path(path)
  try:
    resolved = p.resolve()
  except (OSError, RuntimeError):
    return None

  for root, require_core_slug in (
    (apps_root(data_dir), False),
    (platform_core_root(data_dir), True),
  ):
    try:
      rel = resolved.relative_to(root)
    except ValueError:
      continue
    if len(rel.parts) < 2:
      return None
    slug = rel.parts[0]
    if slug.isdigit():
      return None
    if require_core_slug and not is_core_app_slug(slug):
      return None
    return root / slug
  return None
