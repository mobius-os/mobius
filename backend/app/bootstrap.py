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
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.install import install_from_manifest

log = logging.getLogger("mobius.bootstrap")

# The Store is part of the recovery path, so first boot must install the exact
# revision reviewed with this platform release rather than whatever happens to
# be at a mutable branch tip. The catalog has no release tags yet; pinning the
# reviewed commit provides the same immutable input until it does.
BOOTSTRAP_STORE_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-store/"
  "4371719331644b7f5005ef46f2011f7cab0f4851/mobius.json"
)

# The Skills app (browse/install ecosystem skills + the skill-agent chat).
# Canonical home is the app-skills catalog repo. PINNED to a reviewed commit,
# never a mutable branch: core and app releases pair explicitly — this pin
# names the newest app revision reviewed against THIS platform's API surface,
# and bumping it is a deliberate platform commit that rides the same release.
# (Now v2.0.0 — compat badges, catalog browser — which requires this platform's
# skills API; pinned here as core #146 and app #4 merged together in this
# release. The prior v1.1.2 pin needed no skills API and ran on any core.)
BOOTSTRAP_SKILLS_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-skills/"
  "113210883ddab380a01da1443e61600439d23b2a/mobius.json"
)

BOOTSTRAP_MEMORY_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-memory/main/mobius.json"
)
BOOTSTRAP_REFLECTION_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-reflection/main/mobius.json"
)
BOOTSTRAP_CONNECTIONS_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-connections/main/mobius.json"
)
BOOTSTRAP_IDENTITY_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-mobius-you/main/mobius.json"
)


@dataclass(frozen=True)
class _PublishedBootstrapApp:
  manifest_id: str
  manifest_url: str
  published_at: datetime


# Activation gate for the audited Social release. Keep this closed until the
# migrated release is public, then replace None with its manifest id, immutable
# commit-pinned manifest URL, and publication time. The timestamp separates
# post-publication deployments from existing owners without another settings
# table or a one-shot install migration.
BOOTSTRAP_SOCIAL_RELEASE: _PublishedBootstrapApp | None = None


@dataclass(frozen=True)
class _BootstrapApp:
  manifest_id: str
  manifest_url: str
  reinstall_after_uninstall: bool


_CORE_BOOTSTRAP_APPS = (
  _BootstrapApp("store", BOOTSTRAP_STORE_MANIFEST_URL, True),
  # Skills is NOT the recovery surface — an owner uninstall is respected, and
  # the Store remains the way back.
  _BootstrapApp("skills", BOOTSTRAP_SKILLS_MANIFEST_URL, False),
  _BootstrapApp("memory", BOOTSTRAP_MEMORY_MANIFEST_URL, False),
  _BootstrapApp(
    "reflection", BOOTSTRAP_REFLECTION_MANIFEST_URL, False,
  ),
  # Integrations (internal package id `connections`) manages owner MCP
  # connections — the only management surface since the Settings section moved
  # into the app. An owner uninstall is respected; the Store remains the way
  # back.
  _BootstrapApp(
    "connections", BOOTSTRAP_CONNECTIONS_MANIFEST_URL, False,
  ),
  # Möbius · You is useful on every new deployment. Managed owners arrive
  # signed in; local owners see the same app with account linking optional.
  _BootstrapApp(
    "identity", BOOTSTRAP_IDENTITY_MANIFEST_URL, False,
  ),
)

_PINNED_SOCIAL_MANIFEST = re.compile(
  r"https://raw\.githubusercontent\.com/mobius-os/app-social/"
  r"[0-9a-f]{40}/mobius\.json"
)


def _deployment_predates(db: Session, published_at: datetime) -> bool:
  """Whether durable owner/app state existed before an app was published.

  Bootstrap is invoked on every server start. Comparing against the immutable
  release time keeps a failed install retryable on a genuinely new deployment,
  while an upgrade of an established deployment never acquires a newly-added
  default merely because bootstrap ran again. Null legacy timestamps are
  conservatively treated as pre-existing.
  """
  cutoff = published_at.astimezone(UTC).replace(tzinfo=None)
  for created_at in (models.Owner.created_at, models.App.created_at):
    if db.query(created_at).filter(or_(
      created_at.is_(None), created_at < cutoff,
    )).first() is not None:
      return True
  return False


def _configured_bootstrap_apps(db: Session) -> tuple[_BootstrapApp, ...]:
  """Return this release's defaults, failing closed for unpublished Social."""
  release = BOOTSTRAP_SOCIAL_RELEASE
  if release is None:
    return _CORE_BOOTSTRAP_APPS
  if (
    not release.manifest_id.strip()
    or _PINNED_SOCIAL_MANIFEST.fullmatch(release.manifest_url) is None
    or release.published_at.tzinfo is None
  ):
    log.error(
      "bootstrap: Social release gate is invalid; Social remains disabled"
    )
    return _CORE_BOOTSTRAP_APPS
  if _deployment_predates(db, release.published_at):
    log.info(
      "bootstrap: Social skipped for a deployment predating its release"
    )
    return _CORE_BOOTSTRAP_APPS
  return _CORE_BOOTSTRAP_APPS + (_BootstrapApp(
    release.manifest_id,
    release.manifest_url,
    False,
  ),)

# Tests set MOEBIUS_SKIP_BOOTSTRAP=1 so the pytest suite doesn't hit
# the live GitHub URL. Set in docker-compose.test.yml's `pytest`
# service environment block.
_SKIP_ENV = "MOEBIUS_SKIP_BOOTSTRAP"


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
  from app.install import _find_install_identity_row

  # Resolve the release cohort once before any successful install creates an
  # App row. This keeps the first-install decision stable for the whole pass.
  bootstrap_apps = _configured_bootstrap_apps(db)
  for bootstrap_app in bootstrap_apps:
    # Use the installer's identity resolver here too: bootstrap and an explicit
    # Store action must agree across ref moves and legacy rows whose matching
    # catalog origin predates persisted manifest identity.
    existing = _find_install_identity_row(
      db,
      source_url=bootstrap_app.manifest_url,
      manifest_id=bootstrap_app.manifest_id,
    )
    existing_id = existing.id if existing is not None else None
    already_installed = existing is not None and (
      existing.deleted_at is None
      or not bootstrap_app.reinstall_after_uninstall
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
