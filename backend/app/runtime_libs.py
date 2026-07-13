"""Canonical list of mini-app runtime libraries externalized by esbuild."""

import re
from pathlib import Path

from app.app_compile_contract import RUNTIME_LIBS
from app.config import get_settings


def frame_path_candidates() -> list[Path]:
  """Resolution order for app-frame.html — the single source of truth for the
  mini-app importmap. Matches routes/apps.py get_frame's `frame_candidates`
  exactly: served platform frontend first, then the baked-in image fallback."""
  return [
    Path(get_settings().data_dir)
    / "platform" / "frontend" / "public" / "app-frame.html",
    # Repo-relative: resolves to the served clone in the container (backend
    # runs from /data/platform/backend) AND to the checkout in dev/test where
    # /data/platform and /app do not exist.
    Path(__file__).resolve().parents[2] / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
  ]


def resolve_frame_path() -> Path | None:
  return next((p for p in frame_path_candidates() if p.exists()), None)


def importmap_block() -> str:
  """The JSON object inside app-frame.html's <script type="importmap">, as text.

  app-frame.html is the ONE source of truth for the mini-app importmap; the
  standalone PWA host (routes/standalone.py) reads it through here instead of
  carrying its own hand-synced copy (which drifted once — codemirror/katex added
  in-shell but not standalone, 404'ing Notes on its home-screen surface)."""
  frame = resolve_frame_path()
  if frame is None:
    raise FileNotFoundError(
      "app-frame.html not resolvable from any of: "
      + ", ".join(str(p) for p in frame_path_candidates())
    )
  match = re.search(
    r'<script type="importmap">\s*(\{.*?\})\s*</script>',
    frame.read_text(),
    re.DOTALL,
  )
  if not match:
    raise ValueError(f"no <script type=\"importmap\"> block found in {frame}")
  return match.group(1)
