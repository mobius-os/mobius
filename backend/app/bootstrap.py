"""First-boot bootstrap for the App Store, Memory, and Reflection apps.

Called from the FastAPI lifespan handler once the server is up and the
DB is migrated. Calls `install_from_manifest()` directly (in-process)
rather than HTTPing the install route — the server isn't necessarily
ready to accept connections from itself at lifespan-startup time, and
an in-process call skips the auth + rate-limit layers that exist for
external callers we don't need to traverse here.

Failure is non-fatal and isolated per app: a network blip fetching one
manifest must not crash uvicorn or prevent the remaining bootstrap apps
from installing. We log each failure and continue.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import legacy_platform_apps, models
from app.config import get_settings
from app.install import _is_historical_platform_app_source_dir, install_from_manifest

log = logging.getLogger("mobius.bootstrap")

# Pinned to `main` for v1 — the app-store repo doesn't have tagged
# releases yet. TODO: switch to a tagged release URL once we cut one,
# so a fresh container doesn't pick up an in-flight store commit.
BOOTSTRAP_STORE_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-store/main/mobius.json"
)

# The Skills app (browse/install ecosystem skills + the skill-agent chat).
# Canonical home is this catalog repo; the v2 app (compat badges, catalog
# browser) depends on this platform's skills API, so its repo merges after
# the platform does. Bootstrap is failure-tolerant, so a manifest/API skew
# just logs and retries next boot.
BOOTSTRAP_SKILLS_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-skills/main/mobius.json"
)

LEGACY_PLATFORM_APP_MANIFEST_URLS = legacy_platform_apps.MANIFEST_URLS


@dataclass(frozen=True)
class _BootstrapApp:
  name: str
  manifest_url: str
  reinstall_after_uninstall: bool


_BOOTSTRAP_APPS = (
  _BootstrapApp("store", BOOTSTRAP_STORE_MANIFEST_URL, True),
  _BootstrapApp("skills", BOOTSTRAP_SKILLS_MANIFEST_URL, True),
  _BootstrapApp("memory", LEGACY_PLATFORM_APP_MANIFEST_URLS["memory"], False),
  _BootstrapApp(
    "reflection", LEGACY_PLATFORM_APP_MANIFEST_URLS["reflection"], False,
  ),
)

# Tests set MOEBIUS_SKIP_BOOTSTRAP=1 so the pytest suite doesn't hit
# the live GitHub URL. Set in docker-compose.test.yml's `pytest`
# service environment block.
_SKIP_ENV = "MOEBIUS_SKIP_BOOTSTRAP"


def _is_legacy_platform_row(app: models.App) -> bool:
  """True for active old rows whose source matches retired baked-app shapes.

  The historical /data/apps/<slug> shape additionally requires an empty
  manifest_url: once a migration stamps the canonical identity the row is on the
  catalog model and must NOT be re-migrated on the next boot. Re-migrating
  re-fetched the catalog from GitHub every restart and, when upstream had
  advanced with no local edits, silently fast-forwarded the app past owner
  review. The platform-core shape (`is_legacy_source_dir`) needs no such gate —
  a migrated row moves out of /data/platform/core-apps, so it stops matching on
  the source path alone.
  """
  data_dir = get_settings().data_dir
  return legacy_platform_apps.is_legacy_source_dir(
    app.source_dir, data_dir, app.slug,
  ) or _is_historical_platform_app_source_dir(
    app.source_dir, app.manifest_url, data_dir, app.slug,
  )


async def _migrate_legacy_platform_apps(db: Session) -> None:
  """Move old built-in rows forward by installing their trusted catalog entry.

  This only runs when an active row from an older image is already present and
  still points at the retired platform-core source tree.
  """
  rows = (
    db.query(models.App)
    .filter(
      models.App.deleted_at.is_(None),
      models.App.slug.in_(tuple(LEGACY_PLATFORM_APP_MANIFEST_URLS)),
    )
    .all()
  )
  for row in rows:
    if not _is_legacy_platform_row(row):
      continue
    manifest_url = LEGACY_PLATFORM_APP_MANIFEST_URLS[row.slug]
    log.info(
      "bootstrap: migrating legacy platform app %s from %s",
      row.slug, row.source_dir,
    )
    try:
      await install_from_manifest(
        db,
        manifest_url=manifest_url,
        manifest=None,
        raw_base=None,
        source="bootstrap",
      )
    except Exception as exc:
      log.exception(
        "bootstrap: legacy platform app migration failed for %s — %s",
        row.slug, exc,
      )


async def ensure_bootstrap_apps_installed(db: Session) -> None:
  """Idempotently install the configured bootstrap apps when absent.

  Identity is keyed on `manifest_url`, not slug. This means:
    1. The bootstrapped store doesn't always end up with slug='store'
       — if the user already built an app called "store", first-boot
       slug-assignment hands it a fallback like 'app-store'. A slug
       check would then mis-treat the bootstrapped store as absent
       and try to install it again every boot.
    2. Every bootstrap app retains its canonical identity even if its
       assigned slug differs from the catalog slug.

  Caller is the FastAPI lifespan/startup handler. Owns no transaction
  state — `install_from_manifest` commits its own work on success and
  rolls back on failure. We just decide whether to call it.
  """
  if os.environ.get(_SKIP_ENV) == "1":
    log.info("bootstrap: %s=1, skipping bootstrap app installs", _SKIP_ENV)
    return

  await _migrate_legacy_platform_apps(db)

  # Manifest installs store the canonical identity key
  # (`<base>#manifest-id=<id>`, with a trailing `/mobius.json` stripped from the
  # base), not the bare URL.
  from app.install import _canonical_base

  for bootstrap_app in _BOOTSTRAP_APPS:
    query = db.query(models.App).filter(models.App.manifest_url.like(
      _canonical_base(bootstrap_app.manifest_url) + "#manifest-id=%"
    ))
    if bootstrap_app.reinstall_after_uninstall:
      # The store is the recovery surface, so it must return after uninstall;
      # owner uninstalls of the other bootstrap apps are respected.
      query = query.filter(models.App.deleted_at.is_(None))
    existing = query.first()
    if existing is not None:
      log.info(
        "bootstrap: %s already installed (app id=%s)",
        bootstrap_app.name, existing.id,
      )
      continue
    log.info(
      "bootstrap: installing %s from %s",
      bootstrap_app.name, bootstrap_app.manifest_url,
    )
    try:
      app, mode, warnings, _manifest, _conflicts, _divergence = (
        await install_from_manifest(
          db,
          manifest_url=bootstrap_app.manifest_url,
          manifest=None,
          raw_base=None,
          source="bootstrap",
        )
      )
    except Exception as exc:
      # Catch-all on purpose: no manifest failure should crash lifespan or
      # prevent the remaining bootstrap apps from installing.
      log.exception(
        "bootstrap: %s install failed — %s", bootstrap_app.name, exc,
      )
      continue
    log.info(
      "bootstrap: %s install %s (app id=%s, warnings=%s)",
      bootstrap_app.name, mode, app.id, warnings,
    )
