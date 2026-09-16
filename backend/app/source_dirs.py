"""Shared source-directory rules for installable mini-apps."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

SourceDirKind = Literal["app"]


def apps_root(data_dir: str | Path) -> Path:
  return (Path(data_dir) / "apps").resolve()


def platform_core_root(data_dir: str | Path) -> Path:
  """Legacy location used only to recognize old rows during migration."""
  return (Path(data_dir) / "platform" / "core-apps").resolve()


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
