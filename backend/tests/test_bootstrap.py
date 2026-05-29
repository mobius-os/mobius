"""First-boot bootstrap — ensure_store_installed contract.

Validates the four behaviors that matter for boot-time correctness:
  1. Calls install_from_manifest with the pinned store URL when no
     App with slug='store' exists.
  2. Skips the install call when one already does.
  3. Swallows install failures so lifespan can't crash.
  4. Respects MOEBIUS_SKIP_BOOTSTRAP=1 so tests + offline boots don't
     hit GitHub.

The FastAPI startup-hook wiring itself isn't exercised here — that's
TestClient lifecycle territory and the in-process call is a one-liner
that's easier to read than test.
"""

from unittest.mock import patch, AsyncMock

import pytest
from fastapi import HTTPException

from app import models
from app.bootstrap import BOOTSTRAP_STORE_MANIFEST_URL, ensure_store_installed


@pytest.mark.asyncio
async def test_bootstrap_installs_store_when_absent(db, monkeypatch):
  """Empty DB → install_from_manifest called with the pinned URL."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  mock_app = models.App(id=1, name="Store", slug="store")
  install_mock = AsyncMock(return_value=(mock_app, "install", [], {}))
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)
  assert install_mock.await_count == 1
  call_kwargs = install_mock.await_args.kwargs
  assert call_kwargs["manifest_url"] == BOOTSTRAP_STORE_MANIFEST_URL
  assert call_kwargs["manifest"] is None
  assert call_kwargs["raw_base"] is None


@pytest.mark.asyncio
async def test_bootstrap_skips_when_store_already_installed(db, monkeypatch):
  """Pre-existing slug='store' → install_from_manifest never called."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  existing = models.App(
    name="Store",
    description="already here",
    jsx_source="export default function App() {}",
    slug="store",
  )
  db.add(existing)
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)
  install_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_logs_but_doesnt_raise_on_network_failure(
  db, monkeypatch, caplog,
):
  """install_from_manifest raises HTTPException(502) → returns cleanly.

  Bootstrap failure on first boot must not crash uvicorn — without
  the server, there's no recovery surface to install the store from.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  install_mock = AsyncMock(side_effect=HTTPException(502, "upstream down"))
  with patch("app.bootstrap.install_from_manifest", install_mock):
    # Should NOT raise — explicit assert on no-exception behavior.
    await ensure_store_installed(db)
  assert install_mock.await_count == 1
  # The failure is logged so an operator can find out why the store
  # didn't auto-install. Don't pin the exact message — just that it
  # surfaced as an exception-level record from the bootstrap logger.
  bootstrap_errors = [
    r for r in caplog.records
    if r.name == "mobius.bootstrap" and r.levelname == "ERROR"
  ]
  assert bootstrap_errors, "expected bootstrap failure to log at ERROR"


@pytest.mark.asyncio
async def test_bootstrap_respects_skip_env_var(db, monkeypatch):
  """MOEBIUS_SKIP_BOOTSTRAP=1 → install_from_manifest never called even
  when the DB is empty. This is the env var docker-compose.test.yml
  sets so the test suite can't accidentally reach GitHub."""
  monkeypatch.setenv("MOEBIUS_SKIP_BOOTSTRAP", "1")
  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)
  install_mock.assert_not_awaited()
