"""Canonical source locations for installed apps."""

from pathlib import Path


def resolve_app_source_dir(app_source_dir: str) -> Path:
  """Resolve the canonical source tree stored on an app row."""
  return Path(app_source_dir).resolve()
