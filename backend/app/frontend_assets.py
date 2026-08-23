"""One request-time resolver for the editable and baked frontend builds."""

import os
import time
from pathlib import Path

_TTL_SECONDS = 1.0
_memo: dict[tuple[str, str], tuple[Path, float]] = {}


def live_frontend_dir(data_dir: str) -> Path:
  return Path(data_dir) / "platform" / "frontend" / "dist"


def baked_frontend_dir() -> Path:
  # The baked SPA is an image-level fallback, not relative to this clone.
  return Path(os.environ.get("MOBIUS_BAKED_STATIC_DIR", "/app/static"))


def is_complete_frontend_build(directory: Path) -> bool:
  return (
    directory.is_dir()
    and (directory / "assets").is_dir()
    and (directory / "index.html").is_file()
    and (directory / "sw.js").is_file()
    and (directory / "manifest.webmanifest").is_file()
  )


def resolve_frontend_dir(data_dir: str) -> Path:
  """Return the live complete build, otherwise the immutable baked fallback.

  Resolution is deliberately request-time: the frontend watcher swaps dist
  while the backend remains running. The short memo avoids repeated stat sets
  on asset-heavy pages without pinning a pre-swap decision.
  """
  live = live_frontend_dir(data_dir)
  baked = baked_frontend_dir()
  key = (str(live), str(baked))
  now = time.monotonic()
  cached = _memo.get(key)
  if cached is not None and now - cached[1] < _TTL_SECONDS:
    return cached[0]
  resolved = live if is_complete_frontend_build(live) else baked
  _memo[key] = (resolved, now)
  return resolved


def reset_frontend_dir_cache() -> None:
  """Test/maintenance seam for deliberate generation swaps."""
  _memo.clear()
