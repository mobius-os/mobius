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

from app import models
from app.install import install_from_manifest

log = logging.getLogger("mobius.bootstrap")

# Pinned to `main` for v1 — the app-store repo doesn't have tagged
# releases yet. TODO: switch to a tagged release URL once we cut one,
# so a fresh container doesn't pick up an in-flight store commit.
BOOTSTRAP_STORE_MANIFEST_URL = (
  "https://raw.githubusercontent.com/mobius-os/app-store/main/mobius.json"
)

# Tests set MOEBIUS_SKIP_BOOTSTRAP=1 so the pytest suite doesn't hit
# the live GitHub URL. Set in docker-compose.test.yml's `pytest`
# service environment block.
_SKIP_ENV = "MOEBIUS_SKIP_BOOTSTRAP"


async def ensure_store_installed(db: Session) -> None:
  """Idempotent: if no App with slug='store' exists, install it.

  Caller is the FastAPI lifespan/startup handler. Owns no transaction
  state — `install_from_manifest` commits its own work on success and
  rolls back on failure. We just decide whether to call it.
  """
  if os.environ.get(_SKIP_ENV) == "1":
    log.info("bootstrap: %s=1, skipping store install", _SKIP_ENV)
    return
  existing = (
    db.query(models.App).filter(models.App.slug == "store").first()
  )
  if existing is not None:
    log.info("bootstrap: store already installed (app id=%s)", existing.id)
    return
  log.info("bootstrap: installing store from %s", BOOTSTRAP_STORE_MANIFEST_URL)
  try:
    app, mode, warnings, _manifest = await install_from_manifest(
      db,
      manifest_url=BOOTSTRAP_STORE_MANIFEST_URL,
      manifest=None,
      raw_base=None,
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
