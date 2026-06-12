"""The frame's post-mount error guard must not be dead code.

handleFrameError in app-frame.html suppresses the error panel when
`window.__frameMounted` is truthy, so a transient unhandled rejection in a
RUNNING mini-app (e.g. an offline fetch failing at game-over) doesn't blank
a working UI. The guard shipped once with NOTHING ever setting the flag —
read-but-never-assigned made it dead code and every post-mount error still
destroyed the app. These tests lock the read and the write together, and
pin the write to a passive effect: createRoot().render() only SCHEDULES the
first render, so a synchronous assignment after it would also swallow
errors thrown during the initial render (which must still show the panel).
"""

import re
from pathlib import Path

import pytest

from app.config import get_settings


def _find_app_frame() -> Path | None:
  """Resolve app-frame.html the same way the frame route does, plus the
  repo-relative path so the local (non-Docker) test run finds it too."""
  candidates = [
    Path(get_settings().data_dir) / "shell" / "public" / "app-frame.html",
    Path(__file__).resolve().parents[2] / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
    Path("/app/static/app-frame.html"),
  ]
  return next((p for p in candidates if p.exists()), None)


def _frame_html() -> str:
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  return frame.read_text()


def test_error_guard_reads_mounted_flag():
  html = _frame_html()
  guard = re.search(
    r"function handleFrameError[^}]*window\.__frameMounted", html, re.DOTALL
  )
  assert guard, (
    "handleFrameError no longer gates on window.__frameMounted — post-mount "
    "errors in a running mini-app would blank the app to the error panel."
  )


def test_mounted_flag_is_set_inside_an_effect():
  html = _frame_html()
  # The assignment must exist (the original bug: guard read a flag nothing
  # set) and must live inside a useEffect callback so it only flips after
  # React COMMITS the first render — a synchronous set after render() would
  # suppress the panel for components that throw during their first render.
  assignment = re.search(
    r"useEffect\(\(\)\s*=>\s*\{\s*window\.__frameMounted\s*=\s*true", html
  )
  assert assignment, (
    "window.__frameMounted is never set inside a useEffect — the post-mount "
    "error guard is dead code (or fires before the first render commits)."
  )
