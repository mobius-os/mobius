"""Canonical source and legacy-runtime locations for installed apps."""

import re
from pathlib import Path

from app import legacy_platform_apps, models
from app.config import get_settings


def legacy_platform_runtime_dir_for_app(app: models.App) -> Path | None:
  """Return the old cron-replay sidecar dir for retired platform-core rows."""
  settings = get_settings()
  if not legacy_platform_apps.is_legacy_source_dir(
    app.source_dir, settings.data_dir, app.slug,
  ):
    return None
  return legacy_platform_apps.runtime_sidecar_dir(settings.data_dir, app.slug)


def resolve_app_source_dir(app_source_dir, app_name, settings) -> Path | None:
  """Resolve an app's source tree: the stored source_dir, else a name-based
  fallback for legacy rows. Returns None when neither resolves."""
  if app_source_dir:
    try:
      return Path(app_source_dir).resolve()
    except OSError:
      return None
  if app_name and re.fullmatch(r"[a-zA-Z0-9_-]+", app_name):
    try:
      return (Path(settings.data_dir) / "apps" / app_name).resolve()
    except OSError:
      return None
  return None
