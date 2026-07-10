"""Legacy no-op helpers for the retired core-app suppression marker.

Catalog apps are no longer re-seeded from platform-owned source on boot, so
ordinary tombstones are enough: uninstalling Memory, Reflection, or Beat Machine
does not need a durable "do not resurrect" marker. The module remains as a tiny
compatibility shim for routes/tests that still call clear/list while old marker
files may exist on disk.
"""

from pathlib import Path

SUPPRESSIBLE_CORE_SLUGS = frozenset()

# Relative to data_dir. Kept so old marker files are still inspectable and can
# be cleaned up by recover/reinstall paths.
_SUPPRESS_SUBDIR = "shared/suppressed-core-apps"


def is_suppressible_core_slug(slug: str | None) -> bool:
  """Compatibility helper: no installable catalog app is suppressible now."""
  return bool(slug) and slug in SUPPRESSIBLE_CORE_SLUGS


def _marker_path(data_dir, slug: str) -> Path:
  return Path(data_dir) / _SUPPRESS_SUBDIR / slug


def mark_suppressed(data_dir, slug: str, *, app_id: int | None = None) -> None:
  """No-op: catalog apps are not boot-reseeded anymore."""
  return


def clear_suppressed(data_dir, slug: str) -> None:
  """Remove the suppression marker for ``slug`` — the owner brought it back.

  No-op when there is no marker (the common case for a normal recover/install).
  """
  if not slug:
    return
  try:
    _marker_path(data_dir, slug).unlink(missing_ok=True)
  except OSError:
    pass


def is_suppressed(data_dir, slug: str) -> bool:
  """True when an old suppression marker still exists for ``slug``."""
  return bool(slug) and _marker_path(data_dir, slug).exists()


def list_suppressed(data_dir) -> set[str]:
  """Return old suppression markers that may still exist on disk."""
  directory = Path(data_dir) / _SUPPRESS_SUBDIR
  try:
    return {p.name for p in directory.iterdir() if p.is_file()}
  except OSError:
    return set()
