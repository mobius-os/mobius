"""Low-overhead process memory accounting and opt-in allocation profiling.

The ordinary production path reads Linux's existing process/cgroup counters
only when the authenticated debug surface asks for them, plus at a handful of
major lifecycle boundaries. It never walks the Python heap continuously.

For a controlled restart, allocation tracing can be enabled with either
``MOBIUS_MEMORY_TRACE=<frames>`` or ``$DATA_DIR/run/memory-trace``. Tracing is
off by default because tracemalloc deliberately trades CPU and RAM for source
attribution. The flag file makes one diagnostic restart possible without
changing the container environment; remove it and restart to return to the
zero-tracing production path.
"""

from __future__ import annotations

import gc
import logging
import os
import re
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any


log = logging.getLogger("moebius.memory")

_CHECKPOINT_LIMIT = 128
_checkpoints: deque[dict[str, Any]] = deque(maxlen=_CHECKPOINT_LIMIT)
_checkpoint_lock = Lock()
_trace_status: dict[str, Any] = {
  "enabled": False,
  "frames": 0,
  "source": "disabled",
}


def _read_text(path: Path) -> str | None:
  try:
    return path.read_text(encoding="utf-8")
  except (OSError, UnicodeError):
    return None


def _read_int(path: Path) -> int | None:
  value = _read_text(path)
  if value is None:
    return None
  try:
    return int(value.strip())
  except ValueError:
    return None


def _kb_fields(text: str | None) -> dict[str, int]:
  """Parse ``Name: 123 kB`` proc fields into byte counts."""
  result: dict[str, int] = {}
  for line in (text or "").splitlines():
    name, separator, raw = line.partition(":")
    if not separator:
      continue
    parts = raw.strip().split()
    if not parts:
      continue
    try:
      value = int(parts[0])
    except ValueError:
      continue
    result[name] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
  return result


def _trace_frames_from_configuration() -> tuple[int, str]:
  raw = os.getenv("MOBIUS_MEMORY_TRACE")
  source = "environment"
  if raw is None:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    flag = data_dir / "run" / "memory-trace"
    raw = _read_text(flag)
    if raw is None:
      return 0, "disabled"
    raw = raw.strip() or "1"
    source = "flag"
  normalized = raw.strip().lower()
  if normalized in {"", "0", "false", "off", "no"}:
    return 0, "disabled"
  if normalized in {"true", "on", "yes"}:
    return 1, source
  try:
    frames = int(normalized)
  except ValueError:
    return 0, f"{source}_invalid"
  if not 1 <= frames <= 50:
    return 0, f"{source}_invalid"
  return frames, source


def maybe_start_allocation_tracing() -> bool:
  """Start tracemalloc at package import only when explicitly requested."""
  frames, source = _trace_frames_from_configuration()
  if frames == 0:
    _trace_status.update(enabled=False, frames=0, source=source)
    return False
  try:
    import tracemalloc
    if not tracemalloc.is_tracing():
      tracemalloc.start(frames)
    _trace_status.update(enabled=True, frames=frames, source=source)
    return True
  except Exception:  # Profiling must never become a boot dependency.
    log.exception("allocation tracing could not start")
    _trace_status.update(enabled=False, frames=0, source="start_failed")
    return False


def tracing_status() -> dict[str, Any]:
  result = dict(_trace_status)
  if not result["enabled"]:
    return result
  try:
    import tracemalloc
    current, peak = tracemalloc.get_traced_memory()
    result.update(
      current_bytes=current,
      peak_bytes=peak,
      tracer_overhead_bytes=tracemalloc.get_tracemalloc_memory(),
    )
  except Exception:
    result["available"] = False
  return result


def _process_uptime_seconds(pid: int, proc_root: Path) -> float | None:
  stat = _read_text(proc_root / str(pid) / "stat")
  uptime = _read_text(proc_root / "uptime")
  if stat is None or uptime is None:
    return None
  try:
    # The comm field is parenthesized and may itself contain spaces.
    fields_after_comm = stat[stat.rfind(")") + 2:].split()
    start_ticks = int(fields_after_comm[19])
    system_uptime = float(uptime.split()[0])
    ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
    return max(0.0, system_uptime - start_ticks / ticks_per_second)
  except (IndexError, TypeError, ValueError, OSError):
    return None


def process_memory_snapshot(
  pid: int | None = None,
  *,
  proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
  """Return current Linux process memory without allocating a heap snapshot."""
  target_pid = int(pid or os.getpid())
  root = proc_root / str(target_pid)
  status = _kb_fields(_read_text(root / "status"))
  smaps = _kb_fields(_read_text(root / "smaps_rollup"))
  if not status and not smaps:
    return {"available": False, "pid": target_pid}
  result: dict[str, Any] = {
    "available": True,
    "pid": target_pid,
    "rss_bytes": smaps.get("Rss", status.get("VmRSS", 0)),
    "pss_bytes": smaps.get("Pss"),
    "anonymous_bytes": smaps.get("Anonymous", status.get("RssAnon", 0)),
    "private_clean_bytes": smaps.get("Private_Clean"),
    "private_dirty_bytes": smaps.get("Private_Dirty"),
    "shared_clean_bytes": smaps.get("Shared_Clean"),
    "shared_dirty_bytes": smaps.get("Shared_Dirty"),
    "swap_bytes": smaps.get("Swap", status.get("VmSwap", 0)),
    "data_bytes": status.get("VmData"),
    "threads": status.get("Threads"),
  }
  uptime = _process_uptime_seconds(target_pid, proc_root)
  if uptime is not None:
    result["uptime_seconds"] = round(uptime, 3)
  return result


def _cgroup_dir(
  *,
  proc_root: Path = Path("/proc"),
  cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Path:
  membership = _read_text(proc_root / "self" / "cgroup")
  for line in (membership or "").splitlines():
    parts = line.split(":", 2)
    if len(parts) == 3 and parts[0] == "0":
      relative = parts[2].lstrip("/")
      candidate = cgroup_root / relative
      if candidate.exists():
        return candidate
  return cgroup_root


def _pressure_fields(text: str | None) -> dict[str, dict[str, float | int]]:
  result: dict[str, dict[str, float | int]] = {}
  for line in (text or "").splitlines():
    parts = line.split()
    if not parts:
      continue
    values: dict[str, float | int] = {}
    for item in parts[1:]:
      key, separator, raw = item.partition("=")
      if not separator:
        continue
      try:
        values[key] = int(raw) if key == "total" else float(raw)
      except ValueError:
        continue
    result[parts[0]] = values
  return result


def cgroup_memory_snapshot(
  *,
  proc_root: Path = Path("/proc"),
  cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
  """Return cgroup-v2 memory split so file cache is not mistaken for heap."""
  root = _cgroup_dir(proc_root=proc_root, cgroup_root=cgroup_root)
  current = _read_int(root / "memory.current")
  if current is None:
    return {"available": False}
  stat: dict[str, int] = {}
  for line in (_read_text(root / "memory.stat") or "").splitlines():
    key, separator, raw = line.partition(" ")
    if not separator:
      continue
    try:
      stat[key] = int(raw)
    except ValueError:
      continue
  raw_limit = (_read_text(root / "memory.max") or "").strip()
  limit = None
  if raw_limit and raw_limit != "max":
    try:
      limit = int(raw_limit)
    except ValueError:
      pass
  inactive_file = stat.get("inactive_file", 0)
  return {
    "available": True,
    "current_bytes": current,
    "working_set_bytes": max(0, current - inactive_file),
    "limit_bytes": limit,
    "swap_current_bytes": _read_int(root / "memory.swap.current"),
    "anon_bytes": stat.get("anon"),
    "file_bytes": stat.get("file"),
    "inactive_file_bytes": inactive_file,
    "active_file_bytes": stat.get("active_file"),
    "kernel_bytes": stat.get("kernel"),
    "slab_bytes": stat.get("slab"),
    "pressure": _pressure_fields(_read_text(root / "memory.pressure")),
  }


def estimate_payload_bytes(value: Any, _seen: set[int] | None = None) -> int:
  """Estimate owned content bytes without serializing or retaining objects.

  This deliberately measures payload, not CPython object overhead. The
  difference between payload and process memory is itself useful evidence:
  e.g. a 3 MiB catch-up log cannot explain a 300 MiB private heap.
  """
  if value is None:
    return 0
  if isinstance(value, str):
    return len(value.encode("utf-8", errors="replace"))
  if isinstance(value, (bytes, bytearray, memoryview)):
    return len(value)
  if isinstance(value, (bool, int, float)):
    return 8
  if not isinstance(value, (dict, list, tuple, set, frozenset)):
    return 0
  seen = _seen if _seen is not None else set()
  identity = id(value)
  if identity in seen:
    return 0
  seen.add(identity)
  if isinstance(value, dict):
    return sum(
      estimate_payload_bytes(key, seen) + estimate_payload_bytes(item, seen)
      for key, item in value.items()
    )
  return sum(estimate_payload_bytes(item, seen) for item in value)


def _process_identity(
  pid: int,
  comm: str,
  cmdline: str,
) -> tuple[str, str | None]:
  """Return a stable category and a safe, useful owner label.

  Executable names alone are not ownership: installed services commonly run
  behind generic hosts such as gunicorn, and browser workers are only useful
  when tied back to their profile. Keep the label deliberately derived from
  known local paths rather than exposing arbitrary command lines, which can
  contain credentials.
  """
  if pid == os.getpid():
    return "mobius_server", "platform"
  value = f"{comm} {cmdline}".lower()
  if any(token in value for token in ("chromium", "chrome", "headless_shell")):
    match = re.search(r"agent-browser-profiles/([^/\\s]+)", cmdline)
    return "browser", match.group(1) if match else None
  if "codex" in value:
    return "codex", None
  if "claude" in value:
    return "claude", None
  if any(token in value for token in ("node", "npm", "esbuild", "vite")):
    return "frontend_tools", None
  if "caddy" in value:
    return "proxy", "platform"
  if "tandoor" in value:
    return "app_service", "tandoor"
  match = re.search(r"/data/apps/([^/\\s]+)", cmdline)
  if match:
    return "app_service", match.group(1)
  if "recover" in value:
    return "recovery", "platform"
  return "other", None


def process_inventory(
  *,
  limit: int = 20,
  proc_root: Path = Path("/proc"),
  cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
  """Aggregate all processes in this cgroup; intended for explicit debug use."""
  root = _cgroup_dir(proc_root=proc_root, cgroup_root=cgroup_root)
  raw_pids = _read_text(root / "cgroup.procs")
  if raw_pids is None:
    return {"available": False, "groups": [], "top_processes": []}
  rows: list[dict[str, Any]] = []
  groups: dict[str, dict[str, int]] = defaultdict(
    lambda: {"process_count": 0, "rss_bytes": 0, "pss_bytes": 0,
             "anonymous_bytes": 0, "swap_bytes": 0}
  )
  for raw in raw_pids.split():
    try:
      pid = int(raw)
    except ValueError:
      continue
    snapshot = process_memory_snapshot(pid, proc_root=proc_root)
    if not snapshot.get("available"):
      continue
    comm = (_read_text(proc_root / str(pid) / "comm") or "").strip()
    cmdline = (
      _read_text(proc_root / str(pid) / "cmdline") or ""
    ).replace("\x00", " ")
    category, owner = _process_identity(pid, comm, cmdline)
    row = {
      "pid": pid,
      "name": comm[:80],
      "category": category,
      "owner": owner,
      "rss_bytes": snapshot.get("rss_bytes") or 0,
      "pss_bytes": snapshot.get("pss_bytes") or 0,
      "anonymous_bytes": snapshot.get("anonymous_bytes") or 0,
      "swap_bytes": snapshot.get("swap_bytes") or 0,
    }
    rows.append(row)
    group = groups[category]
    group["process_count"] += 1
    for key in ("rss_bytes", "pss_bytes", "anonymous_bytes", "swap_bytes"):
      group[key] += row[key]
  return {
    "available": True,
    "process_count": len(rows),
    "groups": [
      {"category": category, **values}
      for category, values in sorted(
        groups.items(), key=lambda item: item[1]["pss_bytes"], reverse=True
      )
    ],
    "top_processes": sorted(
      rows, key=lambda row: row["pss_bytes"], reverse=True
    )[:max(0, min(limit, 100))],
  }


def memory_map_summary(
  pid: int | None = None,
  *,
  proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
  """Group Linux memory mappings into heap, anonymous, stack, and files."""
  target_pid = int(pid or os.getpid())
  text = _read_text(proc_root / str(target_pid) / "smaps")
  if text is None:
    return {"available": False, "groups": []}
  groups: dict[str, dict[str, int]] = defaultdict(
    lambda: {
      "mapping_count": 0, "rss_bytes": 0, "pss_bytes": 0,
      "anonymous_bytes": 0, "private_dirty_bytes": 0, "swap_bytes": 0,
    }
  )
  current_group: str | None = None
  for line in text.splitlines():
    first = line.split(" ", 1)[0]
    if "-" in first and ":" not in first:
      parts = line.split()
      name = " ".join(parts[5:]) if len(parts) > 5 else ""
      if name == "[heap]":
        current_group = "heap"
      elif name.startswith("[stack"):
        current_group = "stack"
      elif not name or name.startswith("[anon") or name.startswith("["):
        current_group = "anonymous_mappings"
      else:
        current_group = "file_mappings"
      groups[current_group]["mapping_count"] += 1
      continue
    if current_group is None:
      continue
    key, separator, raw = line.partition(":")
    if not separator or key not in {
      "Rss", "Pss", "Anonymous", "Private_Dirty", "Swap",
    }:
      continue
    parts = raw.strip().split()
    try:
      value = int(parts[0]) * 1024
    except (IndexError, ValueError):
      continue
    destination = {
      "Rss": "rss_bytes",
      "Pss": "pss_bytes",
      "Anonymous": "anonymous_bytes",
      "Private_Dirty": "private_dirty_bytes",
      "Swap": "swap_bytes",
    }[key]
    groups[current_group][destination] += value
  return {
    "available": True,
    "groups": [
      {"category": category, **values}
      for category, values in sorted(
        groups.items(), key=lambda item: item[1]["pss_bytes"], reverse=True
      )
    ],
  }


def gc_diagnostics(*, deep: bool = False, limit: int = 25) -> dict[str, Any]:
  """Return GC counters; an explicit deep request also counts tracked types."""
  result: dict[str, Any] = {
    "counts": list(gc.get_count()),
    "thresholds": list(gc.get_threshold()),
  }
  if not deep:
    return result
  objects = gc.get_objects()
  counts = Counter(
    f"{type(item).__module__}.{type(item).__qualname__}" for item in objects
  )
  result.update(
    tracked_object_count=len(objects),
    top_tracked_types=[
      {"type": name, "count": count}
      for name, count in counts.most_common(max(0, min(limit, 100)))
    ],
  )
  return result


def allocation_report(*, limit: int = 25) -> dict[str, Any]:
  """Attribute live traced Python allocations without retaining a snapshot."""
  status = tracing_status()
  if not status.get("enabled") or limit <= 0:
    return status
  import tracemalloc
  snapshot = tracemalloc.take_snapshot()
  statistics = snapshot.statistics("lineno")
  status["top_allocations"] = [
    {
      "file": str(stat.traceback[0].filename),
      "line": stat.traceback[0].lineno,
      "bytes": stat.size,
      "count": stat.count,
    }
    for stat in statistics[:max(0, min(limit, 100))]
  ]
  status["traced_total_bytes"] = sum(stat.size for stat in statistics)
  return status


def record_memory_checkpoint(label: str, **context: Any) -> dict[str, Any]:
  """Capture one bounded lifecycle sample and keep the last 128 in memory."""
  process = process_memory_snapshot()
  cgroup = cgroup_memory_snapshot()
  entry: dict[str, Any] = {
    "at": datetime.now(UTC).isoformat(),
    "label": str(label)[:80],
    "process": process,
    "cgroup": {
      key: cgroup.get(key)
      for key in (
        "available", "current_bytes", "working_set_bytes", "anon_bytes",
        "file_bytes", "inactive_file_bytes", "kernel_bytes",
      )
    },
    "tracing": tracing_status(),
  }
  if context:
    entry["context"] = {
      str(key)[:80]: value
      for key, value in context.items()
      if value is None or isinstance(value, (str, int, float, bool))
    }
  with _checkpoint_lock:
    _checkpoints.append(entry)
  log.info(
    "memory checkpoint label=%s rss=%s pss=%s anon=%s cgroup=%s",
    entry["label"],
    process.get("rss_bytes"),
    process.get("pss_bytes"),
    process.get("anonymous_bytes"),
    cgroup.get("current_bytes"),
  )
  return entry


def memory_status(*, include_checkpoints: bool = False) -> dict[str, Any]:
  result = {
    "process": process_memory_snapshot(),
    "cgroup": cgroup_memory_snapshot(),
    "tracing": tracing_status(),
  }
  if include_checkpoints:
    with _checkpoint_lock:
      result["checkpoints"] = list(_checkpoints)
  return result
