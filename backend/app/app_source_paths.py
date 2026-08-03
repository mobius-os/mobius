"""Canonical source locations for installed apps."""

from pathlib import Path


def resolve_app_source_dir(app_source_dir) -> Path | None:
  """Resolve the canonical stored source tree, or None when it is invalid."""
  if not app_source_dir:
    return None
  try:
    return Path(app_source_dir).resolve()
  except OSError:
    return None
