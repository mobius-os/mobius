"""Every built-in core app must ship a complete, installable manifest.

This guards the class of bug where a rename or refactor leaves a
`core-apps/<slug>/` dir without its `mobius.json` (or without an `id`/entry),
which `install-core-apps.sh` cannot register — so a fresh instance silently
fails to install that built-in. It reached origin/main once (a dreaming->
reflection rename dropped reflection's manifest); a cheap read-only check
catches it in CI.
"""

import json
from pathlib import Path

import pytest


def _core_apps_dir() -> Path:
    # Baked into the image at /app/core-apps; lives at the repo root in a
    # local checkout (this file is backend/tests/, so two parents up).
    for candidate in (Path("/app/core-apps"), Path(__file__).resolve().parents[2] / "core-apps"):
        if candidate.is_dir():
            return candidate
    pytest.skip("core-apps/ not found in this environment")


def _slugs() -> list[Path]:
    return sorted(p for p in _core_apps_dir().iterdir() if p.is_dir())


def test_core_apps_present():
    assert _slugs(), "no built-in apps found under core-apps/"


@pytest.mark.parametrize("app", _slugs(), ids=lambda p: p.name)
def test_core_app_manifest_is_installable(app: Path):
    manifest = app / "mobius.json"
    assert manifest.is_file(), f"{app.name}: missing mobius.json (cannot be installed)"

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(data.get("id"), str) and data["id"], f"{app.name}: mobius.json has no id"
    assert isinstance(data.get("name"), str) and data["name"], f"{app.name}: mobius.json has no name"

    entry = data.get("entry", "index.jsx")
    assert (app / entry).is_file(), f"{app.name}: declared entry '{entry}' is missing"
