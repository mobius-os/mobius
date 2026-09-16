"""Durable recorder for kernel OOM-kill events in this container's cgroup.

Railway emails "ran out of memory" every time the kernel OOM-kills any process
in this container's memory cgroup, but by the time anyone looks the victim is
gone and kernel logs are not retained, so the killed process and the concurrent
workload that caused the spike were previously unknowable. This records a
durable, timestamped diagnostic the instant the cgroup ``oom_kill`` counter
rises: the cgroup memory split at detection, the current process inventory, the
active agent turns (the concurrent workload), and — by diffing against the
previous rolling sample — the PIDs that vanished across the kill (the likely
victims).

The case that matters is a restart. Interrupted turns auto-resume as the new
container boots; several heavy (~1 GB) agent subprocesses can allocate up
together past the cgroup limit within the first seconds of boot, and the kernel
sacrifices one. Möbius itself survives and the page reloads healthy, so nothing
looks wrong from inside the app — only this record and Railway's email witness
it. The watchdog is therefore started early (with the process services, before
chat supervisors trigger resume) and samples fast during the boot window.

Persistence mirrors ``capacity_history``: a capped JSONL ring under
``{data_dir}/logs`` with high-water trim and best-effort writes (any ``OSError``
is swallowed), so the recorder can never amplify the very pressure it observes.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory_observability import (
  _cgroup_dir,
  _read_text,
  cgroup_memory_snapshot,
  cgroup_oom_kill_count,
  process_inventory,
)

# OOM kills are rare, so the ring is small; each event is a rich record.
_EVENTS_HIGH_WATER = 400
_EVENTS_TRIM_TARGET = 300

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _events_path(data_dir: str | Path) -> Path:
  return Path(data_dir) / "logs" / "oom-events.jsonl"


def record_oom_event(data_dir: str | Path, event: dict[str, Any]) -> bool:
  """Append one OOM diagnostic to the capped ring. Best-effort.

  Returns True on a successful append, False on any OSError (the watchdog keeps
  running regardless — a disk-full box is exactly when OOMs are likeliest, and
  the recorder must never become the thing that fails).
  """
  path = _events_path(data_dir)
  line = json.dumps(event, separators=(",", ":"), default=str)
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
      f.write(line + "\n")
  except OSError:
    return False
  try:
    if _line_count(path) > _EVENTS_HIGH_WATER:
      _trim(path)
  except OSError:
    pass
  return True


def _line_count(path: Path) -> int:
  with open(path, encoding="utf-8") as f:
    return sum(1 for _ in f)


def _trim(path: Path) -> None:
  with open(path, encoding="utf-8") as f:
    retained = deque(f, maxlen=_EVENTS_TRIM_TARGET)
  staging = path.with_name(f".{path.name}.tmp")
  with open(staging, "w", encoding="utf-8") as f:
    f.writelines(retained)
  os.replace(staging, path)


def recent_oom_events(
  data_dir: str | Path, *, limit: int = 20,
) -> list[dict[str, Any]]:
  """Most recent recorded OOM events, oldest→newest. Empty on any read error."""
  path = _events_path(data_dir)
  events: list[dict[str, Any]] = []
  try:
    with open(path, encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          events.append(json.loads(line))
        except ValueError:
          continue
  except OSError:
    return []
  return events[-limit:] if limit else events


def lightweight_process_sample(
  *,
  proc_root: Path = Path("/proc"),
  cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
  """Cheap pid/name/RSS snapshot of the cgroup, read every watchdog tick.

  Deliberately avoids ``smaps``/PSS (the expensive read the full inventory
  uses): this runs on a short cadence during the boot window, so it reads only
  ``comm`` and ``statm`` (RSS = resident pages × page size). The point is to
  retain, from the tick *before* a kill, which PIDs existed — so the post-kill
  diff can name the process the kernel removed.
  """
  root = _cgroup_dir(proc_root=proc_root, cgroup_root=cgroup_root)
  raw_pids = _read_text(root / "cgroup.procs")
  processes: list[dict[str, Any]] = []
  if raw_pids is not None:
    for raw in raw_pids.split():
      try:
        pid = int(raw)
      except ValueError:
        continue
      statm = _read_text(proc_root / str(pid) / "statm")
      if not statm:
        continue
      fields = statm.split()
      try:
        rss_bytes = int(fields[1]) * _PAGE_SIZE
      except (IndexError, ValueError):
        rss_bytes = 0
      comm = (_read_text(proc_root / str(pid) / "comm") or "").strip()
      processes.append({"pid": pid, "name": comm[:80], "rss_bytes": rss_bytes})
  return {
    "at": datetime.now(UTC).isoformat(),
    "ts": time.time(),
    "processes": processes,
  }


def _vanished_processes(
  pre: dict[str, Any] | None, post: dict[str, Any] | None,
) -> list[dict[str, Any]]:
  """PIDs present in ``pre`` but absent in ``post`` — the likely OOM victims."""
  if not pre or not post:
    return []
  post_pids = {p["pid"] for p in post.get("processes", [])}
  return [
    p for p in pre.get("processes", []) if p["pid"] not in post_pids
  ]


def active_turn_summary() -> dict[str, Any]:
  """The agent turns running now — the concurrent workload behind the spike."""
  try:
    from app.runner_registry import RunnerKind, registry
  except Exception:
    return {"available": False}
  summary: dict[str, Any] = {"available": True}
  try:
    for kind in RunnerKind:
      summary[kind.value] = [
        handle.chat_id for handle in registry.handles_by_kind(kind)
      ]
    summary["starting"] = list(registry.starting_chat_ids())
  except Exception:
    return {"available": False}
  return summary


def capture_oom_event(
  *,
  oom_kill_count: int,
  kills_since_last: int,
  seconds_since_boot: float | None,
  pre_sample: dict[str, Any] | None,
  post_sample: dict[str, Any] | None,
  inventory_limit: int = 30,
) -> dict[str, Any]:
  """Assemble one full OOM diagnostic record from live runtime state."""
  return {
    "at": datetime.now(UTC).isoformat(),
    "ts": time.time(),
    "kind": "oom_kill",
    "oom_kill_count": oom_kill_count,
    "kills_since_last": kills_since_last,
    "seconds_since_boot": seconds_since_boot,
    "cgroup": cgroup_memory_snapshot(),
    "active_turns": active_turn_summary(),
    "likely_victims": _vanished_processes(pre_sample, post_sample),
    "pre_event_sample": pre_sample,
    "process_inventory": process_inventory(limit=inventory_limit),
  }
