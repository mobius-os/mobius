"""Last-resort admission guard for provider turns under real resource pressure."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any, Awaitable, Callable

from app.resource_pressure import MIB, resource_status


# A just-admitted turn has not raised ``pids.current`` yet: its CLI, MCP servers
# and tools start over the following seconds. Without a hold, a burst of
# simultaneous sends or helper spawns all read the same pre-burst count and all
# start. Each recently admitted turn therefore holds this estimated budget
# until it ends or has had time to start, by which point the measured count
# includes it.
TASKS_PER_STARTING_TURN = 40
START_HOLD_SECONDS = 60.0


class AgentTurnDeferred(RuntimeError):
  """Starting another provider turn would endanger durable chat data.

  ``resource`` names what the turn is waiting for (``memory``, ``storage`` or
  ``processes``) so the caller can park it for automatic continuation once
  that resource is back rather than failing it.
  """

  def __init__(self, message: str, *, resource: str) -> None:
    super().__init__(message)
    self.resource = resource


class TurnStartHold:
  """One admitted turn's share of the process budget while it starts."""

  def __init__(self, holds: "TurnStartHolds") -> None:
    self._holds = holds

  def release(self) -> None:
    """End the hold early, for a turn that finished or failed while starting.

    Idempotent; an unreleased hold still lapses after ``START_HOLD_SECONDS``.
    """
    self._holds._release(self)


class TurnStartHolds:
  """Admitted turns that may not show in the measured task count yet."""

  def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
    self._clock = clock
    self._lock = threading.Lock()
    self._admitted_at: dict[TurnStartHold, float] = {}

  def starting_turns(self) -> int:
    with self._lock:
      cutoff = self._clock() - START_HOLD_SECONDS
      for hold, admitted_at in list(self._admitted_at.items()):
        if admitted_at <= cutoff:
          del self._admitted_at[hold]
      return len(self._admitted_at)

  def hold(self) -> TurnStartHold:
    hold = TurnStartHold(self)
    with self._lock:
      self._admitted_at[hold] = self._clock()
    return hold

  def _release(self, hold: TurnStartHold) -> None:
    with self._lock:
      self._admitted_at.pop(hold, None)


START_HOLDS = TurnStartHolds()


def _section(status: dict[str, Any], *path: str) -> dict[str, Any]:
  node: Any = status
  for key in path:
    node = node.get(key) if isinstance(node, dict) else None
  return node if isinstance(node, dict) else {}


def _memory_deferral(status: dict[str, Any]) -> str | None:
  """Why a new turn must wait for memory, or ``None`` when it may start.

  PSI is a retrospective contention signal: an avg60 threshold can remain
  tripped after the stall has cleared, and by itself does not imply OOM risk.
  Keep PSI visible in resource diagnostics, but admission defers only when the
  point-in-time unreclaimable working set is at its critical ratio.
  """
  memory = _section(status, "pressure", "memory")
  ratio = memory.get("working_set_ratio")
  critical_at = memory.get("critical_at_ratio")
  if (
    not isinstance(ratio, (int, float))
    or not isinstance(critical_at, (int, float))
    or ratio < critical_at
  ):
    return None
  return (
    "memory headroom because unreclaimable footprint is "
    f"{ratio:.0%} of the limit (threshold {critical_at:.0%})"
  )


def _storage_deferral(status: dict[str, Any]) -> str | None:
  """Why measured disk pressure requires a pause, or ``None`` otherwise.

  The shared pressure snapshot already owns the installation-aware critical
  floor. Admission deliberately does not multiply a guessed per-turn growth
  allowance by the number of active chats: that turned healthy free space
  into a hidden concurrency cap and reported contradictory owner-facing
  numbers. Cleanup and the next fresh snapshot decide whether the real floor
  is still crossed.
  """
  disk = _section(status, "pressure", "disk")
  if disk.get("state") != "critical":
    return None
  # A critical disk assessment always carries the parsed free bytes and the
  # floor it was compared against (resource_pressure._disk_pressure).
  free = _section(status, "facts", "disk")["free_bytes"]
  threshold = disk["critical_below_bytes"]
  return (
    f"critically low shared storage ({free // MIB} MiB free; "
    f"safety floor {threshold // MIB} MiB)"
  )


def _process_deferral(status: dict[str, Any], *, starting_turns: int) -> str | None:
  """Why the container's process/thread cap requires a pause, or ``None``.

  Past the cgroup task cap no process can start a thread, so the server's own
  routes fail along with every running turn. Holding new turns short of the cap
  lets running work finish instead of taking the whole instance down. Turns
  admitted but still starting count at ``TASKS_PER_STARTING_TURN`` each, since
  the measured count does not include them yet.
  """
  processes = _section(status, "pressure", "processes")
  current = processes.get("current_tasks")
  limit = processes.get("limit_tasks")
  critical_at = processes.get("critical_at_ratio")
  if processes.get("state") != "critical" and not (
    starting_turns
    and isinstance(current, int)
    and isinstance(limit, int)
    and isinstance(critical_at, (int, float))
    and limit > 0
    and (current + starting_turns * TASKS_PER_STARTING_TURN) / limit >= critical_at
  ):
    return None
  reason = f"{current} of its {limit} processes and threads in use"
  if starting_turns:
    noun = "turn" if starting_turns == 1 else "turns"
    reason += f" and {starting_turns} more agent {noun} still starting"
  return reason


def _deferral(
  status: dict[str, Any],
  *,
  allow_low_storage: bool = False,
  starting_turns: int,
) -> AgentTurnDeferred | None:
  """One attempt: the owner-facing deferral for measured critical memory,
  storage, or process headroom, or ``None`` when the turn may start. Unknown
  telemetry fails open."""
  memory_reason = _memory_deferral(status)
  if memory_reason is not None:
    return AgentTurnDeferred(
      f"This turn is waiting for {memory_reason}. Finish or stop other agent "
      "work if you want to free headroom sooner; it will continue "
      "automatically when memory settles.",
      resource="memory",
    )
  storage_reason = None if allow_low_storage else _storage_deferral(status)
  if storage_reason is not None:
    return AgentTurnDeferred(
      f"This turn is waiting because Möbius has {storage_reason}. It will "
      "continue automatically after cleanup frees space.",
      resource="storage",
    )
  process_reason = _process_deferral(status, starting_turns=starting_turns)
  if process_reason is not None:
    return AgentTurnDeferred(
      f"This turn is waiting because Möbius has {process_reason}. Finish or "
      "stop other agent work to free them sooner; it will continue "
      "automatically once enough are free. If no other agent work is "
      "running, a leftover browser or tool server may be holding them.",
      resource="processes",
    )
  return None


async def require_agent_turn_admission(
  data_dir: str | Path,
  *,
  status_reader: Callable[[str | Path], dict[str, Any]] = resource_status,
  scratch_sweeper: Callable[[], Awaitable[dict[str, Any]]] | None = None,
  allow_low_storage: bool = False,
  holds: TurnStartHolds | None = None,
) -> TurnStartHold:
  """Admit a turn or raise its owner-facing :class:`AgentTurnDeferred`.

  The returned hold counts the turn against the process budget while it
  starts; release it when the turn ends. Admission runs on the event loop and
  nothing awaits between the final measurement and taking the hold, so a burst
  of concurrent admissions each sees the turns admitted before it.
  """
  holds = START_HOLDS if holds is None else holds
  deferral = await agent_turn_deferral(
    data_dir, status_reader=status_reader, scratch_sweeper=scratch_sweeper,
    allow_low_storage=allow_low_storage, holds=holds,
  )
  if deferral is not None:
    raise deferral
  return holds.hold()


async def agent_turn_deferral(
  data_dir: str | Path,
  *,
  status_reader: Callable[[str | Path], dict[str, Any]] = resource_status,
  scratch_sweeper: Callable[[], Awaitable[dict[str, Any]]] | None = None,
  allow_low_storage: bool = False,
  holds: TurnStartHolds | None = None,
) -> AgentTurnDeferred | None:
  """Why a new turn must wait now, reclaiming idle scratch once first.

  The continuation sweep asks this before resuming a resource-parked turn, so
  a wait that is still short stays one parked run instead of starting a turn
  that only re-parks and saves another identical waiting card.

  Owner lifecycle cleanup should normally keep the system above the measured
  critical boundary. Unknown telemetry fails open for developer hosts and
  unusual self-hosted runtimes.
  """
  holds = START_HOLDS if holds is None else holds
  status = status_reader(data_dir)
  # One warned owner continuation may start below the disk floor.
  # Memory admission still applies; this is not a global resource bypass.
  deferral = _deferral(
    status, allow_low_storage=allow_low_storage,
    starting_turns=holds.starting_turns(),
  )
  if deferral is None:
    return None
  if _storage_deferral(status) is None:
    # Deleting paused chats' scratch frees no memory.
    return deferral
  if scratch_sweeper is None:
    from app.agent_scratch import START_RACE_GRACE_SECONDS, sweep_idle_scratch

    async def scratch_sweeper():
      # Disk pressure outranks keeping paused chats' scratch for a day.
      return await sweep_idle_scratch(idle_seconds=START_RACE_GRACE_SECONDS)
  try:
    await scratch_sweeper()
  except OSError:
    # Cleanup is an opportunistic last chance, not the admission decision.
    # A filesystem race or permission issue must not turn a truthful resource
    # wait into an unrelated setup failure; the fresh measurement below still
    # owns whether the turn may start.
    pass
  return _deferral(
    status_reader(data_dir), allow_low_storage=allow_low_storage,
    starting_turns=holds.starting_turns(),
  )
