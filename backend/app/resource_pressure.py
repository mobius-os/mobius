"""Current resource facts and their pressure interpretation.

This module deliberately stops before policy.  It answers two questions:

* facts: what capacity exists and how much of it is currently in use;
* pressure: whether those facts describe normal, constrained, or critical
  headroom.

Callers decide what to do.  Keeping observation and interpretation free of
job, app, notification, hosting-provider, and pricing knowledge gives later
policy and communication work one small, stable seam to consume.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from app.memory_observability import cgroup_memory_snapshot


MIB = 1024 * 1024
GIB = 1024 * MIB

# Disk thresholds combine a useful absolute floor on small volumes with a
# bounded proportional margin on larger ones.  The caps prevent a large
# self-hosted disk from being labelled constrained merely because ten percent
# of it is many gigabytes.
_DISK_CONSTRAINED_FRACTION = 0.10
_DISK_CONSTRAINED_FLOOR = 64 * MIB
_DISK_CONSTRAINED_CEILING = 2 * GIB
_DISK_CRITICAL_FRACTION = 0.05
_DISK_CRITICAL_FLOOR = 32 * MIB
_DISK_CRITICAL_CEILING = 1 * GIB

# memory.current includes reclaimable file cache, so pressure is based on the
# existing cgroup working-set estimate.  PSI catches sustained contention that
# a single ratio snapshot can miss.  PSI averages are percentages of wall time.
_MEMORY_CONSTRAINED_RATIO = 0.75
_MEMORY_CRITICAL_RATIO = 0.90
_MEMORY_CONSTRAINED_SOME_AVG60 = 1.0
_MEMORY_CONSTRAINED_FULL_AVG60 = 0.5
_MEMORY_CRITICAL_SOME_AVG60 = 10.0
_MEMORY_CRITICAL_FULL_AVG60 = 2.0

_STATE_RANK = {
  "unknown": -1,
  "normal": 0,
  "constrained": 1,
  "critical": 2,
}


def _bounded_headroom(
  total_bytes: int,
  *,
  fraction: float,
  floor_bytes: int,
  ceiling_bytes: int,
) -> int:
  """Return a capacity-relative threshold bounded for tiny and large disks."""
  threshold = max(floor_bytes, int(total_bytes * fraction))
  return min(total_bytes, threshold, ceiling_bytes)


def _memory_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
  """Select the stable cgroup facts used by pressure and future consumers."""
  keys = (
    "available",
    "current_bytes",
    "working_set_bytes",
    "limit_bytes",
    "swap_current_bytes",
    "anon_bytes",
    "file_bytes",
    "inactive_file_bytes",
    "active_file_bytes",
    "kernel_bytes",
    "slab_bytes",
    "pressure",
  )
  return {key: snapshot.get(key) for key in keys if key in snapshot}


def resource_facts(
  data_dir: str | Path,
  *,
  disk_usage: Callable[[str | Path], Any] = shutil.disk_usage,
  memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Read one cheap point-in-time capacity snapshot.

  ``memory`` lets an existing caller reuse a cgroup snapshot it already read.
  No directory walk, process inventory, heap traversal, or sampling loop is
  implied by this function.
  """
  captured_at = datetime.now(UTC).isoformat()
  try:
    usage = disk_usage(data_dir)
    disk = {
      "available": True,
      "path": str(data_dir),
      "total_bytes": int(usage.total),
      "used_bytes": int(usage.used),
      "free_bytes": int(usage.free),
    }
  except OSError:
    disk = {
      "available": False,
      "path": str(data_dir),
    }

  memory_snapshot = (
    cgroup_memory_snapshot() if memory is None else memory
  )
  return {
    "captured_at": captured_at,
    "disk": disk,
    "memory": _memory_facts(memory_snapshot),
  }


def _disk_pressure(disk: dict[str, Any]) -> dict[str, Any]:
  if not disk.get("available"):
    return {
      "state": "unknown",
      "reason": {
        "resource": "disk",
        "code": "disk_facts_unavailable",
      },
    }
  try:
    total = int(disk["total_bytes"])
    free = int(disk["free_bytes"])
  except (KeyError, TypeError, ValueError):
    return {
      "state": "unknown",
      "reason": {
        "resource": "disk",
        "code": "disk_facts_unavailable",
      },
    }
  if total <= 0 or free < 0:
    return {
      "state": "unknown",
      "reason": {
        "resource": "disk",
        "code": "disk_facts_unavailable",
      },
    }

  constrained_below = _bounded_headroom(
    total,
    fraction=_DISK_CONSTRAINED_FRACTION,
    floor_bytes=_DISK_CONSTRAINED_FLOOR,
    ceiling_bytes=_DISK_CONSTRAINED_CEILING,
  )
  critical_below = _bounded_headroom(
    total,
    fraction=_DISK_CRITICAL_FRACTION,
    floor_bytes=_DISK_CRITICAL_FLOOR,
    ceiling_bytes=_DISK_CRITICAL_CEILING,
  )
  state = "normal"
  reason = None
  if free < critical_below:
    state = "critical"
    reason = {
      "resource": "disk",
      "code": "disk_free_below_critical",
      "observed_bytes": free,
      "threshold_bytes": critical_below,
    }
  elif free < constrained_below:
    state = "constrained"
    reason = {
      "resource": "disk",
      "code": "disk_free_below_constrained",
      "observed_bytes": free,
      "threshold_bytes": constrained_below,
    }
  return {
    "state": state,
    "free_ratio": free / total,
    "constrained_below_bytes": constrained_below,
    "critical_below_bytes": critical_below,
    "reason": reason,
  }


def app_install_storage_budget(data_dir: str | Path) -> dict[str, int]:
  """Return writable app-install headroom while preserving the disk margin.

  App packages share the data volume with the database and owner content.  The
  constrained-pressure threshold is therefore the quota boundary: installers
  may consume currently free bytes above it, but never the safety margin itself.
  This is a cheap statvfs snapshot, not a directory walk.
  """
  usage = shutil.disk_usage(data_dir)
  reserve = _bounded_headroom(
    int(usage.total),
    fraction=_DISK_CONSTRAINED_FRACTION,
    floor_bytes=_DISK_CONSTRAINED_FLOOR,
    ceiling_bytes=_DISK_CONSTRAINED_CEILING,
  )
  free = int(usage.free)
  return {
    "total_bytes": int(usage.total),
    "free_bytes": free,
    "reserve_bytes": reserve,
    "available_bytes": max(0, free - reserve),
  }


def _float(value: Any) -> float:
  try:
    return float(value)
  except (TypeError, ValueError):
    return 0.0


def _memory_pressure(memory: dict[str, Any]) -> dict[str, Any]:
  if not memory.get("available"):
    return {
      "state": "unknown",
      "reason": {
        "resource": "memory",
        "code": "memory_facts_unavailable",
      },
    }
  try:
    working_set = int(memory["working_set_bytes"])
    limit = int(memory["limit_bytes"])
  except (KeyError, TypeError, ValueError):
    return {
      "state": "unknown",
      "reason": {
        "resource": "memory",
        "code": "memory_limit_unavailable",
      },
    }
  if working_set < 0 or limit <= 0:
    return {
      "state": "unknown",
      "reason": {
        "resource": "memory",
        "code": "memory_limit_unavailable",
      },
    }

  ratio = working_set / limit
  pressure = memory.get("pressure")
  pressure = pressure if isinstance(pressure, dict) else {}
  some = pressure.get("some")
  full = pressure.get("full")
  some = some if isinstance(some, dict) else {}
  full = full if isinstance(full, dict) else {}
  some_avg60 = _float(some.get("avg60"))
  full_avg60 = _float(full.get("avg60"))

  state = "normal"
  reason = None
  if (
    ratio >= _MEMORY_CRITICAL_RATIO
    or some_avg60 >= _MEMORY_CRITICAL_SOME_AVG60
    or full_avg60 >= _MEMORY_CRITICAL_FULL_AVG60
  ):
    state = "critical"
    reason = {
      "resource": "memory",
      "code": "memory_pressure_critical",
      "working_set_ratio": ratio,
      "some_avg60": some_avg60,
      "full_avg60": full_avg60,
    }
  elif (
    ratio >= _MEMORY_CONSTRAINED_RATIO
    or some_avg60 >= _MEMORY_CONSTRAINED_SOME_AVG60
    or full_avg60 >= _MEMORY_CONSTRAINED_FULL_AVG60
  ):
    state = "constrained"
    reason = {
      "resource": "memory",
      "code": "memory_pressure_constrained",
      "working_set_ratio": ratio,
      "some_avg60": some_avg60,
      "full_avg60": full_avg60,
    }
  return {
    "state": state,
    "working_set_ratio": ratio,
    "headroom_bytes": max(0, limit - working_set),
    "some_avg60": some_avg60,
    "full_avg60": full_avg60,
    "constrained_at_ratio": _MEMORY_CONSTRAINED_RATIO,
    "critical_at_ratio": _MEMORY_CRITICAL_RATIO,
    "reason": reason,
  }


def assess_memory_pressure(
  memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Assess memory alone, for callers that must not react to disk pressure.

  Without a cgroup limit there is nothing to be constrained relative to, so
  the result is ``unknown`` and callers fail open rather than treating missing
  telemetry as pressure.
  """
  snapshot = cgroup_memory_snapshot() if memory is None else memory
  return _memory_pressure(_memory_facts(snapshot))


def assess_resource_pressure(facts: dict[str, Any]) -> dict[str, Any]:
  """Interpret resource facts without deciding what the platform should do."""
  disk = _disk_pressure(
    facts.get("disk") if isinstance(facts.get("disk"), dict) else {},
  )
  memory = _memory_pressure(
    facts.get("memory") if isinstance(facts.get("memory"), dict) else {},
  )
  assessments = (disk, memory)
  known = [
    assessment["state"]
    for assessment in assessments
    if assessment["state"] != "unknown"
  ]
  state = (
    max(known, key=lambda value: _STATE_RANK[value])
    if known else "unknown"
  )
  reasons = [
    assessment["reason"]
    for assessment in assessments
    if assessment.get("reason") is not None
  ]
  return {
    "state": state,
    "reasons": reasons,
    "disk": disk,
    "memory": memory,
  }


def resource_status(
  data_dir: str | Path,
  *,
  disk_usage: Callable[[str | Path], Any] = shutil.disk_usage,
  memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Return the stable facts/pressure seam future policy can consume."""
  facts = resource_facts(
    data_dir,
    disk_usage=disk_usage,
    memory=memory,
  )
  return {
    "facts": facts,
    "pressure": assess_resource_pressure(facts),
  }
