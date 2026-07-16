"""Owner-gated platform self-update routes.

Small endpoints behind ``get_current_owner`` + ``reject_cross_site``:
``GET /status`` (cheap, read-only, fetch-free — drives the Settings "Updates"
line), ``POST /check`` (owner-triggered ``git fetch`` + fresh status, the
on-demand refresh for the "Check for updates" button), ``POST /apply`` (fetch
origin + rebase local edits onto the new version, or record a conflict),
``POST /conflict-resolver-chat`` (owner-clicked resolver chat), and
``POST /restart`` (owner-confirmed self-restart, same SIGTERM pattern as the
normal Settings restart). The status/check routes are wrapped so a transient git
error can never break the Settings page. Conflict resolution is a separate
owner-clicked endpoint so applying an update never silently starts an agent turn.
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
from app.platform_update import (
  PlatformApplyResult, PlatformConflictResolverChatOut, PlatformStatus,
  PlatformUpdateError, PlatformUpdatePreview,
)
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
      recorded_upstream_sha=None, contained_upstream_sha=None,
      seed_required=False, conflict_paths=[],
      conflict_chat_id=None,
    )


@router.post("/check", dependencies=[Depends(reject_cross_site)])
async def check_platform_updates(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformStatus:
  """Owner-triggered "Check for updates": fetch origin, then return the fresh
  availability. `GET /status` is fetch-free (cheap), so this is the on-demand
  refresh. A failed check is a 503 so Settings cannot mistake stale tracking
  data for an authoritative "No updates found" result."""
  try:
    return await asyncio.to_thread(platform_update.check_for_updates)
  except Exception as exc:
    log.warning("platform check failed: %r", exc)
    raise HTTPException(
      status_code=503,
      detail="Could not reach the platform update source.",
    )


@router.get("/update-preview")
async def get_platform_update_preview(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformUpdatePreview:
  """Read-only preview of the incoming platform update, for the Settings review
  step the owner sees before Apply. Fetch-free and non-mutating like ``/status``.
  Never raises — a git hiccup degrades to an empty preview so the review sheet
  shows "nothing to review" rather than breaking the page."""
  try:
    return await asyncio.to_thread(platform_update.platform_update_preview)
  except Exception as exc:
    log.warning("platform update-preview failed: %r", exc)
    return platform_update.empty_platform_update_preview()


@router.post("/apply", dependencies=[Depends(reject_cross_site)])
async def apply_platform_update(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
) -> PlatformApplyResult:
  """Fetch origin and rebase the local edits onto the new platform version, or
  record a conflict for an owner-clicked resolver chat. A clean apply advances
  the served tree on disk and marks a restart to load it."""
  try:
    return await platform_update.apply_platform_update(db)
  except PlatformUpdateError as exc:
    # A known, recoverable precondition failure (offline fetch, not a clone) —
    # tell the UI plainly; the instance is untouched.
    raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conflict-resolver-chat", dependencies=[Depends(reject_cross_site)])
async def create_platform_conflict_resolver_chat(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
) -> PlatformConflictResolverChatOut:
  """Create or return the resolver chat for a recorded platform update conflict."""
  try:
    return await platform_update.create_platform_conflict_resolver_chat(db)
  except PlatformUpdateError as exc:
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
