"""Migration helpers for apps that used to ship from platform core-apps."""

from __future__ import annotations

from pathlib import Path

from app import source_dirs

MANIFEST_URLS = {
  "memory": "https://raw.githubusercontent.com/mobius-os/app-memory/main/mobius.json",
  "reflection": "https://raw.githubusercontent.com/mobius-os/app-reflection/main/mobius.json",
  "beat-machine": "https://raw.githubusercontent.com/mobius-os/app-beat-machine/main/mobius.json",
}

SLUGS = frozenset(MANIFEST_URLS)


def is_legacy_source_dir(
  source_dir: str | Path | None,
  data_dir: str | Path,
  slug: str | None,
) -> bool:
  """True for old rows whose source was owned by /data/platform/core-apps."""
  if not source_dir or not slug or slug not in SLUGS:
    return False
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    return False
  return (
    resolved.parent == source_dirs.platform_core_root(data_dir)
    and resolved.name == slug
  )


def runtime_sidecar_dir(data_dir: str | Path, slug: str | None) -> Path | None:
  """Old platform-core scheduled apps replayed cron from /data/apps/<slug>."""
  if slug not in SLUGS:
    return None
  return (Path(data_dir) / "apps" / str(slug)).resolve()
