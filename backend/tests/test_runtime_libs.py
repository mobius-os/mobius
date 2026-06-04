"""RUNTIME_LIBS (esbuild externals) must stay in sync with the mini-app
importmap in app-frame.html.

A bare import an app makes (e.g. `from "codemirror"`) is resolved at
runtime by the importmap, but it only *compiles* if RUNTIME_LIBS marks the
specifier external — otherwise esbuild tries to bundle it and the install
fails ("Could not resolve 'codemirror'"). The two lists drifted once
(codemirror/katex were added to the importmap for the Notes app but not to
RUNTIME_LIBS), which made Notes uninstallable. These tests lock the lists
together so the next addition to either side can't silently desync.
"""

import json
import re
from pathlib import Path

import pytest

from app.config import get_settings
from app.runtime_libs import RUNTIME_LIBS


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


def _importmap_keys(html: str) -> set[str]:
  match = re.search(
    r'<script type="importmap">\s*(\{.*?\})\s*</script>', html, re.DOTALL
  )
  assert match, "no importmap block found in app-frame.html"
  return set(json.loads(match.group(1))["imports"].keys())


def _externalized(key: str) -> bool:
  """True if `key` is marked external by RUNTIME_LIBS. A wildcard entry
  like "three/addons/*" externalizes every subpath, which also covers the
  importmap's "three/addons/" prefix key."""
  if key in RUNTIME_LIBS:
    return True
  return any(
    lib.endswith("/*") and key.startswith(lib[:-1]) for lib in RUNTIME_LIBS
  )


def _has_importmap_entry(lib: str, keys: set[str]) -> bool:
  if lib in keys:
    return True
  # "three/addons/*" is backed by the importmap's "three/addons/" prefix.
  return lib.endswith("/*") and lib[:-1] in keys


def test_importmap_specifiers_are_all_externalized():
  """Every importmap specifier must be externalized, or an app importing
  it fails to compile (the Notes/codemirror regression)."""
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  keys = _importmap_keys(frame.read_text())
  missing = sorted(k for k in keys if not _externalized(k))
  assert not missing, (
    "importmap specifiers not externalized by RUNTIME_LIBS — apps "
    f"importing these fail to compile: {missing}. Add them to "
    "backend/app/runtime_libs.py."
  )


def test_externalized_libs_all_have_importmap_entries():
  """Every externalized lib must have an importmap entry, or an app
  importing it compiles but fails to resolve the bare specifier at
  runtime."""
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  keys = _importmap_keys(frame.read_text())
  missing = sorted(
    lib for lib in RUNTIME_LIBS if not _has_importmap_entry(lib, keys)
  )
  assert not missing, (
    "RUNTIME_LIBS entries with no importmap mapping — apps importing "
    f"these compile but fail to resolve at runtime: {missing}. Add them "
    "to the importmap in frontend/public/app-frame.html."
  )
