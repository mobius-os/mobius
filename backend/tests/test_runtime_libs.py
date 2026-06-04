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


CODEMIRROR_DIRECT_IMPORTS = {
  "@codemirror/state",
  "@codemirror/view",
  "@codemirror/commands",
  "@codemirror/language",
  "@codemirror/lang-markdown",
  "@lezer/highlight",
}


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


def _standalone_importmap_keys() -> set[str]:
  """The standalone PWA route (routes/standalone.py) ships its OWN importmap
  embedded in a Python f-string, so its braces are doubled. It must carry the
  same specifiers as app-frame.html, or an app resolves a bare import in the
  shell but 404s the module on its standalone home-screen surface (the Notes
  /codemirror runtime regression)."""
  src = (
    Path(__file__).resolve().parents[1] / "app" / "routes" / "standalone.py"
  ).read_text()
  match = re.search(
    r'<script type="importmap">(.*?)</script>', src, re.DOTALL
  )
  assert match, "no importmap block found in standalone.py"
  block = match.group(1).strip().replace("{{", "{").replace("}}", "}")
  return set(json.loads(block)["imports"].keys())


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


def test_codemirror_subpackages_are_supported_as_direct_imports():
  """Notes and other editor apps can import the CodeMirror 6 packages they use
  directly instead of going through the umbrella `codemirror` re-export."""
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  keys = _importmap_keys(frame.read_text())
  runtime_libs = set(RUNTIME_LIBS)
  missing_importmap = sorted(CODEMIRROR_DIRECT_IMPORTS - keys)
  missing_external = sorted(CODEMIRROR_DIRECT_IMPORTS - runtime_libs)
  assert not missing_importmap, (
    "CodeMirror direct imports missing from app-frame importmap: "
    f"{missing_importmap}"
  )
  assert not missing_external, (
    "CodeMirror direct imports missing from RUNTIME_LIBS externals: "
    f"{missing_external}"
  )


def test_standalone_importmap_matches_app_frame():
  """The standalone PWA route and the in-shell frame must offer the SAME
  bare specifiers. They drifted once: codemirror/katex were added to
  app-frame.html (Notes worked in-shell) but not to standalone.py (Notes
  404'd the module on its home-screen surface). Keep them identical so an
  app resolves the same imports on both surfaces."""
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  shell_keys = _importmap_keys(frame.read_text())
  standalone_keys = _standalone_importmap_keys()
  only_in_shell = sorted(shell_keys - standalone_keys)
  only_in_standalone = sorted(standalone_keys - shell_keys)
  assert shell_keys == standalone_keys, (
    "in-shell (app-frame.html) and standalone (standalone.py) importmaps "
    f"have drifted. Only in app-frame: {only_in_shell}. Only in "
    f"standalone: {only_in_standalone}. An app's bare imports must resolve "
    "the same on both surfaces."
  )
