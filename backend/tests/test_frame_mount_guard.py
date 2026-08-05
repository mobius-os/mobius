"""The frame's post-mount error guard must not be dead code.

handleFrameError in app-frame.html suppresses the error panel when
`window.__frameMounted` is truthy, so a transient unhandled rejection in a
RUNNING mini-app (e.g. an offline fetch failing at game-over) doesn't blank
a working UI. The guard shipped once with NOTHING ever setting the flag —
read-but-never-assigned made it dead code and every post-mount error still
destroyed the app. These tests lock the read and the write together, and pin the write to a
commit-time ref callback: createRoot().render() only SCHEDULES the first
render, so a synchronous assignment after it would also swallow errors thrown
during the initial render (which must still show the panel). A ref runs during
commit — after the DOM is attached, before layout effects, and never on a
render that throws — so the flag means "committed, visible DOM".
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


def test_mounted_flag_is_set_by_a_commit_ref():
  html = _frame_html()
  # The assignment must exist (the original bug: the guard read a flag nothing
  # set) and must flip only when React COMMITS the first render. The signal is a
  # commit-time ref callback, not a synchronous set after createRoot().render()
  # (which only SCHEDULES the render and would suppress the panel for a component
  # that throws during it).
  assignment = re.search(
    r"function signalFrameMounted\([^)]*\)\s*\{[^}]*window\.__frameMounted\s*=\s*true",
    html,
    re.DOTALL,
  )
  assert assignment, (
    "window.__frameMounted is no longer assigned in signalFrameMounted — the "
    "post-mount error guard would read a flag nothing sets (dead code)."
  )
  # The setter ignores ref detach (a null node) and never flips twice, so a
  # re-attach cannot re-post 'mounted'.
  assert re.search(
    r"function signalFrameMounted\([^)]*\)\s*\{\s*if\s*\(!node\s*\|\|\s*"
    r"window\.__frameMounted\)\s*return",
    html,
  ), "signalFrameMounted no longer guards against a null node / double flip."
  # MountSignal must attach the setter as a commit ref, not a passive effect: a
  # ref runs during commit (DOM attached, before layout effects), never on an
  # aborted first render.
  mount_signal = html[
    html.index("function MountSignal"):
    html.index("// Immersive safe-area passthrough")
  ]
  assert re.search(r"ref:\s*signalFrameMounted", mount_signal), (
    "MountSignal no longer wires signalFrameMounted as a commit ref."
  )
  assert "useEffect" not in mount_signal, (
    "MountSignal sets the mount flag from an effect again — use the commit ref "
    "so 'mounted' means committed, visible DOM."
  )


def test_font_check_is_requested_separately_from_app_mount():
  html = _frame_html()
  mount_signal = html[
    html.index("function MountSignal"):
    html.index("// Immersive safe-area passthrough")
  ]
  assert "__mobiusFontReadiness" not in mount_signal
  assert "msg.type === 'moebius:frame-font-check'" in html
  assert "type: 'moebius:frame-font-check-result'" in html
