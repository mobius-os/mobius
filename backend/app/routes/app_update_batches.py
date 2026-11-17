"""Review-bound batch admission and recoverable per-app progress."""

import json
from contextlib import aclosing
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.app_update_batches import AppUpdateBatches, ReviewedUpdateBatch
from app.database import get_db
from app.streaming import ClosingStreamingResponse
from app.deps import (
  get_owner_or_app_with_manage_apps, get_principal, Principal,
  reject_cross_site, require_owner_or_full_access_control,
  revocable_browser_stream,
)

router = APIRouter(prefix="/update-batches")


async def batches(request: Request) -> AppUpdateBatches:
  supervisors = getattr(request.app.state, "runtime_supervisors", None)
  owner = getattr(supervisors, "app_updates", None)
  if owner is not None and owner.failure:
    # A boot read that failed (an unreadable receipt, a transient database
    # error) is retried here rather than needing a restart.
    await owner.retry_start()
  if owner is not None and owner.failure:
    raise HTTPException(503, owner.failure)
  if owner is None or not owner.accepting:
    raise HTTPException(503, "App update progress is unavailable while the server restarts.")
  return owner


@router.post("", dependencies=[
  Depends(reject_cross_site),
  Depends(require_owner_or_full_access_control),
  Depends(get_owner_or_app_with_manage_apps),
])
async def start_batch(
  body: ReviewedUpdateBatch, request: Request, db: Session = Depends(get_db),
):
  return (await batches(request)).submit(body, db)


@router.get("", dependencies=[Depends(get_owner_or_app_with_manage_apps)])
async def list_batches(request: Request, db: Session = Depends(get_db)):
  owner = await batches(request)
  for snapshot in owner.recent():
    try:
      await owner.refresh_results(snapshot["id"], db)
    except HTTPException as exc:
      if exc.status_code != 404:
        raise
      # Pruned by a concurrent submit while listing: just leave it out.
  return {"batches": owner.recent()}


@router.get("/{batch_id}", dependencies=[Depends(get_owner_or_app_with_manage_apps)])
async def get_batch(batch_id: UUID, request: Request, db: Session = Depends(get_db)):
  owner = await batches(request)
  await owner.refresh_results(str(batch_id), db)
  return owner.snapshot(str(batch_id))


@router.get("/{batch_id}/events", dependencies=[Depends(get_owner_or_app_with_manage_apps)])
async def batch_events(
  batch_id: UUID, request: Request,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  owner = await batches(request)

  async def generate():
    # aclosing: a disconnect closes the inner generator at once, removing
    # its listener, rather than whenever it is garbage-collected.
    async with aclosing(owner.snapshots(str(batch_id))) as snapshots:
      async for snapshot in snapshots:
        yield ": keepalive\n\n" if snapshot is None else f"data: {json.dumps(snapshot)}\n\n"

  # Wrap first: a guest stream copies the owner's id and token epoch now,
  # while the principal is still attached; the refresh below expires it and
  # closing the session detaches it.
  body = revocable_browser_stream(generate(), principal)
  await owner.refresh_results(str(batch_id), db)
  db.close()
  # Closes the snapshot generator as soon as ASGI abandons a send, so a
  # dropped reader's listener is removed promptly.
  return ClosingStreamingResponse(
    body,
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
