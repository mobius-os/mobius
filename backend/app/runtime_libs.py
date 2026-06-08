"""Canonical list of mini-app runtime libraries externalized by esbuild."""

import re
from pathlib import Path

from app.config import get_settings

RUNTIME_LIBS: tuple[str, ...] = (
  "react",
  "react/jsx-runtime",
  "react-dom",
  "react-dom/client",
  "recharts",
  "date-fns",
  "three",
  "three/addons/*",
  "pdfjs-dist",
  # CodeMirror 6 + KaTeX — the importmap (app-frame.html) resolves these
  # at runtime; this list must externalize them or esbuild tries to bundle
  # the bare specifier and the install fails ("Could not resolve
  # 'codemirror'"). They were in the importmap (for the Notes app) but not
  # here, which made Notes uninstallable. tests/test_runtime_libs.py locks
  # the two lists together so the next addition can't desync.
  "codemirror",
  "@codemirror/state",
  "@codemirror/view",
  "@codemirror/commands",
  "@codemirror/language",
  "@codemirror/lang-markdown",
  "@lezer/highlight",
  "katex",
)


def frame_path_candidates() -> list[Path]:
  """Resolution order for app-frame.html — the single source of truth for the
  mini-app importmap. Matches routes/apps.py get_frame's `frame_candidates`
  exactly: agent-editable copy under /data first, then the dev-mode repo path,
  then the baked-in image fallback. (runtime_libs.py sits one directory shallower
  than routes/apps.py, so the repo path uses parents[2] here.)"""
  return [
    Path(get_settings().data_dir) / "shell" / "public" / "app-frame.html",
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
