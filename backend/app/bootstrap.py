"""First-boot bootstrap for the core and managed-account apps.

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
from app.config import get_settings
from app.install import install_from_manifest

log = logging.getLogger("mobius.bootstrap")

# The Store is part of the recovery path, so first boot must install the exact
# revision reviewed with this platform release rather than whatever happens to
# be at a mutable branch tip. The catalog has no release tags yet; pinning the
# reviewed commit provides the same immutable input until it does.
BOOTSTRAP_STORE_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-store/"
  "44468e72008d6b9a4a5a0344e8148d69e7b58fa1/mobius.json"
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
class _BootstrapApp:
  name: str
  manifest_url: str
  reinstall_after_uninstall: bool


_BOOTSTRAP_APPS = (
  _BootstrapApp("store", BOOTSTRAP_STORE_MANIFEST_URL, True),
  # Skills is NOT the recovery surface — an owner uninstall is respected, and
  # the Store remains the way back.
  _BootstrapApp("skills", BOOTSTRAP_SKILLS_MANIFEST_URL, False),
  _BootstrapApp("memory", BOOTSTRAP_MEMORY_MANIFEST_URL, False),
  _BootstrapApp(
    "reflection", BOOTSTRAP_REFLECTION_MANIFEST_URL, False,
  ),
  # Connections manages owner MCP connections — the only management surface
  # since the Settings section moved into the app. An owner uninstall is
  # respected; the Store remains the way back.
  _BootstrapApp(
    "connections", BOOTSTRAP_CONNECTIONS_MANIFEST_URL, False,
  ),
  # Managed Railway owners have already authenticated with mobius.you. Their
  # identity surface is therefore useful immediately, but it must not appear
  # on ordinary self-hosted instances where no launcher account exists.
  _BootstrapApp(
    "identity", BOOTSTRAP_IDENTITY_MANIFEST_URL, False,
  ),
)

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

  for bootstrap_app in _BOOTSTRAP_APPS:
    if bootstrap_app.name == "identity" and not get_settings().mobius_sso_enabled:
      log.info("bootstrap: identity skipped for local-account deployment")
      continue
    # Use the installer's identity resolver here too: bootstrap and an explicit
    # Store action must agree across ref moves and legacy rows whose matching
    # catalog origin predates persisted manifest identity.
    existing = _find_install_identity_row(
      db,
      source_url=bootstrap_app.manifest_url,
      manifest_id=bootstrap_app.name,
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
        bootstrap_app.name, existing_id,
      )
      continue
    log.info(
      "bootstrap: installing %s from %s",
      bootstrap_app.name, bootstrap_app.manifest_url,
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
        "bootstrap: %s install failed — %s", bootstrap_app.name, exc,
      )
      continue
    log.info(
      "bootstrap: %s install %s (app id=%s, warnings=%s)",
      bootstrap_app.name, mode, app.id, warnings,
    )
