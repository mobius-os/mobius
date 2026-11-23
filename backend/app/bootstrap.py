"""First-boot bootstrap for the default apps.

Called from the FastAPI lifespan handler once the server is up and the
DB is migrated. Calls `install_from_manifest()` directly (in-process)
rather than HTTPing the install route — the server isn't necessarily
ready to accept connections from itself at lifespan-startup time, and
an in-process call skips the auth + rate-limit layers that exist for
external callers we don't need to traverse here.

Failure is non-fatal and isolated per app: a network blip fetching one
manifest must not crash uvicorn or prevent the remaining bootstrap apps
from installing. We log each failure and continue. When a failure is only a
wait (a GitHub rate limit, a gateway error, a slow Git transfer), one
background retry repeats the idempotent bootstrap with capped backoff and the
owner is told once, so a new deployment is never left without its Store until
the next restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import app_git, fs_locks, models
from app.broadcast import get_system_broadcast
from app.manifest_identity import canonical_manifest_identity_key
from app.git_transfers import discard_staged
from app.install import install_from_manifest, prepare_package, transient_failure
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
BOOTSTRAP_SOCIAL_MANIFEST_URL = _MANIFEST_URL.format(repo="app-social")


@dataclass(frozen=True)
class _BootstrapApp:
  manifest_id: str
  name: str
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
    "store", "App Store", BOOTSTRAP_STORE_MANIFEST_URL, True,
    pin_on_first_install=True,
  ),
  # Skills is NOT the recovery surface — an owner uninstall is respected, and
  # the Store remains the way back.
  _BootstrapApp("skills", "Skills", BOOTSTRAP_SKILLS_MANIFEST_URL, False),
  _BootstrapApp("memory", "Memory", BOOTSTRAP_MEMORY_MANIFEST_URL, False),
  _BootstrapApp(
    "reflection", "Reflection", BOOTSTRAP_REFLECTION_MANIFEST_URL, False,
  ),
  # Integrations is the only management surface for owner MCP integrations
  # since the Settings section moved into the app. Its explicit predecessor
  # lets active installs migrate while an owner uninstall remains respected.
  _BootstrapApp(
    "integrations", "Integrations", BOOTSTRAP_INTEGRATIONS_MANIFEST_URL, False,
    predecessor_manifest_id="connections",
    predecessor_manifest_url=(
      BOOTSTRAP_INTEGRATIONS_PREDECESSOR_MANIFEST_URL
    ),
  ),
  # Social (federated community board + direct messages) ships on every
  # deployment. Installing it does NOT join the public community: browsing is
  # open, and the owner explicitly opts in to join before they can post.
  _BootstrapApp(
    "social", "Social", BOOTSTRAP_SOCIAL_MANIFEST_URL, False,
  ),
)

# Tests set MOEBIUS_SKIP_BOOTSTRAP=1 so the pytest suite doesn't hit
# the live GitHub URL. Set in docker-compose.test.yml's `pytest`
# service environment block.
_SKIP_ENV = "MOEBIUS_SKIP_BOOTSTRAP"

# Fixed sentinel key for the single DefaultPinInitialization row.
_DEFAULT_PINS_MARKER_ID = "default_pins"

# Waits between retries of a bootstrap that failed only transiently; the last
# repeats. An upstream wait hint (Retry-After) lengthens a step up to that cap.
# Retrying stops after a day; the next process start tries again.
_RETRY_DELAYS_SECS = (60, 5 * 60, 15 * 60, 60 * 60)
_RETRY_WINDOW_SECS = 24 * 60 * 60


@dataclass(frozen=True)
class BootstrapOutcome:
  """Default apps (manifest ids) whose install failed only transiently."""

  pending: tuple[str, ...] = ()
  rate_limited: bool = False
  retry_after: int | None = None


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


async def ensure_bootstrap_apps_installed(
  db: Session, *, retry: tuple[str, ...] | None = None,
) -> BootstrapOutcome:
  """Idempotently install the configured bootstrap apps when absent.

  Identity is keyed on `manifest_url`, not slug. This means:
    1. The bootstrapped store doesn't always end up with slug='store'
       — if the user already built an app called "store", first-boot
       slug-assignment hands it a fallback like 'app-store'. A slug
       check would then mis-treat the bootstrapped store as absent
       and try to install it again every boot.
    2. Every bootstrap app retains its canonical identity even if its
       assigned slug differs from the catalog slug.

  Caller is the FastAPI lifespan/startup handler, before anything is
  serving, or `retry_bootstrap_apps` with `retry` naming the manifest ids that
  failed. A retry installs only those, so an app the owner uninstalled during
  the session stays uninstalled, and still settles an owed Store pin. Owns no
  transaction state — `install_from_manifest` commits its own work on success
  and rolls back on failure. We just decide whether to call it. Returns the
  apps that failed transiently, for `retry_bootstrap_apps`.
  """
  if os.environ.get(_SKIP_ENV) == "1":
    log.info("bootstrap: %s=1, skipping bootstrap app installs", _SKIP_ENV)
    return BootstrapOutcome()

  # Bootstrap uses the same resolver as preview and install so all three paths
  # agree on persisted identities, moved refs, and proven legacy origins.
  from app.install import _find_install_identity_row

  # Decide before installing anything, since installs create app rows.
  try:
    default_pin_pending = _default_store_pin_pending(db)
  except Exception:
    db.rollback()
    log.exception("bootstrap: could not decide default pins; pinning nothing")
    default_pin_pending = False

  pending: list[str] = []
  rate_limited = False
  retry_after: int | None = None
  for bootstrap_app in _CORE_BOOTSTRAP_APPS:
    prepared = None
    stage_error: Exception | None = None
    if retry is not None and bootstrap_app.manifest_id in retry:
      # A retry holds the lifecycle lock below; no network may run under it,
      # so the package is fetched first and simply discarded if it goes unused.
      try:
        prepared = await prepare_package(bootstrap_app.manifest_url)
      except Exception as exc:
        stage_error = exc
    try:
      # Once the server is serving, owner installs and uninstalls, the tombstone
      # purge and schedule convergence run too; take the lifecycle lock per app,
      # as the install route does, so one slow transfer never blocks them all.
      async with (
        fs_locks.install_uninstall_lock() if retry is not None
        else contextlib.nullcontext()
      ):
        # Use the installer's address lookup here too: bootstrap and an
        # explicit Store action must agree across ref moves and legacy rows whose
        # matching catalog origin predates persisted manifest identity. This is
        # only the address half of `install.resolve_package_row`: it runs before
        # any package is fetched, so there is no `package_id` to check, and it
        # must see a deleted predecessor to keep an owner's uninstall. The
        # install below applies the whole rule to the fetched package.
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
          predecessor.manifest_url = canonical_manifest_identity_key(
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
            _publish_app_updated(existing.id)
          continue
        if retry is not None and bootstrap_app.manifest_id not in retry:
          continue
        log.info(
          "bootstrap: installing %s from %s",
          bootstrap_app.manifest_id, bootstrap_app.manifest_url,
        )
        try:
          if stage_error is not None:
            raise stage_error
          if retry is None:
            # First boot holds no lock, so the package is fetched here.
            prepared = await prepare_package(bootstrap_app.manifest_url)
          result = await install_from_manifest(
            db,
            manifest_url=bootstrap_app.manifest_url,
            manifest=None,
            raw_base=None,
            source="bootstrap",
            prepared=prepared,
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
          transient, wait = transient_failure(exc)
          if transient:
            pending.append(bootstrap_app.manifest_id)
            rate_limited = rate_limited or (
              isinstance(exc, HTTPException) and exc.status_code == 429
            )
            if wait is not None:
              retry_after = max(retry_after or 0, wait)
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
      # Like the install route: the Shell refetches its apps, so an app (and its
      # pin) installed after the page loaded appears without a reload.
      _publish_app_updated(app.id)
    finally:
      if prepared is not None:
        await asyncio.to_thread(discard_staged, prepared.staged_clone)
  return BootstrapOutcome(tuple(pending), rate_limited, retry_after)


def _publish_app_updated(app_id: int) -> None:
  get_system_broadcast().publish({"type": "app_updated", "appId": str(app_id)})


async def retry_bootstrap_apps(outcome: BootstrapOutcome) -> None:
  """Repeat the bootstrap with capped backoff until nothing transient is left.

  Runs as a runtime supervisor once the server is serving. Each attempt
  reruns the idempotent bootstrap for the pending apps only, under the
  lifecycle lock, so apps that installed meanwhile are skipped, an owner's
  uninstall during the session is respected, and a still-owed default Store
  pin settles with the Store. The owner is told once per retry run, only when the first
  retry has also failed and an owner exists.
  """
  from app.database import SessionLocal

  deadline = time.monotonic() + _RETRY_WINDOW_SECS
  notification_id = str(uuid.uuid4())
  notified = False
  attempt = 0
  while outcome.pending:
    if attempt and not notified:
      notified = await _notify_owner_pending(outcome, notification_id)
    step = _RETRY_DELAYS_SECS[min(attempt, len(_RETRY_DELAYS_SECS) - 1)]
    delay = max(step, min(outcome.retry_after or 0, _RETRY_DELAYS_SECS[-1]))
    if time.monotonic() + delay > deadline:
      log.warning(
        "bootstrap: still could not install %s; retrying at next start",
        ", ".join(outcome.pending),
      )
      return
    log.info(
      "bootstrap: retrying %s in %d seconds", ", ".join(outcome.pending), delay,
    )
    await asyncio.sleep(delay)
    attempt += 1
    try:
      with SessionLocal() as db:
        outcome = await ensure_bootstrap_apps_installed(
          db, retry=outcome.pending,
        )
    except Exception:
      # The previous outcome stays owed; the next step tries again.
      log.exception("bootstrap: retry attempt failed")
  log.info("bootstrap: retry finished the default apps")


async def _notify_owner_pending(
  outcome: BootstrapOutcome, notification_id: str,
) -> bool:
  """Tell the owner once which default apps are still on their way."""
  from app import push
  from app.database import SessionLocal

  names = [
    app.name for app in _CORE_BOOTSTRAP_APPS
    if app.manifest_id in outcome.pending
  ]
  listed = (
    names[0] if len(names) == 1
    else ", ".join(names[:-1]) + " and " + names[-1]
  )
  cause = "GitHub rate limit" if outcome.rate_limited else "temporary problem"
  try:
    with SessionLocal() as db:
      owner = db.query(models.Owner.id).first()
      if owner is None:
        return False
      await push.notify_owner_async(
        db,
        owner[0],
        title="Some apps are still installing",
        body=(
          f"{listed} couldn't be installed yet ({cause}); "
          "retrying automatically."
        ),
        notification_id=notification_id,
      )
  except Exception:
    log.warning("bootstrap: could not notify the owner", exc_info=True)
    return False
  return True
