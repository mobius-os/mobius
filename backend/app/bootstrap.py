"""First-boot bootstrap that auto-installs the curated app-store mini-app.

Called from the FastAPI lifespan handler once the server is up and the
DB is migrated. Calls `install_from_manifest()` directly (in-process)
rather than HTTPing the install route — the server isn't necessarily
ready to accept connections from itself at lifespan-startup time, and
an in-process call skips the auth + rate-limit layers that exist for
external callers we don't need to traverse here.

Failure is non-fatal: a network blip fetching the store manifest must
not crash uvicorn (otherwise the whole platform is unreachable on the
first boot after a deploy that lands during a GitHub outage). We log
the failure and return; the owner can install the store manually.
"""

from __future__ import annotations

import logging
import os

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

LEGACY_PLATFORM_APP_MANIFEST_URLS = legacy_platform_apps.MANIFEST_URLS

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

  New instances do not auto-install these apps. This only runs when an active
  row from an older image is already present and still points at the retired
  platform-core source tree.
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


async def ensure_store_installed(db: Session) -> None:
  """Idempotent: if no App installed from BOOTSTRAP_STORE_MANIFEST_URL
  exists, install it.

  Identity is keyed on `manifest_url`, not slug. Two reasons:
    1. The bootstrapped store doesn't always end up with slug='store'
       — if the user already built an app called "store", first-boot
       slug-assignment hands it a fallback like 'app-store'. A slug
       check would then mis-treat the bootstrapped store as absent
       and try to install it again every boot.
    2. If the user uninstalls the bootstrapped store (DELETE
       /api/apps/<id>), the next container restart should reinstall
       it. Keying on manifest_url makes the re-install fire correctly
       regardless of what slug the deleted row had.

  Caller is the FastAPI lifespan/startup handler. Owns no transaction
  state — `install_from_manifest` commits its own work on success and
  rolls back on failure. We just decide whether to call it.
  """
  if os.environ.get(_SKIP_ENV) == "1":
    log.info("bootstrap: %s=1, skipping store install", _SKIP_ENV)
    return

  await _migrate_legacy_platform_apps(db)

  # Installs store the CANONICAL identity key (`<base>#manifest-id=<id>`, with a
  # trailing `/mobius.json` stripped from the base), NOT the bare URL — so match
  # on that canonical prefix, else this lookup misses every restart and
  # needlessly re-fetches + updates the store (Codex review round-10 #8,
  # round-11 #1).
  from app.install import _canonical_base
  existing = (
    db.query(models.App)
    .filter(models.App.manifest_url.like(
      _canonical_base(BOOTSTRAP_STORE_MANIFEST_URL) + "#manifest-id=%"
    ))
    # A tombstoned (soft-deleted) store reads as ABSENT here so this boot
    # reinstalls it — install_from_manifest reattaches the same row and clears
    # deleted_at, reviving it with its data. Without this filter an uninstalled
    # store would never return on restart, contradicting reason (2) above and
    # leaving the owner with no UI surface to get it back (feature 110).
    .filter(models.App.deleted_at.is_(None))
    .first()
  )
  if existing is not None:
    log.info("bootstrap: store already installed (app id=%s)", existing.id)
    return
  log.info("bootstrap: installing store from %s", BOOTSTRAP_STORE_MANIFEST_URL)
  try:
    app, mode, warnings, _manifest, _conflicts, _divergence = (
      await install_from_manifest(
        db,
        manifest_url=BOOTSTRAP_STORE_MANIFEST_URL,
        manifest=None,
        raw_base=None,
        source="bootstrap",
      )
    )
  except Exception as exc:
    # Catch-all on purpose: HTTPException, network errors, JSON parse
    # errors, anything else. The cost of letting the bootstrap crash
    # lifespan is higher than the cost of an uninstalled store —
    # without uvicorn there's no recovery surface to install it from.
    log.exception("bootstrap: store install failed — %s", exc)
    return
  log.info(
    "bootstrap: store install %s (app id=%s, warnings=%s)",
    mode, app.id, warnings,
  )
