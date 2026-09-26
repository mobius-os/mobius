"""First-boot bootstrap for the default apps.

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

from app import models
from app.install import install_from_manifest
from app.timeutil import now_naive_utc

log = logging.getLogger("mobius.bootstrap")

# Every default app installs from its catalog repository's `main`, so a new
# deployment starts on the latest version of each app; later versions arrive
# through the Store's ordinary update flow. Nothing here freezes a revision:
# `tests/test_bootstrap_live.py` (its own CI step) installs this whole set on an
# empty instance so a `main` that no longer installs on this platform fails CI
# instead of silently leaving new deployments without the app.
_MANIFEST_URL = "https://raw.githubusercontent.com/mobius-os/{repo}/main/mobius.json"

BOOTSTRAP_STORE_MANIFEST_URL = _MANIFEST_URL.format(repo="app-store")
BOOTSTRAP_SKILLS_MANIFEST_URL = _MANIFEST_URL.format(repo="app-skills")
BOOTSTRAP_MEMORY_MANIFEST_URL = _MANIFEST_URL.format(repo="app-memory")
BOOTSTRAP_REFLECTION_MANIFEST_URL = _MANIFEST_URL.format(repo="app-reflection")
BOOTSTRAP_INTEGRATIONS_MANIFEST_URL = _MANIFEST_URL.format(
  repo="app-integrations",
)
BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL = _MANIFEST_URL.format(
  repo="app-connections",
)
BOOTSTRAP_IDENTITY_MANIFEST_URL = _MANIFEST_URL.format(repo="app-mobius-you")
BOOTSTRAP_SOCIAL_MANIFEST_URL = _MANIFEST_URL.format(repo="app-social")


@dataclass(frozen=True)
class _BootstrapApp:
  manifest_id: str
  manifest_url: str
  reinstall_after_uninstall: bool
  predecessor_manifest_id: str | None = None
  predecessor_manifest_url: str | None = None
  # Pinned in the drawer only when this deployment has never had the app, so
  # an owner's later unpin (or uninstall and reinstall) is never overridden.
  pin_on_first_install: bool = False


_CORE_BOOTSTRAP_APPS = (
  # The Store is how a new owner finds everything else, so it starts pinned.
  _BootstrapApp(
    "store", BOOTSTRAP_STORE_MANIFEST_URL, True, pin_on_first_install=True,
  ),
  # Skills is NOT the recovery surface — an owner uninstall is respected, and
  # the Store remains the way back.
  _BootstrapApp("skills", BOOTSTRAP_SKILLS_MANIFEST_URL, False),
  _BootstrapApp("memory", BOOTSTRAP_MEMORY_MANIFEST_URL, False),
  _BootstrapApp(
    "reflection", BOOTSTRAP_REFLECTION_MANIFEST_URL, False,
  ),
  # Integrations is the only management surface for owner MCP integrations
  # since the Settings section moved into the app. Its explicit predecessor
  # lets active installs migrate while an owner uninstall remains respected.
  _BootstrapApp(
    "integrations", BOOTSTRAP_INTEGRATIONS_MANIFEST_URL, False,
    predecessor_manifest_id="connections",
    predecessor_manifest_url=(
      BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL
    ),
  ),
  # Möbius · You is useful on every new deployment. Managed owners arrive
  # signed in; local owners see the same app with account linking optional.
  _BootstrapApp(
    "identity", BOOTSTRAP_IDENTITY_MANIFEST_URL, False,
  ),
  # Social (federated community board + direct messages) ships on every
  # deployment. Installing it does NOT join the public community: browsing is
  # open, and the owner explicitly opts in to join before they can post.
  _BootstrapApp(
    "social", BOOTSTRAP_SOCIAL_MANIFEST_URL, False,
  ),
)

# Tests set MOEBIUS_SKIP_BOOTSTRAP=1 so the pytest suite doesn't hit
# the live GitHub URL. Set in docker-compose.test.yml's `pytest`
# service environment block.
_SKIP_ENV = "MOEBIUS_SKIP_BOOTSTRAP"

# Fixed sentinel key for the single DefaultPinInitialization row.
_DEFAULT_PINS_MARKER_ID = "default_pins"


def _default_store_pin_pending(db: Session) -> bool:
  """Return whether this deployment still owes its default Store pin.

  Decided once and persisted: an owner or any app row (tombstones included)
  before bootstrap means an existing deployment, which is never pinned. A
  missing Store row alone proves nothing (purged tombstones, identities the
  installer resolves late, deployments that never installed the Store).
  """
  marker = db.query(models.DefaultPinInitialization).filter(
    models.DefaultPinInitialization.id == _DEFAULT_PINS_MARKER_ID,
  ).first()
  if marker is not None:
    pending = marker.initialized_at is None
    db.rollback()
    return pending
  existing_deployment = (
    db.query(models.Owner.id).first() is not None
    or db.query(models.App.id).first() is not None
  )
  db.add(models.DefaultPinInitialization(
    id=_DEFAULT_PINS_MARKER_ID,
    initialized_at=now_naive_utc() if existing_deployment else None,
  ))
  db.commit()
  return not existing_deployment


def _mark_default_store_pinned(db: Session, app_id: int) -> bool:
  """Pin the Store and settle the marker in one transaction.

  A failure rolls back and leaves the marker pending for the next boot.
  """
  try:
    now = now_naive_utc()
    db.query(models.App).filter(
      models.App.id == app_id, models.App.pinned_at.is_(None),
    ).update({models.App.pinned_at: now}, synchronize_session=False)
    db.query(models.DefaultPinInitialization).filter(
      models.DefaultPinInitialization.id == _DEFAULT_PINS_MARKER_ID,
      models.DefaultPinInitialization.initialized_at.is_(None),
    ).update(
      {models.DefaultPinInitialization.initialized_at: now},
      synchronize_session=False,
    )
    db.commit()
    return True
  except Exception:
    db.rollback()
    log.exception(
      "bootstrap: default Store pin failed; leaving it pending for next boot",
    )
    return False


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

  # Bootstrap uses the same resolver as preview and install so all three paths
  # agree on persisted identities, moved refs, and proven legacy origins.
  from app.install import _canonical_identity_key, _find_install_identity_row

  # Decide before installing anything, since installs create app rows.
  try:
    default_pin_pending = _default_store_pin_pending(db)
  except Exception:
    db.rollback()
    log.exception("bootstrap: could not decide default pins; pinning nothing")
    default_pin_pending = False

  for bootstrap_app in _CORE_BOOTSTRAP_APPS:
    # Use the installer's identity resolver here too: bootstrap and an explicit
    # Store action must agree across ref moves and legacy rows whose matching
    # catalog origin predates persisted manifest identity.
    existing = _find_install_identity_row(
      db,
      source_url=bootstrap_app.manifest_url,
      manifest_id=bootstrap_app.manifest_id,
    )
    predecessor = None
    if (
      existing is None
      and bootstrap_app.predecessor_manifest_id
      and bootstrap_app.predecessor_manifest_url
    ):
      predecessor = _find_install_identity_row(
        db,
        source_url=bootstrap_app.predecessor_manifest_url,
        manifest_id=bootstrap_app.predecessor_manifest_id,
      )
    if predecessor is not None and predecessor.deleted_at is not None:
      # Preserve an owner's uninstall after this one-release predecessor
      # checkpoint is deleted. The tombstone keeps its source and storage, but
      # its durable package identity becomes current; a later explicit Store
      # install can therefore revive/update the same row without boot needing
      # to remember the old package forever.
      predecessor.manifest_url = _canonical_identity_key(
        bootstrap_app.manifest_url, bootstrap_app.manifest_id,
      )
      db.commit()
      existing = predecessor
    existing_id = existing.id if existing is not None else None
    already_installed = (
      existing is not None
      and (
        existing.deleted_at is None
        or not bootstrap_app.reinstall_after_uninstall
      )
    )
    # The identity query autobegins a read transaction. Do not retain its
    # connection while the installer performs serial network fetches and
    # compilation; install_from_manifest starts and owns the next transaction.
    db.rollback()
    if already_installed:
      log.info(
        "bootstrap: %s already installed (app id=%s)",
        bootstrap_app.manifest_id, existing_id,
      )
      # A new deployment whose earlier Store-pin transaction failed still owes
      # the pin even though the Store is already installed this boot. The live
      # row (deleted_at is None whenever a reinstall-policy app reads as already
      # installed) is the one to pin.
      if (
        default_pin_pending
        and bootstrap_app.pin_on_first_install
        and existing is not None
        and existing.deleted_at is None
        and _mark_default_store_pinned(db, existing.id)
      ):
        default_pin_pending = False
      continue
    log.info(
      "bootstrap: installing %s from %s",
      bootstrap_app.manifest_id, bootstrap_app.manifest_url,
    )
    try:
      result = await install_from_manifest(
        db,
        manifest_url=bootstrap_app.manifest_url,
        manifest=None,
        raw_base=None,
        source="bootstrap",
      )
      app = result.app
      mode = result.mode
      warnings = result.warnings
    except Exception as exc:
      # Catch-all on purpose: no manifest failure should crash lifespan or
      # prevent the remaining bootstrap apps from installing.
      log.exception(
        "bootstrap: %s install failed — %s", bootstrap_app.manifest_id, exc,
      )
      continue
    log.info(
      "bootstrap: %s install %s (app id=%s, warnings=%s)",
      bootstrap_app.manifest_id, mode, app.id, warnings,
    )
    if (
      default_pin_pending
      and bootstrap_app.pin_on_first_install
      and _mark_default_store_pinned(db, app.id)
    ):
      default_pin_pending = False
