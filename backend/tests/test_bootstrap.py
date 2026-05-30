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
  """No App with the bootstrap manifest_url → install_from_manifest
  called with the pinned URL.

  Pre-assert that no row matches the manifest_url so the test's
  starting state is unambiguous — a slug='store' row with a different
  manifest_url would still satisfy "bootstrap absent" under the new
  identity rule.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  assert (
    db.query(models.App)
    .filter(models.App.manifest_url == BOOTSTRAP_STORE_MANIFEST_URL)
    .first()
  ) is None
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
  """Pre-existing row with the bootstrap manifest_url → install never
  called.

  Identity is keyed on manifest_url (not slug) so an app the user
  built that happens to be called "store" doesn't accidentally
  satisfy this check, and the bootstrapped store's actual slug
  (which may be 'app-store' if 'store' was taken) doesn't have to
  be guessed.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  existing = models.App(
    name="Store",
    description="already here",
    jsx_source="export default function App() {}",
    slug="app-store",
    manifest_url=BOOTSTRAP_STORE_MANIFEST_URL,
  )
  db.add(existing)
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)
  install_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_ignores_unrelated_store_slug(db, monkeypatch):
  """A user-built app with slug='store' but no bootstrap manifest_url
  does NOT prevent bootstrap. Under the prior slug-keyed check, this
  case mis-treated the bootstrapped store as already-installed and
  skipped the real install.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  user_built = models.App(
    name="Store",
    description="user's own app, unrelated to the bootstrap manifest",
    jsx_source="export default function App() {}",
    slug="store",
    manifest_url=None,
  )
  db.add(user_built)
  db.commit()

  mock_app = models.App(id=2, name="Store", slug="app-store")
  install_mock = AsyncMock(return_value=(mock_app, "install", [], {}))
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)
  assert install_mock.await_count == 1
  assert (
    install_mock.await_args.kwargs["manifest_url"]
    == BOOTSTRAP_STORE_MANIFEST_URL
  )


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
