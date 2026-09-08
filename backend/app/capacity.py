"""Multi-domain storage capacity: per-domain attribution + /data alert ladder.

``resource_pressure`` answers "how much headroom on the data_dir MOUNT" with one
cheap ``shutil.disk_usage``. During the 2026-09-01 incident that number
correctly went critical, but it could not tell an operator WHICH domain ate the
volume — the culprit was an inactive Codex telemetry DB under ``/data/cli-auth``,
and the file-explorer ``du`` (routes/fs.py) deliberately deny-lists cli-auth, so
nothing attributed it.

This module adds, WITHOUT forking the resource_pressure seam:

* mount-level facts for the two in-container mounts that matter — host ``/`` (the
  overlay a loop image would grow INTO during recovery) and the data_dir mount —
  reusing ``resource_status`` so the threshold logic stays single-sourced;
* bounded, SIZE-ONLY per-domain attribution under ``/data`` (db + sidecars,
  provider telemetry, chat media, agent-scratch, build cache, ...) so the
  offender becomes a line item. cli-auth is sized by a dedicated, non-following
  ``du`` pass (with a ``scandir``/``stat`` fallback): byte totals ONLY, never a
  content read. This does not relax the file explorer's privacy deny-list;
* an absolute 8 / 5 / 2 GiB free ALERT LADDER on ``/data``, layered on the same
  disk fact, for early operator warning before the turn-workspace reserve;

Sizing is bounded (time budget, no symlink follow) and best-effort;
it is for the periodic monitor / deploy preflight / operator status, NOT the hot
per-turn admission or per-probe readiness path.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from app.resource_pressure import GIB, resource_status

# GNU du walks all named domains in one bounded native pass. On the deployed
# 45-GiB volume this obtains every exact total in ~11 seconds; the former Python
# fixed-order walker spent its whole 8-second budget before reaching contrib.
_DU_TIME_BUDGET_S = 15.0
_DU_PER_DOMAIN_ENTRY_CAP = 100_000

# Absolute free-byte alert ladder for /data, ordered least→most severe. This is
# ADDITIONAL to resource_pressure's proportional admission thresholds, not a
# replacement: it fires earlier (8/5/2 GiB) so operators get warning before
# admission's ~1 GiB critical floor refuses turns.
DATA_ALERT_TIERS: tuple[tuple[int, str], ...] = (
  (8 * GIB, "notice"),
  (5 * GIB, "warning"),
  (2 * GIB, "critical"),
)

TIER_RANK: dict[str, int] = {"none": 0, "notice": 1, "warning": 2, "critical": 3}


def data_alert_tier(free_bytes: int | None) -> str | None:
  """Most severe ladder tier whose threshold ``free_bytes`` falls below."""
  if not isinstance(free_bytes, int):
    return None
  tier = None
  for threshold, label in DATA_ALERT_TIERS:
    if free_bytes < threshold:
      tier = label
  return tier


def _db_paths() -> list[Path]:
  """The SQLite database file and its WAL/SHM sidecars, if SQLite."""
  from app.config import get_settings

  url = get_settings().database_url
  if not url.startswith("sqlite:////"):
    return []
  db = Path(url.replace("sqlite:////", "/"))
  return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]


def _files_size(paths: list[Path]) -> dict[str, Any]:
  total = 0
  present = 0
  for p in paths:
    try:
      total += p.stat().st_blocks * 512
      present += 1
    except OSError:
      continue
  return {"exists": present > 0, "bytes": total, "entries": present, "truncated": False}


def _dir_size(
  root: Path,
  *,
  deadline: float | None = None,
  entry_cap: int = _DU_PER_DOMAIN_ENTRY_CAP,
) -> dict[str, Any]:
  """Bounded, symlink-safe recursive byte total for one subtree.

  Never follows symlinks (no cycles, no escaping the subtree), never reads file
  contents (``stat`` only, so it is safe over cli-auth), and stops at the time
  budget or entry cap — reporting ``truncated: True`` rather than silently
  undercounting.
  """
  if not root.exists() or root.is_symlink():
    return {"exists": False, "bytes": 0, "entries": 0, "truncated": False}
  total = 0
  entries = 0
  truncated = False
  deadline = deadline or (time.monotonic() + _DU_TIME_BUDGET_S)
  if entry_cap <= 0 or time.monotonic() > deadline:
    return {"exists": True, "bytes": 0, "entries": 0, "truncated": True}
  stack: list[Path] = [root]
  while stack:
    current = stack.pop()
    try:
      with os.scandir(current) as it:
        for entry in it:
          entries += 1
          if entries > entry_cap or time.monotonic() > deadline:
            truncated = True
            stack = []
            break
          try:
            if entry.is_symlink():
              continue
            if entry.is_dir(follow_symlinks=False):
              stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
              total += entry.stat(follow_symlinks=False).st_blocks * 512
          except OSError:
            continue
    except OSError:
      continue
  return {"exists": True, "bytes": total, "entries": entries, "truncated": truncated}


def _du_domain_sizes(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
  """Return exact allocated totals from one bounded, non-following du pass."""
  # -l makes each domain independent of operand order when Git has hard-links
  # across roots. Summed domain totals may therefore exceed physical volume
  # use; each individual figure is the domain's reclaim footprint.
  argv = [
    "du", "-sxl", "-B1", "--", *(str(path) for path in paths.values()),
  ]
  process = subprocess.Popen(
    argv,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
  )
  timed_out = False
  try:
    stdout, _ = process.communicate(timeout=_DU_TIME_BUDGET_S)
  except subprocess.TimeoutExpired:
    timed_out = True
    process.kill()
    stdout, _ = process.communicate()

  by_path: dict[str, int] = {}
  for line in stdout.splitlines():
    raw_size, separator, raw_path = line.partition("\t")
    if not separator:
      continue
    try:
      by_path[raw_path] = int(raw_size)
    except ValueError:
      continue
  return {
    name: {
      "exists": path.exists() and not path.is_symlink(),
      "bytes": by_path.get(str(path), 0),
      "entries": None,
      "truncated": str(path) not in by_path and path.exists(),
      **({"timed_out": True} if timed_out and str(path) not in by_path else {}),
    }
    for name, path in paths.items()
  }


def data_domains(
  data_dir: str | Path,
  *,
  dir_sizer: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
  """Size-only per-domain attribution under ``data_dir``.

  Provider telemetry (cli-auth/codex, cli-auth/claude) is byte totals ONLY — no
  filename or content is read or returned — honoring the cli-auth boundary while
  still making the incident's culprit class attributable.
  """
  data = Path(data_dir)
  paths = {
    "provider_telemetry_codex": data / "cli-auth" / "codex",
    "provider_telemetry_claude": data / "cli-auth" / "claude",
    "chats": data / "chats",
    "agent_scratch": data / "agent-scratch",
    "browser_profiles": data / "agent-browser-profiles",
    "contributions": data / "contrib",
    "work": data / "work",
    "worktrees": data / "worktrees",
    "compiled": data / "compiled",
    "apps": data / "apps",
    "shared": data / "shared",
    "platform": data / "platform",
    "logs": data / "logs",
    "app_secrets": data / "app-secrets",
  }
  domains: dict[str, dict[str, Any]] = {"database": _files_size(_db_paths())}
  if dir_sizer is not None:
    domains.update({name: dir_sizer(path) for name, path in paths.items()})
    return domains

  try:
    domains.update(_du_domain_sizes(paths))
  except (OSError, subprocess.SubprocessError):
    # Minimal/dev images may lack GNU du. Keep the safe Python fallback, but
    # divide the aggregate wall-clock budget so every domain is attempted.
    slice_seconds = _DU_TIME_BUDGET_S / max(1, len(paths))
    for name, path in paths.items():
      domains[name] = _dir_size(
        path,
        deadline=time.monotonic() + slice_seconds,
        entry_cap=_DU_PER_DOMAIN_ENTRY_CAP,
      )
  return domains


def _mount_facts(
  path: str,
  *,
  status_reader: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
  status = status_reader(path)
  status = status if isinstance(status, dict) else {}
  facts = status.get("facts") if isinstance(status.get("facts"), dict) else {}
  pressure = status.get("pressure") if isinstance(status.get("pressure"), dict) else {}
  disk_facts = facts.get("disk") if isinstance(facts.get("disk"), dict) else {}
  disk_pressure = pressure.get("disk") if isinstance(pressure.get("disk"), dict) else {}
  return {
    "path": disk_facts.get("path", path),
    "available": disk_facts.get("available", False),
    "free_bytes": disk_facts.get("free_bytes"),
    "total_bytes": disk_facts.get("total_bytes"),
    "used_bytes": disk_facts.get("used_bytes"),
    "state": disk_pressure.get("state", "unknown"),
    "free_ratio": disk_pressure.get("free_ratio"),
    "constrained_below_bytes": disk_pressure.get("constrained_below_bytes"),
    "critical_below_bytes": disk_pressure.get("critical_below_bytes"),
  }


def capacity_snapshot(
  data_dir: str | Path,
  *,
  include_domains: bool = False,
  status_reader: Callable[..., dict[str, Any]] | None = None,
  domains_reader: Callable[[str | Path], dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
  """A point-in-time capacity snapshot for monitoring / deploy / operators.

  Cheap by default (mount reads only). ``include_domains=True`` adds the bounded
  per-domain walk — do that in the periodic loop / deploy preflight, and read
  the cached result elsewhere so a status probe stays cheap.
  """
  reader = status_reader or resource_status
  mounts = {
    "host_root": _mount_facts("/", status_reader=reader),
    "data": _mount_facts(str(data_dir), status_reader=reader),
  }
  data_free = mounts["data"].get("free_bytes")
  tier = data_alert_tier(data_free if isinstance(data_free, int) else None)
  snapshot: dict[str, Any] = {
    "captured_at": datetime.now(UTC).isoformat(),
    "mounts": mounts,
    "data_free_bytes": data_free if isinstance(data_free, int) else None,
    "data_total_bytes": mounts["data"].get("total_bytes"),
    "alert_tier": tier,
    "alert_ladder": {label: threshold for threshold, label in DATA_ALERT_TIERS},
  }
  if include_domains:
    domain_fn = domains_reader or data_domains
    snapshot["domains"] = domain_fn(data_dir)
  return snapshot


# Last snapshot computed by the periodic monitor. Operator reads (debug route)
# consume this cached value so a status probe never triggers a synchronous
# subtree walk — keeping the endpoint cheap as the incident lesson requires
# (readiness/status must not become a new outage vector under disk pressure).
_LATEST_LOCK = threading.Lock()
_LATEST_SNAPSHOT: dict[str, Any] | None = None


def set_latest_snapshot(snapshot: dict[str, Any] | None) -> None:
  global _LATEST_SNAPSHOT
  with _LATEST_LOCK:
    _LATEST_SNAPSHOT = snapshot


def latest_snapshot() -> dict[str, Any] | None:
  with _LATEST_LOCK:
    return _LATEST_SNAPSHOT
