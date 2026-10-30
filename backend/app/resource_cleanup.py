"""Owner-run cleanup of disposable agent files, measured before it runs.

The Settings "Storage and Memory" box uses this when agents cannot start, so
nothing here may need an agent. Each target delegates to the module that
already owns that folder's retention (scratch sweep, browser-profile quota,
provider-session retention); this module only runs it now, at the owner's
request, and reports what it freed. A preview uses the same selection rules,
so the confirm step states what the cleanup then deletes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.capacity import remeasure_domains
from app.settled import run_settled

log = logging.getLogger(__name__)

# Old provider sessions only cost a warm resume (Möbius reseeds a fresh session
# from the chat's own transcript), so the owner's Clear keeps just two days.
SESSION_KEEP_DAYS = 2


class CleanupBusy(Exception):
  """Another owner cleanup is still running."""


@dataclass(frozen=True)
class CleanupTarget:
  # Capacity domains this cleanup shrinks; re-measured after it runs.
  domains: tuple[str, ...]
  preview: Callable[[str], Awaitable[dict]]
  clear: Callable[[str], Awaitable[dict]]


def _alive_chat_ids() -> set[str]:
  """Chats with a live or starting runner, read on the event loop."""
  from app.runner_registry import registry

  return registry.all_alive_chat_ids()


def _still_in_use(chat_id: str) -> bool:
  """A late re-check from a worker thread; an unreadable registry counts as in use."""
  from app.runner_registry import registry

  try:
    return registry.is_alive(chat_id)
  except RuntimeError:
    return True


async def _preview_work_files(_data_dir: str) -> dict:
  from app.agent_scratch import START_RACE_GRACE_SECONDS, idle_scratch_usage

  usage = await asyncio.to_thread(
    idle_scratch_usage, idle_seconds=START_RACE_GRACE_SECONDS,
    alive_chat_ids=_alive_chat_ids(),
  )
  return {"bytes": usage["bytes"], "items": usage["chats"], "kept": usage["kept_chats"]}


async def _clear_work_files(_data_dir: str) -> dict:
  # The disk-pressure sweep: every chat with no turn in flight and no change in
  # the last few minutes, the same rule admission applies when storage is short.
  from app.agent_scratch import START_RACE_GRACE_SECONDS, sweep_idle_scratch

  result = await sweep_idle_scratch(idle_seconds=START_RACE_GRACE_SECONDS)
  return {"bytes": result["bytes"], "items": result["removed"]}


def _sessions_in_use(alive_chat_ids: set[str]) -> frozenset[str]:
  """Provider session ids of chats that are running or starting.

  Such a chat may resume a session older than the cutoff at any moment, so
  its session is never cleared from under it.
  """
  from app import models
  from app.database import SessionLocal
  from app.run_state import running_chat_ids

  with SessionLocal() as db:
    chat_ids = alive_chat_ids | running_chat_ids(db)
    if not chat_ids:
      return frozenset()
    linked = db.query(models.ChatSessionLink.session_id).filter(
      models.ChatSessionLink.chat_id.in_(chat_ids),
    ).all()
    latest = db.query(models.Chat.session_id).filter(
      models.Chat.id.in_(chat_ids), models.Chat.session_id.isnot(None),
    ).all()
  return frozenset(row[0] for row in [*linked, *latest])


async def _preview_agent_sessions(data_dir: str) -> dict:
  from app.provider_session_retention import stale_session_usage

  usage = await asyncio.to_thread(
    stale_session_usage, data_dir, max_age_days=SESSION_KEEP_DAYS,
    in_use=_sessions_in_use(_alive_chat_ids()),
  )
  return {"bytes": usage["bytes"], "items": usage["sessions"]}


def _session_owners(session_ids: list[str]) -> dict[str, str]:
  """The chat that can resume each provider session, when there is one."""
  from app import models
  from app.database import SessionLocal

  if not session_ids:
    return {}
  with SessionLocal() as db:
    linked = db.query(models.ChatSessionLink.session_id, models.ChatSessionLink.chat_id).filter(
      models.ChatSessionLink.session_id.in_(session_ids),
    ).all()
    latest = db.query(models.Chat.session_id, models.Chat.id).filter(
      models.Chat.session_id.in_(session_ids),
    ).all()
  return {session_id: chat_id for session_id, chat_id in [*linked, *latest]}


def _chat_turn_running(chat_id: str) -> bool:
  """A live or starting runner, or a durable running turn, for this chat.

  Starts claim the registry before their run row exists, so checking both with
  no await in between leaves no gap (the rule Chat work files rely on too).
  """
  from app.database import SessionLocal
  from app.run_state import has_running_run
  from app.runner_registry import registry

  if registry.is_alive(chat_id):
    return True
  with SessionLocal() as db:
    return has_running_run(db, chat_id)


async def _clear_agent_sessions(data_dir: str) -> dict:
  from app.chat_queue import get_lock
  from app.provider_session_retention import (
    detach_claude_session,
    reclaim_released_claude_sessions,
    stale_claude_sessions,
    sweep_stale_provider_sessions,
  )

  # Clears run under the cleanup lock; a cancelled request must not release
  # it while its thread is still deleting, so each step settles first.
  stale = await run_settled(stale_claude_sessions, data_dir, max_age_days=SESSION_KEEP_DAYS)
  owners = await run_settled(_session_owners, [transcript.stem for transcript, _side, _size in stale])
  by_chat: dict[str | None, list] = {}
  for transcript, side, _size in stale:
    by_chat.setdefault(owners.get(transcript.stem), []).append((transcript, side))
  removed = 0
  for chat_id, sessions in by_chat.items():
    if chat_id is None:
      # No chat can resume a session it is not linked to.
      removed += sum(detach_claude_session(*session) is not None for session in sessions)
      continue
    # A starting turn claims this chat's run-start lock. With no await between
    # the check and the moves, a turn either started first and keeps its
    # sessions, or starts afterwards and finds none to resume.
    async with get_lock(chat_id):
      if _chat_turn_running(chat_id):
        continue
      removed += sum(detach_claude_session(*session) is not None for session in sessions)
  freed = await run_settled(reclaim_released_claude_sessions, data_dir)
  codex = await run_settled(sweep_stale_provider_sessions, data_dir, max_age_days=SESSION_KEEP_DAYS)
  outcome = {
    "bytes": freed + codex["reclaimed_bytes"] + codex.get("store_reclaimed_bytes", 0),
    "items": removed + codex["removed_files"],
  }
  if codex["status"] == "skipped_active":
    outcome["note"] = "Codex is busy, so its old copies stay until it is idle."
  return outcome


def _browser_inputs() -> tuple[dict, set[str]]:
  from app.browser_profiles import chat_activity_snapshot
  from app.database import SessionLocal

  with SessionLocal() as db:
    chats = chat_activity_snapshot(db)
  return chats, _alive_chat_ids()


async def _preview_agent_browsers(data_dir: str) -> dict:
  from app.browser_profiles import closed_chat_profile_usage

  chats, alive = _browser_inputs()
  usage = await asyncio.to_thread(closed_chat_profile_usage, data_dir, chats, alive)
  return {"bytes": usage["bytes"], "items": usage["profiles"]}


async def _clear_agent_browsers(data_dir: str) -> dict:
  from app.browser_profiles import clear_closed_chat_profiles

  chats, alive = _browser_inputs()
  result = await run_settled(
    clear_closed_chat_profiles, data_dir, chats, alive, still_in_use=_still_in_use,
  )
  return {"bytes": result["bytes"], "items": result["profiles"]}


CLEANUP_TARGETS: dict[str, CleanupTarget] = {
  "work_files": CleanupTarget(("agent_scratch",), _preview_work_files, _clear_work_files),
  "agent_sessions": CleanupTarget(
    ("provider_telemetry_claude", "provider_telemetry_codex"),
    _preview_agent_sessions, _clear_agent_sessions,
  ),
  "agent_browsers": CleanupTarget(
    ("browser_profiles",), _preview_agent_browsers, _clear_agent_browsers,
  ),
}

_CLEANUP_LOCK = asyncio.Lock()


async def preview_cleanup(target_id: str, data_dir: str | Path) -> dict:
  target = CLEANUP_TARGETS[target_id]
  return {"target": target_id, **await target.preview(str(data_dir))}


async def run_cleanup(target_id: str, data_dir: str | Path) -> dict:
  """Run one owner cleanup, then re-measure the domains it shrank.

  One at a time: two overlapping cleanups would race over the same folders and
  report each other's bytes.
  """
  target = CLEANUP_TARGETS[target_id]
  if _CLEANUP_LOCK.locked():
    raise CleanupBusy(target_id)
  async with _CLEANUP_LOCK:
    outcome = await target.clear(str(data_dir))
    try:
      await run_settled(remeasure_domains, data_dir, list(target.domains))
    except Exception:
      # The files are already gone; report the clear even if sizing failed.
      log.warning("re-measure after %s cleanup failed", target_id, exc_info=True)
  return {"target": target_id, **outcome}
