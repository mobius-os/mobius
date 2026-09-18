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

from app.install import install_from_manifest

log = logging.getLogger("mobius.bootstrap")

# The Store is part of the recovery path, so first boot must install the exact
# revision reviewed with this platform release rather than whatever happens to
# be at a mutable branch tip. The catalog has no release tags yet; pinning the
# reviewed commit provides the same immutable input until it does.
BOOTSTRAP_STORE_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-store/"
  "7140bc9afa2f60498993567ab4628a8b40345d14/mobius.json"
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
BOOTSTRAP_INTEGRATIONS_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-integrations/main/mobius.json"
)
BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-connections/main/mobius.json"
)
BOOTSTRAP_IDENTITY_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-mobius-you/main/mobius.json"
)
# Social (federated community board + direct messages). PINNED to a reviewed
# commit, never a mutable branch tip: first boot installs the exact audited
# revision that ships with this release, matching Store/Skills. This is a plain
# manifest install — the same generic mechanism as every other bootstrap app —
# not app-specific first-boot policy in the platform.
BOOTSTRAP_SOCIAL_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-social/"
  "5cb40d86953f689ee376fa45d39bcd07d7bd5f42/mobius.json"
)


@dataclass(frozen=True)
class _BootstrapApp:
  manifest_id: str
  manifest_url: str
  reinstall_after_uninstall: bool
  predecessor_manifest_id: str | None = None
  predecessor_manifest_url: str | None = None


_CORE_BOOTSTRAP_APPS = (
  _BootstrapApp("store", BOOTSTRAP_STORE_MANIFEST_URL, True),
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
