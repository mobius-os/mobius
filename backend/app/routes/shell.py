"""Owner-gated shell self-update routes.

Two tiny endpoints behind ``get_current_owner``: ``GET /status`` (cheap,
read-only — drives the Settings "Shell update" line) and ``POST /apply`` (run
the baked merge or open a resolver chat). The status route is wrapped so a
transient git error can never break the Settings page; the apply route is also
``reject_cross_site``-gated like every state-changing endpoint.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models, shell_update
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.shell_update import ShellApplyResult, ShellStatus

log = logging.getLogger("mobius.shell")

router = APIRouter(prefix="/api/shell", tags=["shell"])


@router.get("/status")
async def get_shell_status(
  _: models.Owner = Depends(get_current_owner),
) -> ShellStatus:
  """Cheap, read-only update availability for Settings. Never raises — a git
  hiccup degrades to "up to date" rather than breaking the page."""
  try:
    return await asyncio.to_thread(shell_update.shell_status)
  except Exception as exc:
    log.warning("shell status failed: %r", exc)
    return ShellStatus(
      available=False, current_build_sha=None, seed_required=False,
      conflict=False, conflict_paths=[], conflict_chat_id=None,
    )


@router.post("/apply", dependencies=[Depends(reject_cross_site)])
async def apply_shell_update(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
) -> ShellApplyResult:
  """Apply the baked shell update to the live shell source, or open an agent
  resolver chat when it conflicts with local edits. A clean apply trips a
  rebuild (hot — no restart)."""
  return await shell_update.apply_shell_update(db)
