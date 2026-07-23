"""First-boot bootstrap — ensure_bootstrap_apps_installed contract.

Validates the boot-time invariants: ordered installs, canonical manifest
identity, per-app uninstall policy and failure isolation, legacy migration,
and the offline-test escape hatch.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import models
from app.bootstrap import (
  BOOTSTRAP_SKILLS_MANIFEST_URL,
  BOOTSTRAP_STORE_MANIFEST_URL,
  LEGACY_PLATFORM_APP_MANIFEST_URLS,
  _migrate_legacy_platform_apps,
  ensure_bootstrap_apps_installed,
)


def _install_result(name="App", slug="app", app_id=1, mode="install"):
  app = models.App(id=app_id, name=name, slug=slug)
  return app, mode, [], {}, [], "none"


def _bootstrap_urls():
  return [
    BOOTSTRAP_STORE_MANIFEST_URL,
    BOOTSTRAP_SKILLS_MANIFEST_URL,
    LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"],
    LEGACY_PLATFORM_APP_MANIFEST_URLS["reflection"],
  ]


@pytest.mark.asyncio
async def test_bootstrap_installs_all_apps_in_order_when_absent(db, monkeypatch):
  """A fresh database installs the store first, then Memory and Reflection."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  install_mock = AsyncMock(return_value=_install_result())

  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  assert install_mock.await_count == 4
  assert [
    call.kwargs["manifest_url"] for call in install_mock.await_args_list
  ] == _bootstrap_urls()
  for call in install_mock.await_args_list:
    assert call.kwargs["manifest"] is None
    assert call.kwargs["raw_base"] is None
    assert call.kwargs["source"] == "bootstrap"


@pytest.mark.asyncio
async def test_bootstrap_applies_per_app_uninstall_policy(db, monkeypatch):
  """Store returns after uninstall; Skills/Memory stay gone; live Reflection skips."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key

  deleted_at = datetime.now(timezone.utc)
  db.add_all([
    models.App(
      name="Store",
      description="owner uninstalled",
      jsx_source="export default function App() {}",
      slug="store",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_STORE_MANIFEST_URL, "store",
      ),
      deleted_at=deleted_at,
    ),
    models.App(
      name="Skills",
      description="owner uninstalled",
      jsx_source="export default function App() {}",
      slug="skills",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_SKILLS_MANIFEST_URL, "skills",
      ),
      deleted_at=deleted_at,
    ),
    models.App(
      name="Memory",
      description="owner uninstalled",
      jsx_source="export default function App() {}",
      slug="memory",
      manifest_url=_canonical_identity_key(
        LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"], "memory",
      ),
      deleted_at=deleted_at,
    ),
    models.App(
      name="Reflection",
      description="already here",
      jsx_source="export default function App() {}",
      slug="reflection",
      manifest_url=_canonical_identity_key(
        LEGACY_PLATFORM_APP_MANIFEST_URLS["reflection"], "reflection",
      ),
    ),
  ])
  db.commit()

  install_mock = AsyncMock(return_value=_install_result("Store", "store"))
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  # Only the Store (the recovery surface) returns after an owner uninstall;
  # Skills and Memory (policy False) stay gone; live Reflection is skipped.
  assert install_mock.await_count == 1
  assert [
    call.kwargs["manifest_url"] for call in install_mock.await_args_list
  ] == [BOOTSTRAP_STORE_MANIFEST_URL]


@pytest.mark.asyncio
async def test_bootstrap_skips_live_apps_by_canonical_manifest(db, monkeypatch):
  """Canonical manifest identity, rather than slug, makes installs idempotent."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key

  db.add_all([
    models.App(
      name="Store",
      description="already here",
      jsx_source="export default function App() {}",
      slug="app-store",
      manifest_url=_canonical_identity_key(BOOTSTRAP_STORE_MANIFEST_URL, "store"),
    ),
    models.App(
      name="Skills",
      description="already here",
      jsx_source="export default function App() {}",
      slug="skills-custom",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_SKILLS_MANIFEST_URL, "skills",
      ),
    ),
    models.App(
      name="Memory",
      description="already here",
      jsx_source="export default function App() {}",
      slug="memory-custom",
      manifest_url=_canonical_identity_key(
        LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"], "memory",
      ),
    ),
    models.App(
      name="Reflection",
      description="already here",
      jsx_source="export default function App() {}",
      slug="reflection-custom",
      manifest_url=_canonical_identity_key(
        LEGACY_PLATFORM_APP_MANIFEST_URLS["reflection"], "reflection",
      ),
    ),
  ])
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)
  install_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_url_shape", ["empty", "raw", "canonical"])
@pytest.mark.parametrize("source_shape", ["platform_core", "data_apps"])
async def test_bootstrap_migrates_active_legacy_platform_rows(
  source_shape, manifest_url_shape, db, monkeypatch,
):
  """The legacy migration remains one-shot and limited to retired sources."""
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
  db.add(models.App(
    name="Memory",
    description="legacy platform app",
    jsx_source="export default function App() {}",
    slug="memory",
    source_dir=legacy_source,
    manifest_url=stored_manifest_url,
  ))
  db.commit()

  should_migrate = source_shape == "platform_core" or manifest_url_shape == "empty"
  install_mock = AsyncMock(
    return_value=_install_result("Memory", "memory", app_id=3, mode="update"),
  )
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await _migrate_legacy_platform_apps(db)

  if should_migrate:
    install_mock.assert_awaited_once()
    assert (
      install_mock.await_args.kwargs["manifest_url"]
      == LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"]
    )
    assert install_mock.await_args.kwargs["source"] == "bootstrap"
  else:
    install_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_ignores_unrelated_store_slug(db, monkeypatch):
  """A user-built app named store does not satisfy canonical app identity."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  db.add(models.App(
    name="Store",
    description="user's own app, unrelated to the bootstrap manifest",
    jsx_source="export default function App() {}",
    slug="store",
    manifest_url=None,
  ))
  db.commit()

  install_mock = AsyncMock(return_value=_install_result())
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  assert [
    call.kwargs["manifest_url"] for call in install_mock.await_args_list
  ] == _bootstrap_urls()


@pytest.mark.asyncio
async def test_bootstrap_failure_doesnt_block_remaining_apps(
  db, monkeypatch, caplog,
):
  """A failed first install is logged and the remaining apps still install."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  install_mock = AsyncMock(side_effect=[
    HTTPException(502, "upstream down"),
    _install_result("Skills", "skills", app_id=4),
    _install_result("Memory", "memory", app_id=2),
    _install_result("Reflection", "reflection", app_id=3),
  ])
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  assert install_mock.await_count == 4
  assert [
    call.kwargs["manifest_url"] for call in install_mock.await_args_list
  ] == _bootstrap_urls()
  bootstrap_errors = [
    record for record in caplog.records
    if record.name == "mobius.bootstrap" and record.levelname == "ERROR"
  ]
  assert bootstrap_errors, "expected bootstrap failure to log at ERROR"


@pytest.mark.asyncio
async def test_bootstrap_respects_skip_env_var(db, monkeypatch):
  """MOEBIUS_SKIP_BOOTSTRAP=1 skips migrations and every app install."""
  monkeypatch.setenv("MOEBIUS_SKIP_BOOTSTRAP", "1")
  install_mock = AsyncMock()
  migration_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock), \
       patch("app.bootstrap._migrate_legacy_platform_apps", migration_mock):
    await ensure_bootstrap_apps_installed(db)
  install_mock.assert_not_awaited()
  migration_mock.assert_not_awaited()


_SKILLS_MAIN_MANIFEST = (
  "https://raw.githubusercontent.com/mobius-os/app-skills/main/mobius.json"
)


@pytest.mark.asyncio
async def test_bootstrap_recognizes_skills_row_installed_at_other_ref(
  db, monkeypatch,
):
  """F-1: the pinned bootstrap URL names a COMMIT, but a skills row installed at
  `main` is the SAME app (identity is the repo, not the ref) — bootstrap must
  recognize it and never reinstall a duplicate."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key, _trusted_catalog_repo_base

  # Guard: this test is only meaningful while the pin is a non-`main` ref.
  assert _trusted_catalog_repo_base(BOOTSTRAP_SKILLS_MANIFEST_URL) == (
    "https://raw.githubusercontent.com/mobius-os/app-skills"
  )
  db.add(models.App(
    id=50, name="Skills", slug="skills",
    manifest_url=_canonical_identity_key(_SKILLS_MAIN_MANIFEST, "skills"),
  ))
  db.commit()

  install_mock = AsyncMock(return_value=_install_result())
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  urls = [c.kwargs["manifest_url"] for c in install_mock.await_args_list]
  assert BOOTSTRAP_SKILLS_MANIFEST_URL not in urls  # skills already present


@pytest.mark.asyncio
async def test_bootstrap_honors_skills_tombstone_at_other_ref(db, monkeypatch):
  """F-1: an owner uninstalled skills (a tombstone) at `main`; a commit-pinned
  bootstrap must still see it and NOT silently reinstall past the uninstall."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key

  db.add(models.App(
    id=51, name="Skills", slug="skills",
    manifest_url=_canonical_identity_key(_SKILLS_MAIN_MANIFEST, "skills"),
    deleted_at=datetime.now(timezone.utc),
  ))
  db.commit()

  install_mock = AsyncMock(return_value=_install_result())
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  urls = [c.kwargs["manifest_url"] for c in install_mock.await_args_list]
  assert BOOTSTRAP_SKILLS_MANIFEST_URL not in urls  # tombstone respected
