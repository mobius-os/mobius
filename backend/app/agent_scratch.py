"""Scratch directories for agent subprocesses, on the bounded data volume.

Agent CLIs write temporary files through TMPDIR. Left at the container's
/tmp those land in the overlay upperdir: no quota, statvfs reporting host
capacity, and — because /tmp is not a tmpfs in this image — never cleared,
so scratch accumulates for the life of the container.

Routing them to the data volume bounds that, but the volume also holds
SQLite, so unbounded scratch there is worse than unbounded scratch on the
host: it takes durable data down with it. This module owns the lifecycle
that makes the move safe.

Scratch is keyed per chat, matching the per-chat agent-browser profile it
sits beside in the runner. A chat with no run in flight cannot be using its
scratch, so that is the deletion rule.
"""

import logging
import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import agent_scratch_root

log = logging.getLogger(__name__)

# A run's row is created around the same moment its scratch is, and the two
# are not ordered against each other. Without a grace period a sweep driven
# by one starting run could delete the scratch of another that had not yet
# registered. Comfortably longer than that gap, far shorter than a turn.
_SWEEP_GRACE_SECONDS = 15 * 60


def _dir_name(chat_id: str) -> str:
  return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")


def scratch_for_chat(chat_id: str) -> Path:
  """Create and return the scratch directory this chat's agents may use."""
  path = agent_scratch_root() / _dir_name(chat_id)
  path.mkdir(parents=True, exist_ok=True)
  return path


def sweep_idle_scratch(db: Session, *, now: float | None = None) -> dict:
  """Delete scratch belonging to chats with no run still in flight.

  Returns a summary so the runtime retention supervisor can report what it
  reclaimed. Failure to remove one directory must not prevent the rest, so
  errors are collected rather than aborting the sweep.
  """
  root = agent_scratch_root()
  if not root.is_dir():
    return {"removed": 0, "bytes": 0, "kept_live": 0, "kept_recent": 0}

  live = {
    _dir_name(chat_id)
    for chat_id in db.scalars(
      select(models.ChatRun.chat_id).where(
        models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES)
      )
    ).all()
  }
  cutoff = (time.time() if now is None else now) - _SWEEP_GRACE_SECONDS
  removed = reclaimed = kept_live = kept_recent = 0

  for entry in root.iterdir():
    if not entry.is_dir():
      continue
    if entry.name in live:
      kept_live += 1
      continue
    try:
      if entry.stat().st_mtime > cutoff:
        kept_recent += 1
        continue
      size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
      shutil.rmtree(entry)
    except OSError as exc:
      log.warning("agent scratch sweep skipped %s: %s", entry.name, exc)
      continue
    removed += 1
    reclaimed += size

  if removed:
    log.info(
      "agent scratch swept dirs=%d bytes=%d kept_live=%d kept_recent=%d",
      removed, reclaimed, kept_live, kept_recent,
    )
  return {
    "removed": removed,
    "bytes": reclaimed,
    "kept_live": kept_live,
    "kept_recent": kept_recent,
  }
