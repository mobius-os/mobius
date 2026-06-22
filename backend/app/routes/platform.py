"""Owner-gated platform self-update routes.

Three tiny endpoints behind ``get_current_owner`` + ``reject_cross_site``:
``GET /status`` (cheap, read-only — drives the Settings "Updates" line),
``POST /apply`` (run the baked merge or open a resolver chat), and
``POST /restart`` (owner-confirmed self-restart, same SIGTERM pattern as the
normal Settings restart). The status route is wrapped so a transient git error
can never break the Settings page.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import models, platform_update
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.platform_update import PlatformApplyResult, PlatformStatus, PlatformUpdateError

log = logging.getLogger("mobius.platform")

router = APIRouter(prefix="/api/platform", tags=["platform"])


def _sigterm_self_after_response() -> None:
  """SIGTERM this worker once the response is flushed; docker-compose
  ``restart: unless-stopped`` reboots the container cleanly (same mechanism as
  the admin and recovery restart paths)."""
  os.kill(os.getpid(), signal.SIGTERM)


@router.get("/status")
async def get_platform_status(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformStatus:
  """Cheap, read-only update availability for Settings. Never raises — a git
  hiccup degrades to "up to date" rather than breaking the page."""
  def _status() -> PlatformStatus:
    platform_update.clear_restart_needed_if_reconciled()
    return platform_update.platform_status()

  try:
    return await asyncio.to_thread(_status)
  except Exception as exc:
    log.warning("platform status failed: %r", exc)
    return PlatformStatus(
      state=platform_update.PlatformUpdateState.UP_TO_DATE.value,
      available=False, needs_restart=False, current_build_sha=None,
      recorded_upstream_sha=None, seed_required=False, conflict_paths=[],
    )


@router.post("/apply", dependencies=[Depends(reject_cross_site)])
async def apply_platform_update(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
) -> PlatformApplyResult:
  """Apply the baked platform update to the live backend, or open an agent
  resolver chat when it conflicts with local edits."""
  try:
    return await platform_update.apply_platform_update(db)
  except PlatformUpdateError as exc:
    # A known, recoverable precondition failure (e.g. missing seed tag) — tell
    # the UI plainly; the instance is untouched.
    raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/restart", dependencies=[Depends(reject_cross_site)])
def restart_platform(
  _: models.Owner = Depends(get_current_owner),
) -> JSONResponse:
  """Owner-confirmed restart to finish an update. Sends the response, then
  SIGTERMs this process so the worker reboots with the new code."""
  return JSONResponse(
    {"status": "restarting"},
    background=BackgroundTask(_sigterm_self_after_response),
  )
