"""Catalog apps must not be baked in as platform core apps."""

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


def test_no_catalog_apps_are_platform_core_apps():
    from app.source_dirs import CORE_APP_SLUGS

    assert _slugs() == []
    assert set(CORE_APP_SLUGS) == set()
