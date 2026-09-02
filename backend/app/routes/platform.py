"""Owner-gated platform self-update routes.

Small endpoints behind ``get_current_owner`` + ``reject_cross_site``:
``GET /status`` (cheap, read-only, fetch-free — drives the Settings "Updates"
line), ``POST /check`` (owner-triggered ``git fetch`` + fresh status, the
on-demand refresh for the "Check for updates" button), ``GET /update-preview``
(an immutable exact-target plan), ``POST /apply`` (apply that reviewed target
and merge it with local edits, or record a conflict),
``GET /update-progress`` (the active Apply phase),
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
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import deployment_control, models, platform_activation, platform_update
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.platform_update import (
  PlatformApplyResult, PlatformConflictResolverChatOut, PlatformStatus,
  PlatformUpdateError, PlatformUpdatePreview, PlatformUpdateProgress,
)
from app.restart_util import restart_this_worker

log = logging.getLogger("mobius.platform")

router = APIRouter(prefix="/api/platform", tags=["platform"])


_PLAN_ERROR_MESSAGES = {
  "update_plan_stale": (
    "Möbius changed since this preview. Refresh and review the update again."
  ),
  "update_plan_invalid": (
    "This update review is no longer valid. Refresh it and try again."
  ),
  "update_plan_target_missing": (
    "The reviewed release source is unavailable. Refresh and try again shortly."
  ),
  "image_release_source_unavailable": (
    "The official image is published, but its source revision could not be "
    "verified yet. Try again shortly."
  ),
  "image_release_invalid": (
    "The official image returned an invalid release identity. Try again later."
  ),
}


def _plan_error_detail(exc: PlatformUpdateError) -> dict[str, str]:
  code = str(exc)
  return {
    "code": code,
    "message": _PLAN_ERROR_MESSAGES.get(
      code,
      "This update could not be verified. Refresh it and try again.",
    ),
  }


class PlatformApplyIn(BaseModel):
  """The immutable update plan returned by ``GET /update-preview``."""

  plan_id: str = Field(min_length=64, max_length=64)
  current_sha: str = Field(min_length=40, max_length=64)
  target_sha: str = Field(min_length=40, max_length=64)
  image_digest: str | None = Field(
    default=None,
    pattern=r"^sha256:[0-9a-f]{64}$",
  )


@router.get("/status")
async def get_platform_status(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformStatus:
  """Read-only update availability for Settings. Railway resolves its published
  GHCR release identity; self-hosting remains a cheap local read. A managed
  release lookup failure is explicit so Settings never calls an unknown release
  current. Local Git diagnostics still degrade without breaking the page.

  Does NOT clear the restart flag here: a restart-needed set by an owner Apply
  must persist (the running uvicorn still has the old code) until an actual boot
  reconcile clears it. Clearing on ancestry alone would drop it in the SAME stale
  process the moment the on-disk tree looks reconciled."""
  try:
    if platform_activation.deployment_kind() == "railway":
      release = await deployment_control.latest_official_release()
      return await asyncio.to_thread(
        platform_update.platform_status,
        target_sha=release["build_sha"],
      )
    return await asyncio.to_thread(platform_update.platform_status)
  except deployment_control.DeploymentControlError as exc:
    raise HTTPException(
      status_code=exc.status_code,
      detail={"code": exc.code, "message": exc.message},
    ) from exc
  except Exception as exc:
    log.warning("platform status failed: %r", exc)
    return PlatformStatus(
      state=platform_update.PlatformUpdateState.UP_TO_DATE.value,
      available=False, needs_restart=False,
      activation=platform_activation.classify_activation([]),
      current_build_sha=None,
      recorded_upstream_sha=None, contained_upstream_sha=None,
      contained_upstream_committed_at=None, upstream_checked_at=None,
      seed_required=False, conflict_paths=[],
      conflict_chat_id=None, newer_updates_available=False,
      rollback_target_sha=None, rollback_error=None,
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
    if platform_activation.deployment_kind() == "railway":
      release = await deployment_control.latest_official_release()
      return await asyncio.to_thread(
        platform_update.check_for_updates,
        target_sha=release["build_sha"],
      )
    return await asyncio.to_thread(platform_update.check_for_updates)
  except deployment_control.DeploymentControlError as exc:
    raise HTTPException(
      status_code=exc.status_code,
      detail={"code": exc.code, "message": exc.message},
    ) from exc
  except platform_update.PlatformUpdateError as exc:
    raise HTTPException(status_code=503, detail=_plan_error_detail(exc)) from exc
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
  step the owner sees before Apply. Railway may fetch the exact source object
  named by GHCR, but never mutates the served branch or working tree. Missing
  managed release source is explicit; generic self-hosted read failures still
  degrade to an empty preview rather than breaking Settings."""
  try:
    if platform_activation.deployment_kind() == "railway":
      release = await deployment_control.latest_official_release()
      return await asyncio.to_thread(
        platform_update.platform_update_preview,
        target_sha=release["build_sha"],
        image_digest=release["image_digest"],
      )
    return await asyncio.to_thread(platform_update.platform_update_preview)
  except deployment_control.DeploymentControlError as exc:
    raise HTTPException(
      status_code=exc.status_code,
      detail={"code": exc.code, "message": exc.message},
    ) from exc
  except platform_update.PlatformUpdateError as exc:
    raise HTTPException(status_code=503, detail=_plan_error_detail(exc)) from exc
  except Exception as exc:
    log.warning("platform update-preview failed: %r", exc)
    return platform_update.empty_platform_update_preview()


@router.get("/update-progress")
async def get_platform_update_progress(
  _: models.Owner = Depends(get_current_owner),
) -> PlatformUpdateProgress:
  """Observable phase of the active or most recent owner-triggered Apply."""
  return platform_update.platform_update_progress()


@router.post("/apply", dependencies=[Depends(reject_cross_site)])
async def apply_platform_update(
  request: PlatformApplyIn,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
) -> PlatformApplyResult:
  """Apply exactly the release represented by the reviewed immutable plan."""
  try:
    return await platform_update.apply_platform_update(
      db,
      plan_id=request.plan_id,
      current_sha=request.current_sha,
      target_sha=request.target_sha,
      image_digest=request.image_digest,
    )
  except PlatformUpdateError as exc:
    # A known, recoverable precondition failure (offline fetch, not a clone) —
    # tell the UI plainly; the instance is untouched.
    raise HTTPException(status_code=409, detail=_plan_error_detail(exc)) from exc


@router.post(
  "/rebuild",
  dependencies=[Depends(reject_cross_site)],
  status_code=202,
)
async def rebuild_reviewed_platform_update(
  request: PlatformApplyIn,
  _: models.Owner = Depends(get_current_owner),
):
  """Deploy the exact digest-pinned GHCR image represented by the review."""
  if not request.image_digest:
    exc = PlatformUpdateError("update_plan_invalid")
    raise HTTPException(status_code=409, detail=_plan_error_detail(exc))
  try:
    return await deployment_control.request_reviewed_rebuild(
      plan_id=request.plan_id,
      current_sha=request.current_sha,
      target_sha=request.target_sha,
      image_digest=request.image_digest,
    )
  except platform_update.PlatformUpdateError as exc:
    raise HTTPException(status_code=409, detail=_plan_error_detail(exc)) from exc
  except deployment_control.DeploymentControlError as exc:
    raise HTTPException(
      status_code=exc.status_code,
      detail={"code": exc.code, "message": exc.message},
    ) from exc


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
