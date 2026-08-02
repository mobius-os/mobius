"""First-boot bootstrap — ensure_bootstrap_apps_installed contract.

Validates the boot-time invariants: ordered installs, canonical manifest
identity, per-app uninstall policy, and failure isolation.
"""

from datetime import datetime, timezone
import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import app_git, models
from app.bootstrap import (
  BOOTSTRAP_MEMORY_MANIFEST_URL,
  BOOTSTRAP_REFLECTION_MANIFEST_URL,
  BOOTSTRAP_SKILLS_MANIFEST_URL,
  BOOTSTRAP_STORE_MANIFEST_URL,
  ensure_bootstrap_apps_installed,
)




def _install_result(name="App", slug="app", app_id=1, mode="install"):
  from app.install import InstallResult

  app = models.App(
    source_dir="/tmp/mobius-tests/test-bootstrap-29",
    id=app_id, name=name, slug=slug,
  )
  return InstallResult(
    app=app,
    mode=mode,
    warnings=[],
    manifest={},
    conflict_paths=[],
    divergence="none",
    reconciliation=app_git.ReconciliationReceipt(),
  )


def _bootstrap_urls():
  return [
    BOOTSTRAP_STORE_MANIFEST_URL,
    BOOTSTRAP_SKILLS_MANIFEST_URL,
    BOOTSTRAP_MEMORY_MANIFEST_URL,
    BOOTSTRAP_REFLECTION_MANIFEST_URL,
  ]


def test_recovery_store_bootstrap_is_pinned_to_an_immutable_commit():
  """A fresh boot cannot silently install a newly moved Store branch tip."""
  assert re.search(
    r"/mobius-os/app-store/[0-9a-f]{40}/mobius\.json$",
    BOOTSTRAP_STORE_MANIFEST_URL,
  )


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
      source_dir="/tmp/mobius-tests/store",
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
      source_dir="/tmp/mobius-tests/skills",
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
      source_dir="/tmp/mobius-tests/memory",
      name="Memory",
      description="owner uninstalled",
      jsx_source="export default function App() {}",
      slug="memory",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_MEMORY_MANIFEST_URL, "memory",
      ),
      deleted_at=deleted_at,
    ),
    models.App(
      source_dir="/tmp/mobius-tests/reflection",
      name="Reflection",
      description="already here",
      jsx_source="export default function App() {}",
      slug="reflection",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_REFLECTION_MANIFEST_URL, "reflection",
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
      source_dir="/tmp/mobius-tests/app-store",
      name="Store",
      description="already here",
      jsx_source="export default function App() {}",
      slug="app-store",
      manifest_url=_canonical_identity_key(BOOTSTRAP_STORE_MANIFEST_URL, "store"),
    ),
    models.App(
      source_dir="/tmp/mobius-tests/skills-custom",
      name="Skills",
      description="already here",
      jsx_source="export default function App() {}",
      slug="skills-custom",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_SKILLS_MANIFEST_URL, "skills",
      ),
    ),
    models.App(
      source_dir="/tmp/mobius-tests/memory-custom",
      name="Memory",
      description="already here",
      jsx_source="export default function App() {}",
      slug="memory-custom",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_MEMORY_MANIFEST_URL, "memory",
      ),
    ),
    models.App(
      source_dir="/tmp/mobius-tests/reflection-custom",
      name="Reflection",
      description="already here",
      jsx_source="export default function App() {}",
      slug="reflection-custom",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_REFLECTION_MANIFEST_URL, "reflection",
      ),
    ),
  ])
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)
  install_mock.assert_not_awaited()






@pytest.mark.asyncio
async def test_bootstrap_ignores_unrelated_store_slug(db, monkeypatch):
  """A user-built app named store does not satisfy canonical app identity."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  db.add(models.App(
    source_dir="/tmp/mobius-tests/store",
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
    source_dir="/tmp/mobius-tests/skills",
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
    source_dir="/tmp/mobius-tests/skills",
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
