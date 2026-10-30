"""Owner routes for the Settings Storage and Memory box: read, measure, clean up."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app import models
from app.capacity import measure_capacity_now
from app.config import get_settings
from app.deps import get_current_owner, reject_cross_site
from app.resource_cleanup import CLEANUP_TARGETS, CleanupBusy, preview_cleanup, run_cleanup
from app.resource_overview import resources_overview
from app.runner_registry import registry

router = APIRouter(prefix="/api/resources", tags=["resources"])


async def _overview(data_dir: str) -> dict:
  # Reading every process's memory takes a moment; keep it off the event loop.
  return await asyncio.to_thread(resources_overview, data_dir, registry.all_alive_chat_ids())


def _known_target(target: str) -> str:
  if target not in CLEANUP_TARGETS:
    raise HTTPException(status_code=404, detail="Unknown cleanup.")
  return target


@router.get("")
async def read_resources(
  refresh: bool = Query(default=False),
  _owner: models.Owner = Depends(get_current_owner),
):
  """Storage, memory, what can be cleared, and running agent turns.

  ``refresh=true`` first re-measures every folder (a bounded pass of several
  seconds); otherwise the sizes are the monitor's last measurement.
  """
  data_dir = get_settings().data_dir
  if refresh:
    await asyncio.to_thread(measure_capacity_now, data_dir)
  return await _overview(data_dir)


@router.get("/cleanup/{target}")
async def preview_resource_cleanup(
  target: str,
  _owner: models.Owner = Depends(get_current_owner),
):
  """How much one cleanup would free now, for the confirm step."""
  return await preview_cleanup(_known_target(target), get_settings().data_dir)


@router.post("/cleanup/{target}", dependencies=[Depends(reject_cross_site)])
async def run_resource_cleanup(
  target: str,
  _owner: models.Owner = Depends(get_current_owner),
):
  """Run one cleanup and return what it freed plus the updated overview."""
  data_dir = get_settings().data_dir
  try:
    outcome = await run_cleanup(_known_target(target), data_dir)
  except CleanupBusy as exc:
    raise HTTPException(status_code=409, detail="Another cleanup is still running.") from exc
  return {"cleanup": outcome, "resources": await _overview(data_dir)}
