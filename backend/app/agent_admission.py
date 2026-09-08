"""Last-resort admission guard for provider turns under real resource pressure."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from app.resource_pressure import MIB, resource_status


class AgentTurnDeferred(RuntimeError):
  """Starting another provider turn would endanger durable chat data.

  ``resource`` names what the turn is waiting for (``memory`` or ``storage``)
  so the caller can park it for automatic continuation once that resource is
  back rather than failing it.
  """

  def __init__(self, message: str, *, resource: str) -> None:
    super().__init__(message)
    self.resource = resource


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
  if not isinstance(ratio, (int, float)):
    return None
  if not isinstance(critical_at, (int, float)):
    critical_at = 0.90
  if ratio < critical_at:
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
  free = _section(status, "facts", "disk").get("free_bytes")
  threshold = disk.get("critical_below_bytes")
  if not isinstance(free, int):
    return "critically low shared storage"
  if isinstance(threshold, int):
    return (
      f"critically low shared storage ({free // MIB} MiB free; "
      f"safety floor {threshold // MIB} MiB)"
    )
  return f"critically low shared storage ({free // MIB} MiB free)"


class _Blocked(Exception):
  """One admission attempt found a resource short; carries the deferral."""

  def __init__(self, deferral: AgentTurnDeferred) -> None:
    self.deferral = deferral


def _admit(status: dict[str, Any]) -> None:
  """One attempt: defer only for measured critical memory or storage.

  Unknown telemetry fails open. Raises ``_Blocked`` with the owner-facing
  deferral when the shared pressure snapshot says a resource is critical.
  """
  memory_reason = _memory_deferral(status)
  if memory_reason is not None:
    raise _Blocked(AgentTurnDeferred(
      f"This turn is waiting for {memory_reason}. It will continue "
      "automatically when enough memory is available.",
      resource="memory",
    ))
  storage_reason = _storage_deferral(status)
  if storage_reason is not None:
    raise _Blocked(AgentTurnDeferred(
      f"This turn is waiting because Möbius has {storage_reason}. It will "
      "continue automatically after cleanup frees space.",
      resource="storage",
    ))


async def require_agent_turn_admission(
  data_dir: str | Path,
  *,
  status_reader: Callable[[str | Path], dict[str, Any]] = resource_status,
  scratch_sweeper: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> None:
  """Admit a turn, reclaiming idle scratch once before a real deferral.

  Owner lifecycle cleanup should normally keep the system above the measured
  critical boundary. Unknown telemetry fails open for developer hosts and
  unusual self-hosted runtimes.
  """
  try:
    return _admit(status_reader(data_dir))
  except _Blocked:
    pass
  if scratch_sweeper is None:
    from app.agent_scratch import sweep_idle_scratch
    scratch_sweeper = sweep_idle_scratch
  try:
    await scratch_sweeper()
  except OSError:
    # Cleanup is best-effort; the fresh snapshot below still owns admission.
    pass
  try:
    return _admit(status_reader(data_dir))
  except _Blocked as blocked:
    raise blocked.deferral from None
