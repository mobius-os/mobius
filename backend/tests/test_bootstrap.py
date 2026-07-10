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
from app.bootstrap import (
  BOOTSTRAP_STORE_MANIFEST_URL,
  LEGACY_PLATFORM_APP_MANIFEST_URLS,
  ensure_store_installed,
)


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
  install_mock = AsyncMock(return_value=(mock_app, "install", [], {}, [], "none"))
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
  from app.install import _canonical_identity_key
  existing = models.App(
    name="Store",
    description="already here",
    jsx_source="export default function App() {}",
    slug="app-store",
    # Install stores the CANONICAL key, not the bare URL — seed that so the
    # bootstrap's canonical-prefix lookup matches (Codex review round-11 #1).
    manifest_url=_canonical_identity_key(BOOTSTRAP_STORE_MANIFEST_URL, "store"),
  )
  db.add(existing)
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)
  install_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_url_shape", ["empty", "raw", "canonical"])
@pytest.mark.parametrize("source_shape", ["platform_core", "data_apps"])
async def test_bootstrap_migrates_active_legacy_platform_rows(
  source_shape, manifest_url_shape, db, monkeypatch,
):
  """The migration is ONE-SHOT: it moves un-migrated baked rows forward through
  the trusted catalog entry, but never re-fires on a row that already carries a
  manifest_url.

  Two un-migrated shapes exist. A `platform_core` row still points at
  /data/platform/core-apps and is migrated regardless of its manifest_url (the
  catalog install moves it out of that tree, so it stops matching next boot). A
  `data_apps` row already sits at the steady-state /data/apps/<slug> path, so it
  only counts as un-migrated while its manifest_url is empty — once the identity
  is stamped (raw or canonical), re-migrating would re-fetch GitHub every restart
  and could fast-forward the app past owner review. Matching data_apps + a set
  manifest_url as legacy was the bug this pins closed.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.config import get_settings
  from app.install import _canonical_identity_key

  data_dir = get_settings().data_dir
  legacy_source = {
    "platform_core": f"{data_dir}/platform/core-apps/memory",
    "data_apps": f"{data_dir}/apps/memory",
  }[source_shape]
  raw_memory_manifest = LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"]
  stored_manifest_url = {
    "empty": None,
    "raw": raw_memory_manifest,
    "canonical": _canonical_identity_key(raw_memory_manifest, "memory"),
  }[manifest_url_shape]
  db.add_all([
    models.App(
      name="Memory",
      description="legacy platform app",
      jsx_source="export default function App() {}",
      slug="memory",
      source_dir=legacy_source,
      manifest_url=stored_manifest_url,
    ),
    models.App(
      name="Store",
      description="already here",
      jsx_source="export default function App() {}",
      slug="store",
      manifest_url=_canonical_identity_key(BOOTSTRAP_STORE_MANIFEST_URL, "store"),
    ),
  ])
  db.commit()

  # A platform-core row is always un-migrated (source still in that tree); a
  # data_apps row is un-migrated only while its manifest_url is empty.
  should_migrate = source_shape == "platform_core" or manifest_url_shape == "empty"

  mock_app = models.App(id=3, name="Memory", slug="memory")
  install_mock = AsyncMock(return_value=(mock_app, "update", [], {}, [], "none"))
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_store_installed(db)

  if should_migrate:
    assert install_mock.await_count == 1
    assert (
      install_mock.await_args.kwargs["manifest_url"]
      == LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"]
    )
    assert install_mock.await_args.kwargs["source"] == "bootstrap"
  else:
    assert install_mock.await_count == 0


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
  install_mock = AsyncMock(return_value=(mock_app, "install", [], {}, [], "none"))
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
