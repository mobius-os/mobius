"""First-boot bootstrap — ensure_bootstrap_apps_installed contract.

Validates the boot-time invariants: ordered installs, canonical manifest
identity, per-app uninstall policy, and failure isolation.
"""

import asyncio
import errno
from datetime import datetime, timezone
import re
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app import app_git, install, models
from app.bootstrap import (
  BOOTSTRAP_INTEGRATIONS_MANIFEST_URL,
  BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL,
  BOOTSTRAP_MEMORY_MANIFEST_URL,
  BOOTSTRAP_REFLECTION_MANIFEST_URL,
  BOOTSTRAP_SKILLS_MANIFEST_URL,
  BOOTSTRAP_SOCIAL_MANIFEST_URL,
  BOOTSTRAP_STORE_MANIFEST_URL,
  ensure_bootstrap_apps_installed,
)


@pytest.fixture(autouse=True)
def _no_git_transfer(monkeypatch):
  """These tests replace the installer, so nothing needs a prepared package."""
  monkeypatch.setattr("app.bootstrap.prepare_package", AsyncMock(return_value=None))


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
    BOOTSTRAP_INTEGRATIONS_MANIFEST_URL,
    BOOTSTRAP_SOCIAL_MANIFEST_URL,
  ]


def _installed_default_rows(created_at, *, deleted=()):
  """Core default rows with the canonical internal package identities."""
  from app.install import _canonical_identity_key

  specs = (
    ("store", "Store", BOOTSTRAP_STORE_MANIFEST_URL),
    ("skills", "Skills", BOOTSTRAP_SKILLS_MANIFEST_URL),
    ("memory", "Memory", BOOTSTRAP_MEMORY_MANIFEST_URL),
    ("reflection", "Reflection", BOOTSTRAP_REFLECTION_MANIFEST_URL),
    ("integrations", "Integrations", BOOTSTRAP_INTEGRATIONS_MANIFEST_URL),
    ("social", "Social", BOOTSTRAP_SOCIAL_MANIFEST_URL),
  )
  deleted_at = datetime.now(timezone.utc)
  return [
    models.App(
      source_dir=f"/tmp/mobius-tests/{manifest_id}",
      name=display_name,
      description="already here",
      jsx_source="export default function App() {}",
      slug=manifest_id,
      manifest_url=_canonical_identity_key(manifest_url, manifest_id),
      created_at=created_at,
      deleted_at=deleted_at if manifest_id in deleted else None,
    )
    for manifest_id, display_name, manifest_url in specs
  ]


@pytest.mark.asyncio
async def test_explicit_skip_needs_neither_database_nor_catalog(monkeypatch):
  monkeypatch.setenv("MOEBIUS_SKIP_BOOTSTRAP", "1")
  with patch("app.bootstrap.install_from_manifest", AsyncMock()) as install:
    await ensure_bootstrap_apps_installed(None)
  install.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_installs_all_apps_in_order_when_absent(db, monkeypatch):
  """A fresh database installs the platform's recovery and management apps."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  install_mock = AsyncMock(return_value=_install_result())

  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  assert install_mock.await_count == len(_bootstrap_urls())
  assert [
    call.kwargs["manifest_url"] for call in install_mock.await_args_list
  ] == _bootstrap_urls()
  for call in install_mock.await_args_list:
    assert call.kwargs["manifest"] is None
    assert call.kwargs["raw_base"] is None
    assert call.kwargs["source"] == "bootstrap"


def _installing_rows(db):
  """An install stand-in that persists each app like the real installer."""
  from app.install import InstallResult, _canonical_identity_key

  async def install(db_, *, manifest_url, **_kwargs):
    manifest_id = manifest_url.split("/")[-3].removeprefix("app-")
    identity = _canonical_identity_key(manifest_url, manifest_id)
    app = db.query(models.App).filter_by(manifest_url=identity).first()
    if app is None:
      app = models.App(
        source_dir=f"/tmp/mobius-tests/{manifest_id}",
        name=manifest_id, description="", jsx_source="",
        slug=manifest_id, manifest_url=identity,
      )
      db.add(app)
    app.deleted_at = None  # a reinstall revives the owner's tombstone
    db.commit()
    return InstallResult(
      app=app, mode="install", warnings=[], manifest={}, conflict_paths=[],
      divergence="none", reconciliation=app_git.ReconciliationReceipt(),
    )

  return install


@pytest.mark.asyncio
async def test_fresh_deployment_starts_with_only_the_store_pinned(
  db, monkeypatch,
):
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)

  with patch("app.bootstrap.install_from_manifest", _installing_rows(db)):
    await ensure_bootstrap_apps_installed(db)

  pinned = {
    app.slug for app in db.query(models.App).all() if app.pinned_at
  }
  assert pinned == {"store"}


@pytest.mark.asyncio
async def test_store_reinstalled_after_uninstall_keeps_owner_unpinned(
  db, monkeypatch,
):
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  db.add_all(_installed_default_rows(
    datetime.now(timezone.utc), deleted=("store",),
  ))
  db.commit()

  with patch("app.bootstrap.install_from_manifest", _installing_rows(db)):
    await ensure_bootstrap_apps_installed(db)

  assert not any(app.pinned_at for app in db.query(models.App).all())


@pytest.mark.asyncio
async def test_existing_deployment_with_purged_store_not_pinned(db, monkeypatch):
  """Finding #1: a Store row's absence is not a fresh-deployment signal.

  An existing deployment whose Store tombstone was hard-purged still has other
  apps rows. Bootstrap reinstalls the (now-absent) Store but must not re-pin it
  against the owner's earlier choice.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  # Every default row EXCEPT the Store — i.e. the Store was hard-purged, the
  # rest of the deployment remains.
  db.add_all([
    row for row in _installed_default_rows(datetime.now(timezone.utc))
    if row.slug != "store"
  ])
  db.commit()

  with patch("app.bootstrap.install_from_manifest", _installing_rows(db)):
    await ensure_bootstrap_apps_installed(db)

  # The Store was (re)installed but left unpinned; nothing is pinned.
  store = db.query(models.App).filter_by(slug="store").one()
  assert store.deleted_at is None
  assert not any(app.pinned_at for app in db.query(models.App).all())


@pytest.mark.asyncio
async def test_installer_resolved_existing_store_not_repinned(db, monkeypatch):
  """Finding #1: bootstrap's identity pre-check can miss a Store the installer
  only resolves through package metadata. On an existing deployment (an apps
  row already present) the Store must not be re-pinned even though bootstrap's
  own `existing` lookup returned nothing and it called install.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import InstallResult

  # A live Store row bootstrap's own pre-check cannot match (no canonical
  # manifest_url and no trusted git origin), standing in for identity the
  # installer resolves later via package_id.
  db.add(models.App(
    source_dir="/tmp/mobius-tests/store",
    name="Store", slug="store",
    manifest_url=None, package_id="store-package",
  ))
  db.commit()
  store_id = db.query(models.App).filter_by(slug="store").one().id

  async def install(_db, *, manifest_url, **_kwargs):
    if manifest_url == BOOTSTRAP_STORE_MANIFEST_URL:
      # The installer's deeper resolution updates the pre-existing row.
      row = db.query(models.App).filter_by(id=store_id).one()
      return InstallResult(
        app=row, mode="update", warnings=[], manifest={},
        conflict_paths=[], divergence="none",
        reconciliation=app_git.ReconciliationReceipt(),
      )
    return _install_result()

  with patch("app.bootstrap.install_from_manifest", install):
    await ensure_bootstrap_apps_installed(db)

  assert db.query(models.App).filter_by(id=store_id).one().pinned_at is None


@pytest.mark.asyncio
async def test_failed_store_pin_retries_and_pins_next_boot(db, monkeypatch):
  """Finding #2: the pin and its deployment marker commit in one transaction.

  A failed pin leaves the marker pending (not the Store unpinned forever) and
  does not abort bootstrap; the next boot retries and pins the already-installed
  Store exactly once.
  """
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)

  def boom():
    raise RuntimeError("pin transaction failed")

  # Boot 1: a genuinely fresh deployment, but the pin transaction fails. The
  # first now_naive_utc() call in bootstrap is inside the pin (a fresh
  # deployment records no marker timestamp during classification).
  with patch("app.bootstrap.install_from_manifest", _installing_rows(db)), \
       patch("app.bootstrap.now_naive_utc", boom):
    await ensure_bootstrap_apps_installed(db)

  assert db.query(models.App).count() == len(_bootstrap_urls())
  assert not any(app.pinned_at for app in db.query(models.App).all())
  marker = db.query(models.DefaultPinInitialization).one()
  assert marker.initialized_at is None  # still pending, retryable

  # Boot 2: the Store is already installed, but the pending marker drives the
  # retry that finally pins it.
  with patch("app.bootstrap.install_from_manifest", _installing_rows(db)):
    await ensure_bootstrap_apps_installed(db)

  pinned = {app.slug for app in db.query(models.App).all() if app.pinned_at}
  assert pinned == {"store"}
  assert db.query(models.DefaultPinInitialization).one().initialized_at is not None


@pytest.mark.asyncio
async def test_new_deployment_does_not_install_mobius_you(
  db, monkeypatch,
):
  """The Möbius account and models live in Settings, so no app is installed."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  install_mock = AsyncMock(return_value=_install_result())

  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  urls = [call.kwargs["manifest_url"] for call in install_mock.await_args_list]
  assert urls == _bootstrap_urls()
  assert not any("app-mobius-you" in url for url in urls)
  assert db.query(models.IdentityAccountLink).count() == 0


@pytest.mark.asyncio
async def test_bootstrap_releases_identity_transaction_before_install(
  db, monkeypatch,
):
  """Serial fetch/compile work must not retain the identity query connection."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)

  def install_after_release(*_args, **_kwargs):
    assert not db.in_transaction()
    return _install_result()

  install_mock = AsyncMock(side_effect=install_after_release)
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  assert install_mock.await_count == len(_bootstrap_urls())
  assert not db.in_transaction()


@pytest.mark.asyncio
async def test_bootstrap_applies_per_app_uninstall_policy(db, monkeypatch):
  """Store returns; other default-app uninstalls remain respected."""
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
    models.App(
      source_dir="/tmp/mobius-tests/integrations",
      name="Integrations",
      description="owner uninstalled",
      jsx_source="export default function App() {}",
      slug="integrations",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_INTEGRATIONS_MANIFEST_URL, "integrations",
      ),
      deleted_at=deleted_at,
    ),
    models.App(
      source_dir="/tmp/mobius-tests/social",
      name="Social",
      description="owner uninstalled",
      jsx_source="export default function App() {}",
      slug="social",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_SOCIAL_MANIFEST_URL, "social",
      ),
      deleted_at=deleted_at,
    ),
  ])
  db.commit()

  install_mock = AsyncMock(return_value=_install_result("Store", "store"))
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  # Only the Store (the recovery surface) returns after an owner uninstall;
  # Skills, Memory, and Integrations (policy False) stay gone;
  # live Reflection is skipped.
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
    models.App(
      source_dir="/tmp/mobius-tests/integrations-custom",
      name="Integrations",
      description="already here",
      jsx_source="export default function App() {}",
      slug="integrations-custom",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_INTEGRATIONS_MANIFEST_URL, "integrations",
      ),
    ),
    models.App(
      source_dir="/tmp/mobius-tests/social-custom",
      name="Social",
      description="already here",
      jsx_source="export default function App() {}",
      slug="social-custom",
      manifest_url=_canonical_identity_key(
        BOOTSTRAP_SOCIAL_MANIFEST_URL, "social",
      ),
    ),
  ])
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)
  install_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_migrates_live_integrations_predecessor(db, monkeypatch):
  """An active Connections install is offered the canonical Integrations app."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key

  created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
  rows = [
    row for row in _installed_default_rows(created_at)
    if row.slug != "integrations"
  ]
  rows.append(models.App(
    source_dir="/tmp/mobius-tests/connections",
    name="Integrations",
    description="predecessor package",
    jsx_source="export default function App() {}",
    slug="connections",
    manifest_url=_canonical_identity_key(
      BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL, "connections",
    ),
    created_at=created_at,
  ))
  db.add_all(rows)
  db.commit()

  install_mock = AsyncMock(
    return_value=_install_result("Integrations", "integrations", app_id=5),
  )
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  install_mock.assert_awaited_once()
  assert install_mock.await_args.kwargs["manifest_url"] == (
    BOOTSTRAP_INTEGRATIONS_MANIFEST_URL
  )


@pytest.mark.asyncio
async def test_bootstrap_preserves_uninstalled_integrations_predecessor(
  db, monkeypatch,
):
  """The checkpoint preserves an uninstall under the current identity."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key

  created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
  rows = [
    row for row in _installed_default_rows(created_at)
    if row.slug != "integrations"
  ]
  rows.append(models.App(
    source_dir="/tmp/mobius-tests/connections",
    name="Integrations",
    description="owner uninstalled predecessor",
    jsx_source="export default function App() {}",
    slug="connections",
    manifest_url=_canonical_identity_key(
      BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL, "connections",
    ),
    created_at=created_at,
    deleted_at=datetime.now(timezone.utc),
  ))
  db.add_all(rows)
  db.commit()

  install_mock = AsyncMock()
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  install_mock.assert_not_awaited()
  predecessor = db.query(models.App).filter(
    models.App.slug == "connections",
  ).one()
  assert predecessor.deleted_at is not None
  assert predecessor.manifest_url == _canonical_identity_key(
    BOOTSTRAP_INTEGRATIONS_MANIFEST_URL, "integrations",
  )






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
    _install_result("Integrations", "integrations", app_id=5),
    _install_result("Social", "social", app_id=6),
  ])
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  assert install_mock.await_count == len(_bootstrap_urls())
  assert [
    call.kwargs["manifest_url"] for call in install_mock.await_args_list
  ] == _bootstrap_urls()
  bootstrap_errors = [
    record for record in caplog.records
    if record.name == "mobius.bootstrap" and record.levelname == "ERROR"
  ]
  assert bootstrap_errors, "expected bootstrap failure to log at ERROR"




# Skills as earlier releases installed it: from a fixed commit, not `main`.
_SKILLS_COMMIT_MANIFEST = (
  "https://raw.githubusercontent.com/mobius-os/app-skills/"
  "113210883ddab380a01da1443e61600439d23b2a/mobius.json"
)


@pytest.mark.asyncio
async def test_bootstrap_recognizes_skills_row_installed_at_other_ref(
  db, monkeypatch,
):
  """F-1: bootstrap installs from `main`, but a skills row installed from an
  earlier release's fixed commit is the SAME app (identity is the repo, not the
  ref) — bootstrap must recognize it and never reinstall a duplicate."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key, _trusted_catalog_origin_url

  assert _trusted_catalog_origin_url(BOOTSTRAP_SKILLS_MANIFEST_URL) == (
    _trusted_catalog_origin_url(_SKILLS_COMMIT_MANIFEST)
  ) == "https://github.com/mobius-os/app-skills.git"
  db.add(models.App(
    source_dir="/tmp/mobius-tests/skills",
    id=50, name="Skills", slug="skills",
    manifest_url=_canonical_identity_key(_SKILLS_COMMIT_MANIFEST, "skills"),
  ))
  db.commit()

  install_mock = AsyncMock(return_value=_install_result())
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  urls = [c.kwargs["manifest_url"] for c in install_mock.await_args_list]
  assert BOOTSTRAP_SKILLS_MANIFEST_URL not in urls  # skills already present


@pytest.mark.asyncio
async def test_bootstrap_honors_skills_tombstone_at_other_ref(db, monkeypatch):
  """F-1: an owner uninstalled skills (a tombstone) installed from a fixed
  commit; bootstrap from `main` must still see it and NOT silently reinstall
  past the uninstall."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  from app.install import _canonical_identity_key

  db.add(models.App(
    source_dir="/tmp/mobius-tests/skills",
    id=51, name="Skills", slug="skills",
    manifest_url=_canonical_identity_key(_SKILLS_COMMIT_MANIFEST, "skills"),
    deleted_at=datetime.now(timezone.utc),
  ))
  db.commit()

  install_mock = AsyncMock(return_value=_install_result())
  with patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  urls = [c.kwargs["manifest_url"] for c in install_mock.await_args_list]
  assert BOOTSTRAP_SKILLS_MANIFEST_URL not in urls  # tombstone respected


@pytest.mark.asyncio
async def test_bootstrap_honors_legacy_trusted_origin_tombstone(
  db, monkeypatch,
):
  """A pre-identity catalog row remains uninstalled after owner deletion."""
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  db.add(models.App(
    source_dir="/tmp/mobius-tests/legacy-memory",
    id=52,
    name="Memory",
    slug="memory",
    manifest_url=None,
    deleted_at=datetime.now(timezone.utc),
  ))
  db.commit()

  install_mock = AsyncMock(return_value=_install_result())
  with patch(
    "app.app_git.origin_url",
    return_value="https://github.com/mobius-os/app-memory.git",
  ), patch("app.bootstrap.install_from_manifest", install_mock):
    await ensure_bootstrap_apps_installed(db)

  urls = [c.kwargs["manifest_url"] for c in install_mock.await_args_list]
  assert BOOTSTRAP_MEMORY_MANIFEST_URL not in urls


@pytest.mark.asyncio
async def test_undecidable_default_pins_never_stop_bootstrap(db, monkeypatch):
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)

  with patch(
    "app.bootstrap._default_store_pin_pending",
    side_effect=RuntimeError("marker unavailable"),
  ), patch("app.bootstrap.install_from_manifest", _installing_rows(db)):
    await ensure_bootstrap_apps_installed(db)

  apps = db.query(models.App).all()
  assert len(apps) == len(_bootstrap_urls())
  assert not any(app.pinned_at for app in apps)


def _store_failing_once(db, exc):
  """Install stand-in whose first Store attempt fails with `exc`."""
  install = _installing_rows(db)
  failures = [exc]

  async def flaky(db_, *, manifest_url, **kwargs):
    if manifest_url == BOOTSTRAP_STORE_MANIFEST_URL and failures:
      raise failures.pop()
    return await install(db_, manifest_url=manifest_url, **kwargs)

  return flaky


@pytest.mark.asyncio
async def test_rate_limited_store_is_retried_and_takes_its_default_pin(
  db, monkeypatch,
):
  """A GitHub rate limit on first boot is a wait, not a stranded instance."""
  from app import bootstrap as bootstrap_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  rate_limited = HTTPException(
    429, "GitHub rate-limited this app request.",
    headers={"Retry-After": "600"},
  )
  delays = []

  async def no_wait(seconds):
    delays.append(seconds)

  monkeypatch.setattr(bootstrap_mod.asyncio, "sleep", no_wait)
  broadcast = MagicMock()
  with patch(
    "app.bootstrap.install_from_manifest", _store_failing_once(db, rate_limited),
  ), patch("app.bootstrap.get_system_broadcast", return_value=broadcast):
    outcome = await ensure_bootstrap_apps_installed(db)
    assert outcome == bootstrap_mod.BootstrapOutcome(
      ("store",), rate_limited=True, retry_after=600,
    )
    assert db.query(models.App).filter_by(slug="store").first() is None
    broadcast.reset_mock()

    await bootstrap_mod.retry_bootstrap_apps(outcome)

  # The upstream hint lengthened the first one-minute step.
  assert delays == [600]
  db.expire_all()
  store = db.query(models.App).filter_by(slug="store").one()
  pinned = {app.slug for app in db.query(models.App).all() if app.pinned_at}
  assert pinned == {"store"}
  # The Shell hears about the Store the retry installed.
  assert {"type": "app_updated", "appId": str(store.id)} in [
    call.args[0] for call in broadcast.publish.call_args_list
  ]
  # One quick successful retry never bothers the owner.
  assert db.query(models.Notification).count() == 0


def _unresolvable_host(errno_=socket.EAI_AGAIN):
  exc = HTTPException(400, "Cannot resolve host 'api.github.com'")
  exc.__cause__ = socket.gaierror(errno_, "name resolution failed")
  return exc


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
  HTTPException(422, "App failed to compile."),
  HTTPException(404, "Not found"),
  install.git_source_error("Nothing changed.", RuntimeError("bad origin")),
  # A full disk fails the same way on every retry.
  install.git_source_error(
    "Nothing was installed.", OSError(errno.ENOSPC, "No space left on device"),
    code="git_install_unavailable",
  ),
  # A missing repository or branch, or a name that does not exist, stays so.
  install.git_source_error(
    "Nothing was installed.",
    app_git.GitTransferFailed("repository not found", transient=False),
    code="git_install_unavailable",
  ),
  app_git.GitTransferFailed("couldn't find remote ref", transient=False),
  socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
  _unresolvable_host(socket.EAI_NONAME),
  ValueError("unexpected"),
])
async def test_non_transient_failure_schedules_no_retry(db, monkeypatch, exc):
  from app import bootstrap as bootstrap_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  with patch(
    "app.bootstrap.install_from_manifest", _store_failing_once(db, exc),
  ):
    outcome = await ensure_bootstrap_apps_installed(db)

  assert outcome == bootstrap_mod.BootstrapOutcome()


@pytest.mark.parametrize("exc", [
  HTTPException(502, "Upstream 500"),
  HTTPException(503, "Unavailable"),
  HTTPException(504, "Timeout"),
  install.git_source_error("Nothing changed.", app_git.GitTransferTimeout("slow")),
  install.git_source_error(
    "Nothing was installed.", app_git.GitTransferFailed("offline"),
    code="git_install_unavailable",
  ),
  install.git_source_error(
    "Nothing changed.", app_git.HistoryIncomplete("merge base hidden"),
  ),
  HTTPException(409, {"code": "git_source_mismatch", "retryable": True}),
  app_git.GitTransferFailed("could not resolve host"),
  app_git.GitTransferTimeout("clone exceeded its ceiling"),
  app_git.HistoryIncomplete("merge base hidden"),
  socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
  _unresolvable_host(),
])
def test_gateway_and_transfer_timeouts_are_transient(exc):
  assert install.transient_failure(exc) == (True, None)


def test_rate_limit_is_transient_with_its_upstream_wait():
  exc = HTTPException(429, "Rate limited", headers={"Retry-After": "120"})
  assert install.transient_failure(exc) == (True, 120)


@pytest.mark.asyncio
async def test_retry_tells_the_owner_once_and_stops_after_a_day(
  db, monkeypatch,
):
  from app import bootstrap as bootstrap_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  db.add(models.Owner(username="owner", hashed_password="unused"))
  db.commit()
  clock = [0.0]

  async def advance(seconds):
    clock[0] += seconds

  monkeypatch.setattr(bootstrap_mod.asyncio, "sleep", advance)
  monkeypatch.setattr(bootstrap_mod.time, "monotonic", lambda: clock[0])
  install = _installing_rows(db)

  async def store_and_social_limited(db_, *, manifest_url, **kwargs):
    if manifest_url in (
      BOOTSTRAP_STORE_MANIFEST_URL, BOOTSTRAP_SOCIAL_MANIFEST_URL,
    ):
      raise HTTPException(429, "rate limited")
    return await install(db_, manifest_url=manifest_url, **kwargs)

  with patch("app.bootstrap.install_from_manifest", store_and_social_limited):
    await bootstrap_mod.retry_bootstrap_apps(bootstrap_mod.BootstrapOutcome(
      ("store", "social"), rate_limited=True,
    ))

  assert clock[0] <= bootstrap_mod._RETRY_WINDOW_SECS
  assert clock[0] > bootstrap_mod._RETRY_WINDOW_SECS - 2 * 60 * 60
  notes = db.query(models.Notification).all()
  assert [note.body for note in notes] == [
    "App Store and Social couldn't be installed yet (GitHub rate limit); "
    "retrying automatically.",
  ]


@pytest.mark.asyncio
async def test_retry_waits_for_owner_lifecycle_work_and_respects_its_uninstall(
  db, monkeypatch,
):
  """The serving-time retry takes the install/uninstall lock per app.

  An owner installs and then uninstalls Social while the retry is due; the
  retry must wait for that work and then honor the owner's tombstone instead
  of reinstalling from an identity check made before it.
  """
  from app import bootstrap as bootstrap_mod
  from app import fs_locks
  from app.install import _canonical_identity_key

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  real_sleep = asyncio.sleep

  async def no_wait(_seconds):
    await real_sleep(0)

  monkeypatch.setattr(bootstrap_mod.asyncio, "sleep", no_wait)
  install = AsyncMock(side_effect=_installing_rows(db))
  lock = fs_locks.install_uninstall_lock()
  with patch("app.bootstrap.install_from_manifest", install):
    await lock.acquire()
    try:
      retry = asyncio.create_task(bootstrap_mod.retry_bootstrap_apps(
        bootstrap_mod.BootstrapOutcome(
          ("store", "social"), rate_limited=True,
        ),
      ))
      for _ in range(20):
        await real_sleep(0)
      assert not retry.done()
      install.assert_not_awaited()
      db.add(models.App(
        source_dir="/tmp/mobius-tests/social",
        name="Social", description="", jsx_source="", slug="social",
        manifest_url=_canonical_identity_key(
          BOOTSTRAP_SOCIAL_MANIFEST_URL, "social",
        ),
        deleted_at=datetime.now(timezone.utc),
      ))
      db.commit()
    finally:
      lock.release()
    await asyncio.wait_for(retry, timeout=10)

  urls = [call.kwargs["manifest_url"] for call in install.await_args_list]
  assert BOOTSTRAP_STORE_MANIFEST_URL in urls
  assert BOOTSTRAP_SOCIAL_MANIFEST_URL not in urls
  assert not lock.locked()


@pytest.mark.asyncio
async def test_retry_stages_each_clone_before_taking_the_lifecycle_lock(
  db, monkeypatch,
):
  """No network runs under the lock, and a staging timeout stays transient."""
  from app import app_git, fs_locks
  from app import bootstrap as bootstrap_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  locked = []

  async def prepare(url):
    locked.append(fs_locks.install_uninstall_lock().locked())
    if url == BOOTSTRAP_SOCIAL_MANIFEST_URL:
      raise app_git.GitTransferTimeout("it ran longer than 600 seconds")
    return None

  monkeypatch.setattr(bootstrap_mod, "prepare_package", prepare)
  install = AsyncMock(side_effect=_installing_rows(db))
  with patch("app.bootstrap.install_from_manifest", install):
    outcome = await bootstrap_mod.ensure_bootstrap_apps_installed(
      db, retry=("store", "social"),
    )

  assert locked == [False, False]
  assert outcome.pending == ("social",)
  assert [call.kwargs["manifest_url"] for call in install.await_args_list] == [
    BOOTSTRAP_STORE_MANIFEST_URL,
  ]


@pytest.mark.asyncio
async def test_unreachable_git_host_at_first_boot_leaves_apps_pending(
  db, monkeypatch,
):
  """Staging the clone is the first network step; a resolver that could not
  answer is retried, while a host that does not exist is not."""
  from app import bootstrap as bootstrap_mod
  from app import install as install_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  monkeypatch.setattr(bootstrap_mod, "prepare_package", install_mod.prepare_package)
  monkeypatch.setattr(install_mod, "_derive_repo_ref", lambda _url: (
    "https://unresolvable.invalid/app.git", "main",
  ))
  for errno_, pending in (
    (socket.EAI_AGAIN, tuple(
      app.manifest_id for app in bootstrap_mod._CORE_BOOTSTRAP_APPS
    )),
    (socket.EAI_NONAME, ()),
  ):
    def resolve(*_args, errno_=errno_):
      raise socket.gaierror(errno_, "name resolution failed")

    monkeypatch.setattr(app_git.socket, "getaddrinfo", resolve)
    install = AsyncMock()
    with patch("app.bootstrap.install_from_manifest", install):
      outcome = await bootstrap_mod.ensure_bootstrap_apps_installed(db)

    install.assert_not_awaited()
    assert outcome.pending == pending


@pytest.mark.asyncio
async def test_runtime_supervisors_own_the_retry_until_shutdown(monkeypatch):
  import logging

  from app import bootstrap as bootstrap_mod
  from app.runtime_supervisors import RuntimeSupervisors

  started = asyncio.Event()

  async def retry_forever(_outcome):
    started.set()
    await asyncio.Event().wait()

  monkeypatch.setattr(bootstrap_mod, "retry_bootstrap_apps", retry_forever)
  supervisors = RuntimeSupervisors(
    settings=MagicMock(data_dir="/tmp"),
    logger=logging.getLogger("test"),
    restart_authorization=None,
    restart_fallback_chats=[],
  )
  supervisors.retry_bootstrap_apps(bootstrap_mod.BootstrapOutcome(("social",)))
  await asyncio.wait_for(started.wait(), timeout=5)
  task = supervisors._tasks["bootstrap-apps-retry"]

  await supervisors.stop()

  assert task.cancelled()
  assert supervisors._tasks == {}


@pytest.mark.asyncio
async def test_retry_installs_only_pending_apps_and_respects_a_store_uninstall(
  db, monkeypatch,
):
  """Startup reinstalls an uninstalled Store; a mid-session retry must not."""
  from app import bootstrap as bootstrap_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  db.add_all([
    row for row in _installed_default_rows(
      datetime.now(timezone.utc), deleted=("store",),
    )
    if row.slug != "social"
  ])
  db.commit()

  async def no_wait(_seconds):
    pass

  monkeypatch.setattr(bootstrap_mod.asyncio, "sleep", no_wait)
  install = AsyncMock(side_effect=_installing_rows(db))
  with patch("app.bootstrap.install_from_manifest", install):
    await bootstrap_mod.retry_bootstrap_apps(
      bootstrap_mod.BootstrapOutcome(("social",), rate_limited=True),
    )

  assert [call.kwargs["manifest_url"] for call in install.await_args_list] == [
    BOOTSTRAP_SOCIAL_MANIFEST_URL,
  ]
  db.expire_all()
  assert db.query(models.App).filter_by(slug="store").one().deleted_at
  assert db.query(models.App).filter_by(slug="social").one().deleted_at is None
