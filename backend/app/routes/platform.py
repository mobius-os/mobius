"""Owner-gated platform self-update routes.

Small endpoints behind ``get_current_owner`` + ``reject_cross_site``:
``GET /status`` (cheap, read-only, fetch-free — drives the Settings "Updates"
line), ``POST /check`` (owner-triggered ``git fetch`` + fresh status, the
on-demand refresh for the "Check for updates" button), ``POST /apply`` (fetch
origin + rebase local edits onto the new version, or open a resolver chat on
conflict), and ``POST /restart`` (owner-confirmed self-restart, same SIGTERM
pattern as the normal Settings restart). The status/check routes are wrapped so a
transient git error can never break the Settings page.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import models, platform_update
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.platform_update import PlatformApplyResult, PlatformStatus, PlatformUpdateError
from app.restart_util import restart_this_worker

log = logging.getLogger("mobius.platform")

router = APIRouter(prefix="/api/platform", tags=["platform"])


@router.get("/status")
async def get_platform_status(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformStatus:
  """Cheap, read-only update availability for Settings. Never raises — a git
  hiccup degrades to "up to date" rather than breaking the page.

  Does NOT clear the restart flag here: a restart-needed set by an owner Apply
  must persist (the running uvicorn still has the old code) until an actual boot
  reconcile clears it. Clearing on ancestry alone would drop it in the SAME stale
  process the moment the on-disk tree looks reconciled."""
  try:
    return await asyncio.to_thread(platform_update.platform_status)
  except Exception as exc:
    log.warning("platform status failed: %r", exc)
    return PlatformStatus(
      state=platform_update.PlatformUpdateState.UP_TO_DATE.value,
      available=False, needs_restart=False, current_build_sha=None,
      recorded_upstream_sha=None, seed_required=False, conflict_paths=[],
      conflict_chat_id=None,
    )


@router.post("/check", dependencies=[Depends(reject_cross_site)])
async def check_platform_updates(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformStatus:
  """Owner-triggered "Check for updates": fetch origin, then return the fresh
  availability. `GET /status` is fetch-free (cheap), so this is the on-demand
  refresh. Never raises — an offline/erroring fetch degrades to a safe "up to
  date" rather than breaking Settings, same contract as /status."""
  try:
    return await asyncio.to_thread(platform_update.check_for_updates)
  except Exception as exc:
    log.warning("platform check failed: %r", exc)
    return PlatformStatus(
      state=platform_update.PlatformUpdateState.UP_TO_DATE.value,
      available=False, needs_restart=False, current_build_sha=None,
      recorded_upstream_sha=None, seed_required=False, conflict_paths=[],
      conflict_chat_id=None,
    )


@router.post("/apply", dependencies=[Depends(reject_cross_site)])
async def apply_platform_update(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
) -> PlatformApplyResult:
  """Fetch origin and rebase the local edits onto the new platform version, or
  open an agent resolver chat when it conflicts with local edits. A clean apply
  advances the served tree on disk and marks a restart to load it."""
  try:
    return await platform_update.apply_platform_update(db)
  except PlatformUpdateError as exc:
    # A known, recoverable precondition failure (offline fetch, not a clone) —
    # tell the UI plainly; the instance is untouched.
    raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/restart", dependencies=[Depends(reject_cross_site)])
def restart_platform(
  _: models.Owner = Depends(get_current_owner),
) -> JSONResponse:
  """Owner-confirmed restart to finish an update. Sends the response, then
  restarts this worker (force-exit fallback) so it reboots with the new code."""
  return JSONResponse(
    {"status": "restarting"},
    background=BackgroundTask(restart_this_worker),
  )
