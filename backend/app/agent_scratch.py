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
sits beside in the runner, and it persists across that chat's turns: an agent
routinely prepares files, ends its turn on an owner question, and continues
from them next turn. The hourly sweep releases a chat's scratch only once no
run is in flight and nothing in it has changed for a day, and it clears loose
files left in the scratch root on the same schedule. When disk pressure
blocks a new turn, admission sweeps once with the short start-race grace.
"""

import asyncio
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from app.browser_profiles import browser_session_targets_for_chat
from app.config import agent_scratch_root
from app.database import SessionLocal
from app.run_state import has_running_run
from app.runner_registry import registry

log = logging.getLogger(__name__)

# A chat's scratch outlives its turns; it is released after this long with
# no change anywhere inside it and no run in flight.
IDLE_RETENTION_SECONDS = 24 * 60 * 60
# A run's row is created around the same moment its scratch is, and the two
# are not ordered against each other, so even the disk-pressure sweep spares
# scratch touched this recently: a starting run may not have registered yet.
START_RACE_GRACE_SECONDS = 15 * 60
_RELEASED_DIR = re.compile(
  r"^\.[A-Za-z0-9_-]+\.released-[0-9a-f]{32}$"
)


def _dir_name(chat_id: str) -> str:
  return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")


def scratch_for_chat(chat_id: str) -> Path:
  """Create and return the scratch directory this chat's agents may use."""
  path = agent_scratch_root() / _dir_name(chat_id)
  path.mkdir(parents=True, exist_ok=True)
  return path


def _detach_if_idle(chat_id: str) -> Path | None:
  """Atomically release one canonical scratch name if no run owns it.

  The caller holds the per-chat queue lock. There must be no await between
  the in-memory starting check, durable running check, and rename: programmatic
  starts claim the registry before their ChatRun row exists and rely on that
  uninterrupted sequence rather than taking the queue lock themselves.
  """
  if not chat_id or registry.is_alive(chat_id):
    return None
  with SessionLocal() as db:
    if has_running_run(db, chat_id):
      return None
  browser_scan = browser_session_targets_for_chat(chat_id)
  if not browser_scan.idle:
    return None

  path = agent_scratch_root() / _dir_name(chat_id)
  if not path.is_dir():
    return None
  detached = path.with_name(f".{path.name}.released-{uuid.uuid4().hex}")
  try:
    path.rename(detached)
  except OSError as exc:
    log.warning("agent scratch release skipped %s: %s", path.name, exc)
    return None
  return detached


def _reclaim_detached(path: Path) -> int:
  size = 0
  for candidate in path.rglob("*"):
    try:
      if candidate.is_file() and not candidate.is_symlink():
        size += candidate.stat().st_size
    except OSError:
      pass
  shutil.rmtree(path)
  return size


async def release_if_idle(chat_id: str) -> int | None:
  """Delete one chat's scratch when no physical run is in flight.

  Claiming is serialized with the per-chat run-start boundary. Before slow
  deletion begins, the directory is atomically detached from its canonical
  name, so cancellation can safely release the lock while the worker finishes
  and a successor recreates scratch. Returns reclaimed bytes, including zero
  for an empty released directory, or ``None`` when a live owner kept it.
  """
  if not chat_id:
    return None
  from app.chat_queue import get_lock

  async with get_lock(chat_id):
    detached = _detach_if_idle(chat_id)
  if detached is None:
    return None
  try:
    return await asyncio.to_thread(_reclaim_detached, detached)
  except asyncio.CancelledError:
    raise
  except OSError as exc:
    # The canonical name is already free for the next run. The hourly sweep
    # owns physical recovery of a detached directory that could not be removed.
    log.warning("detached agent scratch cleanup skipped %s: %s", detached, exc)
    return None


def _last_change(path: Path) -> float:
  """Newest modification time anywhere under ``path`` (scratch is small)."""
  newest = path.lstat().st_mtime
  for candidate in path.rglob("*"):
    try:
      newest = max(newest, candidate.lstat().st_mtime)
    except OSError:
      pass
  return newest


async def sweep_idle_scratch(
  *, now: float | None = None, idle_seconds: float = IDLE_RETENTION_SECONDS,
) -> dict:
  """Delete scratch unchanged for ``idle_seconds`` whose chat has no run in flight.

  Loose files in the scratch root follow the same age rule. Returns a summary
  so the runtime retention supervisor can report what it reclaimed. Failure
  to remove one entry must not prevent the rest, so errors are logged rather
  than aborting the sweep.
  """
  root = agent_scratch_root()
  if not root.is_dir():
    return {"removed": 0, "bytes": 0, "kept_recent": 0}

  cutoff = (time.time() if now is None else now) - idle_seconds
  removed = reclaimed = kept_recent = 0

  for entry in list(root.iterdir()):
    if not entry.is_dir() or entry.is_symlink():
      try:
        stat = entry.lstat()
        if stat.st_mtime > cutoff:
          kept_recent += 1
          continue
        entry.unlink()
      except OSError as exc:
        log.warning("agent scratch sweep skipped %s: %s", entry.name, exc)
        continue
      removed += 1
      reclaimed += stat.st_size
      continue
    if _RELEASED_DIR.fullmatch(entry.name):
      try:
        reclaimed += await asyncio.to_thread(_reclaim_detached, entry)
      except OSError as exc:
        log.warning("detached agent scratch sweep skipped %s: %s", entry, exc)
        continue
      removed += 1
      continue
    try:
      if await asyncio.to_thread(_last_change, entry) > cutoff:
        kept_recent += 1
        continue
    except OSError as exc:
      log.warning("agent scratch sweep skipped %s: %s", entry.name, exc)
      continue
    size = await release_if_idle(entry.name)
    if size is None:
      continue
    removed += 1
    reclaimed += size

  if removed:
    log.info(
      "agent scratch swept dirs=%d bytes=%d kept_recent=%d",
      removed, reclaimed, kept_recent,
    )
  return {
    "removed": removed,
    "bytes": reclaimed,
    "kept_recent": kept_recent,
  }
