"""Provider-telemetry retention policy — a DRY-RUN planner. Deletes NOTHING.

The 2026-09-01 incident was fixed by a human manually removing an inactive,
oversized Codex telemetry database under CODEX_HOME (/data/cli-auth/codex).
Nothing structural prevents recurrence: the chat agent is forbidden to touch
/data/cli-auth (auth + RESUMABLE provider context live there), and no bounded
rotation exists.

This module encodes a CONSERVATIVE retention policy as a PURE planner. Given a
directory listing (supplied by the caller — this module never reads the
filesystem and never opens a file), it classifies each entry as reclaimable or
preserved, byte-accounted, with an explicit reason. It intentionally contains NO
deletion code path: reclaiming files under /data/cli-auth is irreversible and
can corrupt a running provider (a live/current-generation DB) or destroy a
resumable turn's context, so live reclamation must be a deliberate,
explicitly-approved step wired separately after a dry-run is reviewed against
the real layout. ``retention_enabled()`` exposes that gate (default OFF).

PRESERVE (never reclaim):
  * auth.json, *.credentials.json, config.* (provider auth/config),
  * the CURRENT (highest) generation of every telemetry DB family,
  * anything whose mtime is inside the active window (``min_age_seconds``).
RECLAIM (only mechanically-safe classes):
  * superseded lower-generation telemetry DBs,
  * ephemeral scratch dirs (cache/tmp/.tmp/shell-snapshots),
  * rotated login logs,
  * stale ``.claude.json`` backups beyond the newest ``keep_json_backups``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# Codex telemetry/state DB families written under CODEX_HOME, named
# ``<family>_<generation>.sqlite`` (also ``.sqlite-wal``/``-shm`` sidecars).
TELEMETRY_DB_FAMILIES = (
  "state", "logs", "queue", "goals", "memories", "thread_history",
)
_DB_RE = re.compile(
  r"^(?P<family>" + "|".join(TELEMETRY_DB_FAMILIES) + r")_(?P<gen>\d+)\.sqlite"
  r"(?P<sidecar>-wal|-shm)?$"
)

# Ephemeral scratch directories that are always safe to reclaim when stale.
_EPHEMERAL_DIRS = frozenset({
  "cache", "tmp", ".tmp", "shell_snapshots", "shell-snapshots",
})

# Rotated login logs (the current one is preserved by age; rotations are safe).
_LOG_RE = re.compile(r"^codex-login\.log(\.\d+)?$")

# Never reclaim these — provider auth/config. Matched by exact name or suffix.
_PRESERVE_EXACT = frozenset({"auth.json", ".claude.json"})
_PRESERVE_SUFFIXES = (".credentials.json",)
_PRESERVE_PREFIXES = ("config",)

_DEFAULT_MIN_AGE_SECONDS = 24 * 60 * 60  # 1 day — never touch recently-active


@dataclass(frozen=True)
class Entry:
  """One filesystem entry to classify (SUPPLIED by the caller; not read here)."""

  name: str
  size_bytes: int
  mtime: float
  is_dir: bool = False


def retention_enabled() -> bool:
  """Live reclamation gate. Default OFF — enabling requires deliberate intent.

  No actor in this module deletes anything regardless; this flag exists so a
  future, separately-reviewed reclamation step can refuse to run until an
  operator has opted in after inspecting a dry-run plan.
  """
  return os.environ.get("MOBIUS_TELEMETRY_RETENTION_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
  )


def _db_generation(name: str) -> tuple[str, int] | None:
  match = _DB_RE.match(name)
  if match is None:
    return None
  return match.group("family"), int(match.group("gen"))


def _is_preserved_name(name: str) -> bool:
  if name in _PRESERVE_EXACT:
    return True
  if any(name.endswith(suffix) for suffix in _PRESERVE_SUFFIXES):
    return True
  return any(name.startswith(prefix) for prefix in _PRESERVE_PREFIXES)


def plan_reclamation(
  entries: list[Entry],
  *,
  now: float,
  min_age_seconds: int = _DEFAULT_MIN_AGE_SECONDS,
  keep_json_backups: int = 3,
) -> dict[str, Any]:
  """Classify a provider-telemetry directory listing into reclaim/preserve.

  Pure: returns a plan, deletes nothing. Every reclaim decision carries a
  ``reason`` so a reviewer can audit it before any live reclamation is enabled.
  """
  live_generation: dict[str, int] = {}
  for entry in entries:
    parsed = _db_generation(entry.name)
    if parsed is not None:
      family, gen = parsed
      live_generation[family] = max(live_generation.get(family, gen), gen)

  # ``.claude.json`` backups, newest first, so we keep the freshest N.
  json_backups = sorted(
    (e for e in entries if e.name.startswith(".claude.json.")),
    key=lambda e: e.mtime,
    reverse=True,
  )
  keep_backup_names = {e.name for e in json_backups[:keep_json_backups]}

  reclaim: list[dict[str, Any]] = []
  preserve: list[dict[str, Any]] = []

  def _preserve(entry: Entry, reason: str) -> None:
    preserve.append({"name": entry.name, "bytes": entry.size_bytes, "reason": reason})

  def _reclaim(entry: Entry, reason: str) -> None:
    reclaim.append({"name": entry.name, "bytes": entry.size_bytes, "reason": reason})

  age_ok = lambda e: (now - e.mtime) >= min_age_seconds  # noqa: E731

  for entry in entries:
    if _is_preserved_name(entry.name):
      _preserve(entry, "provider auth/config — never reclaimed")
      continue

    parsed = _db_generation(entry.name)
    if parsed is not None:
      family, gen = parsed
      if gen >= live_generation.get(family, gen):
        _preserve(entry, f"current generation of {family} telemetry DB")
      elif not age_ok(entry):
        _preserve(entry, "superseded but modified within the active window")
      else:
        _reclaim(entry, f"superseded {family} telemetry DB (gen {gen} < live)")
      continue

    if entry.is_dir and entry.name in _EPHEMERAL_DIRS:
      if age_ok(entry):
        _reclaim(entry, "ephemeral scratch directory")
      else:
        _preserve(entry, "ephemeral dir modified within the active window")
      continue

    if _LOG_RE.match(entry.name) and entry.name != "codex-login.log":
      _reclaim(entry, "rotated login log") if age_ok(entry) else _preserve(
        entry, "recent rotated log"
      )
      continue

    if entry.name.startswith(".claude.json."):
      if entry.name in keep_backup_names:
        _preserve(entry, "recent .claude.json backup (kept)")
      elif age_ok(entry):
        _reclaim(entry, "stale .claude.json backup beyond newest kept")
      else:
        _preserve(entry, "recent .claude.json backup")
      continue

    _preserve(entry, "unclassified — preserved by default")

  return {
    "reclaim": reclaim,
    "preserve": preserve,
    "reclaimable_bytes": sum(item["bytes"] for item in reclaim),
    "live_generation": live_generation,
    "enabled": retention_enabled(),
  }
