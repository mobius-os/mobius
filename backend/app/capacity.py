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

# Free-byte alert ladder for /data, ordered least→most severe. This is
# ADDITIONAL to resource_pressure's proportional admission thresholds, not a
# replacement: it fires before admission's critical floor (5% of the volume,
# at most 1 GiB) refuses turns. Each tier is the smaller of an absolute amount
# and a share of the volume. Absolute bands alone suit a large volume but left a
# small one permanently inside a tier it had already announced, so a disk that
# then ran out raised no alert at all.
DATA_ALERT_TIERS: tuple[tuple[int, float, str], ...] = (
  (8 * GIB, 0.30, "notice"),
  (5 * GIB, 0.20, "warning"),
  (2 * GIB, 0.10, "critical"),
)

TIER_RANK: dict[str, int] = {"none": 0, "notice": 1, "warning": 2, "critical": 3}


def data_alert_ladder(total_bytes: int | None) -> dict[str, int]:
  """Each tier's free-byte threshold for a volume of ``total_bytes``."""
  ladder = {}
  for absolute, fraction, label in DATA_ALERT_TIERS:
    threshold = absolute
    if isinstance(total_bytes, int) and total_bytes > 0:
      threshold = min(absolute, int(total_bytes * fraction))
    ladder[label] = threshold
  return ladder


def data_alert_tier(
  free_bytes: int | None, total_bytes: int | None = None,
) -> str | None:
  """Most severe ladder tier whose threshold ``free_bytes`` falls below."""
  if not isinstance(free_bytes, int):
    return None
  tier = None
  for label, threshold in data_alert_ladder(total_bytes).items():
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


# The owner-facing name of each domain. The storage alert and the Settings
# Storage and Memory box both say these words, so the owner never has to
# translate an internal key such as ``agent_scratch``.
DOMAIN_LABELS: dict[str, str] = {
  "database": "Database",
  "provider_telemetry_codex": "Codex sessions",
  "provider_telemetry_claude": "Claude sessions",
  "chats": "Chats and uploads",
  "agent_scratch": "Chat work files",
  "browser_profiles": "Agent browsers",
  "contributions": "Contribution copies",
  "work": "Work folders",
  "worktrees": "Work copies",
  "compiled": "Compiled apps",
  "apps": "Apps",
  "shared": "Shared files",
  "platform": "Möbius code",
  "logs": "Logs",
  "app_secrets": "App secrets",
}


def domain_label(name: str) -> str:
  return DOMAIN_LABELS.get(name, name.replace("_", " ").capitalize())


def _domain_paths(data: Path) -> dict[str, Path]:
  return {
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
  paths = _domain_paths(Path(data_dir))
  domains: dict[str, dict[str, Any]] = {"database": _files_size(_db_paths())}
  if dir_sizer is not None:
    domains.update({name: dir_sizer(path) for name, path in paths.items()})
    return domains
  domains.update(_size_domains(paths))
  return domains


def _size_domains(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
  try:
    return _du_domain_sizes(paths)
  except (OSError, subprocess.SubprocessError):
    # Minimal/dev images may lack GNU du. Keep the safe Python fallback, but
    # divide the aggregate wall-clock budget so every domain is attempted.
    slice_seconds = _DU_TIME_BUDGET_S / max(1, len(paths))
    return {
      name: _dir_size(
        path,
        deadline=time.monotonic() + slice_seconds,
        entry_cap=_DU_PER_DOMAIN_ENTRY_CAP,
      )
      for name, path in paths.items()
    }


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
  data_total = mounts["data"].get("total_bytes")
  data_total = data_total if isinstance(data_total, int) else None
  tier = data_alert_tier(
    data_free if isinstance(data_free, int) else None, data_total,
  )
  snapshot: dict[str, Any] = {
    "captured_at": datetime.now(UTC).isoformat(),
    "mounts": mounts,
    "data_free_bytes": data_free if isinstance(data_free, int) else None,
    "data_total_bytes": mounts["data"].get("total_bytes"),
    "alert_tier": tier,
    "alert_ladder": data_alert_ladder(data_total),
  }
  if include_domains:
    domain_fn = domains_reader or data_domains
    snapshot["domains"] = domain_fn(data_dir)
    snapshot["domains_captured_at"] = snapshot["captured_at"]
  return snapshot


# Last snapshot computed by the periodic monitor. Operator reads (debug route)
# consume this cached value so a status probe never triggers a synchronous
# subtree walk — keeping the endpoint cheap as the incident lesson requires
# (readiness/status must not become a new outage vector under disk pressure).
_LATEST_LOCK = threading.Lock()
_LATEST_SNAPSHOT: dict[str, Any] | None = None


def set_latest_snapshot(snapshot: dict[str, Any] | None) -> None:
  """Cache a copy of ``snapshot``, keeping the last domain sizes when it has none.

  The monitor walks domains only every few ticks; the cheap ticks in between
  must not erase the last known sizes. ``domains_captured_at`` keeps saying
  when those sizes were actually measured. The caller's dict is not changed,
  so a cheap tick's own alert never names a largest use from older sizes.
  """
  global _LATEST_SNAPSHOT
  with _LATEST_LOCK:
    _LATEST_SNAPSHOT = _carry_known_domains(snapshot, _LATEST_SNAPSHOT)


def _carry_known_domains(
  snapshot: dict[str, Any] | None, previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
  if snapshot is None:
    return None
  cached = dict(snapshot)
  previous = previous or {}
  if "domains" not in cached and "domains" in previous:
    cached["domains"] = previous["domains"]
    cached["domains_captured_at"] = previous.get("domains_captured_at")
  if "forecast" not in cached and "forecast" in previous:
    cached["forecast"] = previous["forecast"]
  return cached


def latest_snapshot() -> dict[str, Any] | None:
  with _LATEST_LOCK:
    return _LATEST_SNAPSHOT


def measure_capacity_now(data_dir: str | Path) -> dict[str, Any]:
  """Take a fresh domain-aware snapshot and make it the cached one.

  The bounded ``du`` pass takes seconds, so only an explicit owner or operator
  request calls this; ordinary reads use the cached snapshot.
  """
  snapshot = capacity_snapshot(data_dir, include_domains=True)
  set_latest_snapshot(snapshot)
  return snapshot


def remeasure_domains(data_dir: str | Path, names: list[str]) -> dict[str, Any]:
  """Re-size only ``names`` after a cleanup and fold them into the cache.

  A cleanup changes one or two domains; re-walking all of /data for them
  would make the owner wait for unrelated trees. Mount facts are re-read too,
  because they are what the free-space bar shows.
  """
  global _LATEST_SNAPSHOT
  paths = _domain_paths(Path(data_dir))
  fresh = _size_domains({name: paths[name] for name in names if name in paths})
  snapshot = capacity_snapshot(data_dir, include_domains=False)
  with _LATEST_LOCK:
    cached = _carry_known_domains(snapshot, _LATEST_SNAPSHOT)
    if "domains" in cached:
      cached["domains"] = {**cached["domains"], **fresh}
    _LATEST_SNAPSHOT = cached
  return cached
