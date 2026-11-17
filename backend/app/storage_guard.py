"""Keep a filling data volume recoverable.

Turn admission alone cannot do this: it is asked only when a turn starts, so
an install or download already running inside a turn could fill the volume to
the last byte, and at that point nothing can commit — not the owner's next
message, not a Continue anyway, not even the record that a turn is waiting.

The guard watches free space on a short tick and acts in order, each step
only if the previous one was not enough (thresholds from resource_pressure):

* below *constrained*: reclaim what Möbius already treats as disposable
  (agent scratch idle past its ordinary retention, provider session logs past
  theirs), once per episode;
* below *critical*: stop the commands agent runs and app jobs started and
  pause their turns into a durable storage wait that resumes by itself,
  except turns the owner continued anyway from that wait; reclaim once more,
  now also scratch idle for minutes rather than a day;
* below *exhausted*: release the recovery reserve so the database can commit
  and the cleanup has room to work. A continued turn keeps running, and its
  commands are stopped only when that buys something: on a tick that is about
  to release the reserve, so they cannot take it, or while free space is still
  falling, so they may be what fills the volume. Otherwise they are the
  owner's cleanup, which is the point of Continue anyway;
* once free space is comfortable again: re-arm the reserve.

The state lives in the volume itself (free bytes, whether the reserve file
exists) plus in-memory episode flags; none of it is written to the database.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import disk_reserve
from app.resource_pressure import MIB, disk_status

log = logging.getLogger(__name__)

# One statvfs per tick costs microseconds. Under pressure the guard looks
# every second: below the critical line a fast writer would otherwise have
# several seconds to consume the remaining headroom.
NORMAL_TICK_SECS = 5.0
PRESSURE_TICK_SECS = 1.0
# A failed arm (no room, a write error) is retried this rarely, so a volume
# that cannot hold a reserve yet is not rewritten every tick. A filesystem that
# cannot hold one at all is not retried while this process runs.
ARM_RETRY_SECS = 600.0
# Below the exhausted line a continued turn's commands are stopped while free
# space keeps falling. The turn's own transcript and the database still write
# a little every tick; a drop smaller than this between ticks is that, not a
# writer filling the volume.
FALLING_TOLERANCE_BYTES = 1 * MIB

def disk_level(disk: dict[str, Any], free_bytes: int) -> str:
  """Place ``free_bytes`` on the ladder of a disk pressure assessment."""
  if disk.get("state") == "unknown":
    return "normal"
  if free_bytes < disk["exhausted_below_bytes"]:
    return "exhausted"
  if free_bytes < disk["critical_below_bytes"]:
    return "critical"
  if free_bytes < disk["constrained_below_bytes"]:
    return "constrained"
  return "normal"


# ------------------------------------------------------- continued turns
#
# A turn the owner continued with "Continue anyway" from its storage wait
# (allow_low_storage, admitted in chat.run_chat) is the owner's explicit
# choice to let an agent free space. The guard keeps it running and spares its
# commands, below the exhausted line too unless stopping them buys something
# (StorageGuard.tick). The choice covers that one run: a later turn in the
# same chat is paused like any other.

_continued: dict[str, str] = {}


def _is_live_run(chat_id: str, run_token: str) -> bool:
  """Whether ``run_token`` is still the chat's live turn.

  A turn that has not registered its event sink yet still counts, so a
  continued turn is spared from its first tick.
  """
  from app.chat import get_active_sink, registry
  if not registry.is_alive(chat_id):
    return False
  sink = get_active_sink(chat_id)
  return sink is None or getattr(sink, "run_token", None) == run_token


def spare_continued_turn(chat_id: str, run_token: str) -> None:
  """Record a live turn the owner continued through a storage wait."""
  _continued[chat_id] = run_token


def continued_turns() -> dict[str, str]:
  """``chat_id -> run_token`` of continued turns that are still live."""
  for chat_id, run_token in list(_continued.items()):
    if not _is_live_run(chat_id, run_token):
      _continued.pop(chat_id, None)
  return dict(_continued)


# -------------------------------------------------------------------- effects


@dataclass
class GuardEffects:
  """What the guard does to the world. Tests substitute fakes."""

  disk_usage: Callable[[str | Path], Any] = shutil.disk_usage
  reclaim: Callable[[str, str], Awaitable[int]] | None = None
  stop_agent_commands: Callable[[frozenset[str]], int] | None = None
  pause_live_turns: Callable[[frozenset[str]], Awaitable[int]] | None = None
  notify: Callable[[str, str], Awaitable[None]] | None = None
  arm_reserve: Callable[..., disk_reserve.ArmOutcome] = disk_reserve.arm
  release_reserve: Callable[[str | Path], int] = disk_reserve.release
  reserve_held: Callable[[str | Path], int] = disk_reserve.held_bytes


async def _reclaim_disposable(data_dir: str, tier: str) -> int:
  """Delete only what existing retention already treats as disposable.

  At the ``constrained`` tier scratch keeps its ordinary day of idle
  retention, so a chat waiting on its helpers keeps its clones. At
  ``critical`` disk pressure outranks that, as it does for turn admission
  (agent_admission), and scratch idle past the start-race grace goes too.
  """
  from app.agent_scratch import (
    IDLE_RETENTION_SECONDS, START_RACE_GRACE_SECONDS, sweep_idle_scratch,
  )
  from app.provider_session_retention import sweep_stale_provider_sessions

  idle_seconds = (
    START_RACE_GRACE_SECONDS if tier == "critical" else IDLE_RETENTION_SECONDS
  )
  reclaimed = 0
  try:
    scratch = await sweep_idle_scratch(idle_seconds=idle_seconds)
    reclaimed += int(scratch.get("bytes") or 0)
  except Exception:
    log.warning("storage guard: scratch reclaim failed", exc_info=True)
  try:
    sessions = await asyncio.to_thread(sweep_stale_provider_sessions, data_dir)
    reclaimed += int(sessions.get("reclaimed_bytes") or 0)
    reclaimed += int(sessions.get("store_reclaimed_bytes") or 0)
  except Exception:
    log.warning("storage guard: provider session reclaim failed", exc_info=True)
  return reclaimed


def _stop_agent_commands(spare_markers: frozenset[str]) -> int:
  from app.process_groups import stop_agent_commands
  return stop_agent_commands(spare_markers=spare_markers)


async def _pause_live_turns(spare_chat_ids: frozenset[str]) -> int:
  from app.chat import pause_live_turns_for_storage
  return await pause_live_turns_for_storage(spare_chat_ids)


async def _notify_owner(title: str, body: str) -> None:
  from app import models, push
  from app.database import SessionLocal

  def owner_id():
    with SessionLocal() as db:
      row = db.query(models.Owner.id).first()
      return row[0] if row is not None else None

  owner = await asyncio.to_thread(owner_id)
  if owner is None:
    return
  with SessionLocal() as db:
    await push.notify_owner_async(
      db, owner, title=title, body=body, source_type="capacity",
      tag="storage-guard",
    )


def default_effects() -> GuardEffects:
  return GuardEffects(
    reclaim=_reclaim_disposable,
    stop_agent_commands=_stop_agent_commands,
    pause_live_turns=_pause_live_turns,
    notify=_notify_owner,
  )


# ---------------------------------------------------------------------- guard


@dataclass
class TickOutcome:
  level: str
  free_bytes: int | None
  reclaimed_bytes: int = 0
  commands_stopped: int = 0
  turns_paused: int = 0
  reserve_released_bytes: int = 0
  reserve: str | None = None


class StorageGuard:
  """One data volume's guard. ``tick`` is the whole policy; ``run`` loops it."""

  def __init__(
    self,
    data_dir: str | Path,
    *,
    effects: GuardEffects | None = None,
    clock: Callable[[], float] = time.monotonic,
  ) -> None:
    self.data_dir = str(data_dir)
    self.effects = effects or default_effects()
    self.clock = clock
    # Reclaim tiers already run this episode; see _reclaim_disposable.
    self._reclaimed: set[str] = set()
    # Free bytes right after the previous tick's own actions, to tell a
    # volume still filling from one a cleanup is emptying.
    self._last_free: int | None = None
    self._noticed: set[str] = set()
    self._next_arm_attempt = 0.0
    self.reserve_state: str = "unknown"
    self.reserve_detail: str = ""
    self.last: TickOutcome | None = None

  def _measure(self) -> tuple[str, int | None, dict[str, Any], int]:
    status = disk_status(self.data_dir, disk_usage=self.effects.disk_usage)
    disk = status["pressure"]
    facts = status["facts"]
    if disk.get("state") == "unknown":
      return "normal", None, disk, 0
    free = int(facts["free_bytes"])
    return disk_level(disk, free), free, disk, int(facts["total_bytes"])

  async def _notice_once(self, key: str, title: str, body: str) -> None:
    if key in self._noticed or self.effects.notify is None:
      return
    self._noticed.add(key)
    if key == "exhausted":
      # The full-storage notice already says everything the critical one
      # would; releasing the reserve must not trigger a second push.
      self._noticed.add("critical")
    try:
      await self.effects.notify(title, body)
    except Exception:
      log.warning("storage guard: owner notice failed key=%s", key, exc_info=True)

  async def tick(self) -> TickOutcome:
    outcome = await self._tick()
    # Compare the next tick against what this one left, not what it found.
    if outcome.reserve_released_bytes or outcome.reclaimed_bytes:
      self._last_free = self._measure()[1]
    else:
      self._last_free = outcome.free_bytes
    self.last = outcome
    return outcome

  async def _tick(self) -> TickOutcome:
    level, free, disk, total = self._measure()
    outcome = TickOutcome(level=level, free_bytes=free)
    if level == "normal":
      self._reclaimed.clear()
      self._noticed.clear()
      # Arming may fall back to writing incompressible bytes (seconds on a
      # slow disk), so it never runs on the event loop.
      await asyncio.to_thread(self._maybe_arm, outcome, free, disk, total)
      return outcome

    pressed = level in ("critical", "exhausted")
    if pressed:
      # Stop the writers before anything slower: reclaim and a graceful turn
      # interrupt both take seconds a fast writer would otherwise keep using.
      # A turn the owner continued anyway keeps running at both levels.
      continued = continued_turns()
      spare_chats = frozenset(continued)
      if level == "exhausted" and self._continued_commands_must_stop(free):
        spare_markers = frozenset()
      else:
        from app.process_groups import run_marker
        spare_markers = frozenset(run_marker(token) for token in continued.values())
      if self.effects.stop_agent_commands is not None:
        outcome.commands_stopped = self.effects.stop_agent_commands(spare_markers)
      if level == "exhausted":
        # Writers are stopped, so the freed space goes to the database and
        # the cleanup rather than back to whatever was filling the volume.
        outcome.reserve_released_bytes = self.effects.release_reserve(self.data_dir)
        if outcome.reserve_released_bytes:
          self.reserve_state = "released"
          log.warning(
            "storage guard released the %d MiB recovery reserve (free %s)",
            outcome.reserve_released_bytes // MIB, free,
          )

    tier = "critical" if pressed else "constrained"
    if tier not in self._reclaimed and self.effects.reclaim is not None:
      # The critical sweep covers everything the constrained one would delete.
      self._reclaimed.update(("constrained", tier))
      outcome.reclaimed_bytes = await self.effects.reclaim(self.data_dir, tier)
      if outcome.reclaimed_bytes:
        log.info(
          "storage guard reclaimed %d MiB of disposable data",
          outcome.reclaimed_bytes // MIB,
        )
        level, free, disk, total = self._measure()
        outcome.level, outcome.free_bytes = level, free

    if pressed and level in ("critical", "exhausted"):
      if self.effects.pause_live_turns is not None:
        outcome.turns_paused = await self.effects.pause_live_turns(spare_chats)
      await self._notice_once(
        level,
        "Möbius storage is full" if level == "exhausted"
        else "Möbius paused work to keep storage from filling",
        _notice_body(level, outcome),
      )
    if outcome.commands_stopped or outcome.turns_paused:
      log.warning(
        "storage guard level=%s free=%s stopped %d command(s), paused %d turn(s)",
        level, free, outcome.commands_stopped, outcome.turns_paused,
      )
    return outcome

  def _continued_commands_must_stop(self, free: int | None) -> bool:
    """Whether stopping a continued turn's commands below the exhausted line
    buys anything.

    It does while a reserve is held, since this tick releases it and the
    commands must not take it, and while free space is still falling, since
    they may be the writer. Otherwise they are most likely the cleanup the
    owner allowed, and stopping them every second would leave nothing able to
    free space.
    """
    if self.effects.reserve_held(self.data_dir) > 0:
      return True
    if free is None or self._last_free is None:
      return False
    return self._last_free - free > FALLING_TOLERANCE_BYTES

  def _maybe_arm(
    self, outcome: TickOutcome, free: int | None, disk: dict[str, Any], total: int,
  ) -> None:
    if free is None or total <= 0:
      return
    target = disk_reserve.reserve_target_bytes(total)
    held = disk_reserve.held_bytes(self.data_dir)
    if disk_reserve.holds(held, target):
      self.reserve_state = "armed"
      outcome.reserve = "armed"
      return
    # Arm only when the disk stays out of pressure afterwards; otherwise the
    # reserve would itself push the volume into the state it guards against.
    if free < disk["constrained_below_bytes"] + target - held:
      return
    now = self.clock()
    if now < self._next_arm_attempt:
      return
    result = self.effects.arm_reserve(
      self.data_dir, target_bytes=target, disk_usage=self.effects.disk_usage,
    )
    outcome.reserve = result.state
    if result.holds_reserve:
      self.reserve_state, self.reserve_detail = "armed", ""
      if result.state == "armed":
        log.info("storage guard armed a %d MiB recovery reserve", result.held_bytes // MIB)
      return
    self._next_arm_attempt = (
      math.inf if result.state == "unsupported" else now + ARM_RETRY_SECS
    )
    self.reserve_state, self.reserve_detail = (
      "unavailable" if result.state == "unsupported" else "released",
      result.detail,
    )
    log.warning(
      "storage guard could not arm the recovery reserve: %s (%s)",
      result.state, result.detail,
    )

  def interval(self) -> float:
    level = self.last.level if self.last else "normal"
    return NORMAL_TICK_SECS if level == "normal" else PRESSURE_TICK_SECS

  async def run(self) -> None:
    while True:
      try:
        await self.tick()
      except asyncio.CancelledError:
        raise
      except Exception:
        log.error("storage guard tick failed", exc_info=True)
      await asyncio.sleep(self.interval())


def _notice_body(level: str, outcome: TickOutcome) -> str:
  free = f"{(outcome.free_bytes or 0) // 1_000_000} MB free"
  paused = (
    f" Paused {outcome.turns_paused} chat(s); they continue by themselves "
    "once space is back."
    if outcome.turns_paused else ""
  )
  if level == "exhausted":
    return (
      f"Storage is almost completely full ({free}).{paused} Möbius released "
      "its recovery reserve so saving keeps working. Free up space, or use "
      "Continue anyway on a paused chat if an agent needs to do it."
    )
  return (
    f"Storage is critically low ({free}).{paused} Free up space, or use "
    "Continue anyway on a paused chat if an agent needs to do it."
  )


def release_reserve_at_boot(
  data_dir: str | Path,
  *,
  disk_usage: Callable[[str | Path], Any] = shutil.disk_usage,
) -> int:
  """At startup, give an exhausted volume its reserve back before anything writes.

  This is the running guard's own line: what lets the database commit there
  lets the boot commit too. A volume that is only critical, which can last
  days, keeps its reserve through restarts for the moment it is needed. The
  guard's first ticks handle everything else.
  """
  status = disk_status(data_dir, disk_usage=disk_usage)
  facts = status["facts"]
  if facts.get("available") is not True:
    return 0
  if disk_level(status["pressure"], int(facts["free_bytes"])) != "exhausted":
    return 0
  freed = disk_reserve.release(data_dir)
  if freed:
    log.warning(
      "startup released the %d MiB recovery reserve: the data volume is full",
      freed // MIB,
    )
  return freed


_guard: StorageGuard | None = None


def start_guard(data_dir: str | Path) -> StorageGuard:
  """Create this process's guard; ``reserve_report`` reads its reserve state."""
  global _guard
  _guard = StorageGuard(data_dir)
  return _guard


def reserve_report(data_dir: str | Path) -> dict[str, Any]:
  """The recovery reserve as the capacity status shows it.

  ``armed`` while the file holds its space; otherwise ``unavailable`` when the
  filesystem cannot hold one (with the reason) or ``released``.
  """
  held = disk_reserve.held_bytes(data_dir)
  if held:
    return {"state": "armed", "held_bytes": held, "detail": ""}
  guard = _guard
  unavailable = guard is not None and guard.reserve_state == "unavailable"
  return {
    "state": "unavailable" if unavailable else "released",
    "held_bytes": 0,
    "detail": guard.reserve_detail if guard is not None else "",
  }
