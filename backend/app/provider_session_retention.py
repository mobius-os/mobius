"""Bound old Codex resume state without touching chats or credentials.

Möbius keeps provider homes on ``/data`` so chats can resume across container
replacement. Codex does not currently age its rollout JSONL files there, so the
archive grows forever even though Möbius already treats a missing old provider
thread as a normal cold-resume case. This owner applies Möbius's 14-day provider
default to Codex rollout files, then bounds Codex's own SQLite stores and
scratch space under the same lock (codex_store_compaction).

Concurrency waits only for Codex, and is decided by two checks. This owner
takes the exclusive side of the cross-process Codex lock, whose shared side
Möbius holds around each Codex turn, usage probe, and session fork, so none
starts or runs during a sweep. A Codex process that outlives its turn (a warm helper host
between turns) or was started from an agent's shell holds no lock, so the
sweep also skips while any process holds a file under CODEX_HOME open. Other
agents are never paused. Startup can therefore reclaim before SQLite opens
while still skipping safely if an external Codex process is already active.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.storage_io import app_dir_usage, atomic_write, newest_change


DEFAULT_RETENTION_DAYS = {
  "claude": 14,
  "codex": 14,
}
MAX_FILES_PER_SWEEP = 10_000
# Codex turns wait on the sweep lock, so store work stops at this wall-clock
# bound; a backlog resumes on the next pass.
STORE_COMPACTION_BUDGET_SECONDS = 5.0


def _is_rollout_name(name: str) -> bool:
  return name.startswith("rollout-") and name.endswith(".jsonl")


def sweep_stale_provider_sessions(
  data_dir: str | Path,
  *,
  now: float | None = None,
  max_age_days: int = DEFAULT_RETENTION_DAYS["codex"],
  max_files: int = MAX_FILES_PER_SWEEP,
  store_budget_seconds: float | None = STORE_COMPACTION_BUDGET_SECONDS,
) -> dict:
  """Delete stale Codex rollout JSONL files and return a bounded summary.

  The walker never follows symlinks and never opens file contents. It considers
  only files named ``rollout-*.jsonl``; auth, configuration, telemetry,
  generated images, and every path outside ``cli-auth/codex/sessions`` are
  outside this function's authority. Date-bucket directories are traversed
  oldest-first, so the per-sweep cap makes forward progress instead of forever
  rescanning a prefix of recent files.
  """
  root = Path(data_dir) / "cli-auth" / "codex" / "sessions"
  from app.codex_session_lock import codex_home_in_use, try_acquire_codex_session_sweep

  ownership = try_acquire_codex_session_sweep(data_dir)
  if ownership is not None and codex_home_in_use(data_dir):
    # A Codex started outside Möbius's launchers holds no lock but does hold
    # its files open; its rollouts and stores are just as live.
    ownership.release()
    ownership = None
  if ownership is None:
    result = {
      "status": "skipped_active",
      "last_run_at": datetime.now(UTC).isoformat(),
      "scanned_files": 0,
      "removed_files": 0,
      "reclaimed_bytes": 0,
      "errors": 0,
      "truncated": False,
      "store_reclaimed_bytes": 0,
    }
    return result
  now = time.time() if now is None else now
  cutoff = now - max(0, max_age_days) * 86400
  stores: dict = {}
  scanned = removed = reclaimed = errors = 0
  truncated = False

  empty_dir_candidates: list[Path] = []
  try:
    if root.is_dir() and not root.is_symlink():
      for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        names[:] = [name for name in names if not (base / name).is_symlink()]
        names.sort()
        files.sort()
        empty_dir_candidates.append(base)
        for name in files:
          if not _is_rollout_name(name):
            continue
          if scanned >= max_files:
            truncated = True
            break
          scanned += 1
          candidate = base / name
          try:
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
              continue
            candidate.unlink()
            removed += 1
            reclaimed += info.st_blocks * 512
          except FileNotFoundError:
            continue
          except OSError:
            errors += 1
        if truncated:
          break

      for base in reversed(empty_dir_candidates):
        if base == root:
          continue
        try:
          base.rmdir()
        except OSError:
          pass

    try:
      from app.codex_store_compaction import compact_codex_stores
      stores = compact_codex_stores(
        root.parent, now=now, rollout_cutoff=cutoff,
        budget_seconds=store_budget_seconds,
      )
    except Exception as exc:
      # Store upkeep is optional; rollout retention above already succeeded.
      stores = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
  finally:
    ownership.release()

  result = {
    "status": "completed",
    "last_run_at": datetime.now(UTC).isoformat(),
    "scanned_files": scanned,
    "removed_files": removed,
    "reclaimed_bytes": reclaimed,
    "errors": errors,
    "truncated": truncated,
    "stores": stores,
    "store_reclaimed_bytes": stores.get("reclaimed_bytes", 0),
  }
  return result


# Möbius defaults for Claude's persisted settings. Each applies only while its
# key is absent, so an explicit owner or provider choice always wins.
CLAUDE_SETTINGS_DEFAULTS = {
  # Claude applies this horizon to transcripts and to disposable task state,
  # shell snapshots, backups, and tool-result files.
  "cleanupPeriodDays": DEFAULT_RETENTION_DAYS["claude"],
  # Agent commits carry Möbius's own attribution, not Claude's Co-Authored-By
  # trailer and generated-with note.
  "includeCoAuthoredBy": False,
}


def ensure_claude_settings_defaults(data_dir: str | Path) -> list[str]:
  """Add Möbius's Claude settings defaults and return the keys it added.

  Every unrelated Claude preference is preserved. Malformed or non-object
  settings fail loudly rather than being replaced.
  """
  path = Path(data_dir) / "cli-auth" / "claude" / "settings.json"
  if path.exists():
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
      raise ValueError("Claude settings must contain a JSON object")
  else:
    decoded = {}
  added = [key for key in CLAUDE_SETTINGS_DEFAULTS if key not in decoded]
  if added:
    decoded.update({key: CLAUDE_SETTINGS_DEFAULTS[key] for key in added})
    atomic_write(
      path,
      json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
  return added


# Owner-run cleanup ("Agent working copies" in Settings). Deleting an old provider
# session only costs a warm resume: Möbius keeps every chat's transcript and
# reseeds a fresh session from it when the provider's copy is gone.

def _stale_claude_session(transcript: Path, cutoff: float) -> tuple[Path, Path | None, int] | None:
  """A Claude transcript and its side folder, when neither changed since ``cutoff``."""
  info = transcript.lstat()
  if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
    return None
  side = transcript.with_suffix("")
  if side.is_symlink() or not side.is_dir():
    return transcript, None, info.st_size
  if newest_change(side) >= cutoff:
    return None
  return transcript, side, info.st_size + app_dir_usage(side)


def _stale_claude_sessions(
  data_dir: str | Path, cutoff: float, in_use: frozenset[str],
) -> list[tuple[Path, Path | None, int]]:
  root = Path(data_dir) / "cli-auth" / "claude" / "projects"
  if root.is_symlink() or not root.is_dir():
    return []
  transcripts = [
    transcript
    for project in root.iterdir() if project.is_dir() and not project.is_symlink()
    for transcript in project.glob("*.jsonl")
  ]
  stale = []
  for transcript in transcripts:
    if transcript.stem in in_use:
      continue
    try:
      session = _stale_claude_session(transcript, cutoff)
    except OSError:
      continue
    if session is not None:
      stale.append(session)
  return stale


def _codex_rollouts(data_dir: str | Path):
  root = Path(data_dir) / "cli-auth" / "codex" / "sessions"
  if root.is_symlink() or not root.is_dir():
    return
  for directory, _names, files in os.walk(root, followlinks=False):
    yield from (Path(directory) / name for name in files if _is_rollout_name(name))


def _stale_codex_usage(data_dir: str | Path, cutoff: float) -> tuple[int, int]:
  sizes = []
  for rollout in _codex_rollouts(data_dir):
    try:
      info = rollout.lstat()
    except OSError:
      continue
    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
      sizes.append(info.st_size)
  return sum(sizes), len(sizes)


def stale_session_usage(
  data_dir: str | Path, *, max_age_days: int, in_use: frozenset[str] = frozenset(),
  now: float | None = None,
) -> dict:
  """Bytes and count of Claude and Codex sessions older than ``max_age_days``.

  ``in_use`` holds Claude session ids of chats that are running or starting;
  an old session about to be resumed is never counted or deleted.
  """
  cutoff = (time.time() if now is None else now) - max_age_days * 86400
  claude = _stale_claude_sessions(data_dir, cutoff, in_use)
  codex_bytes, codex_count = _stale_codex_usage(data_dir, cutoff)
  return {
    "bytes": sum(size for _transcript, _side, size in claude) + codex_bytes,
    "sessions": len(claude) + codex_count,
  }


def stale_claude_sessions(
  data_dir: str | Path, *, max_age_days: int, now: float | None = None,
) -> list[tuple[Path, Path | None, int]]:
  """Claude transcripts (with their side folders) untouched for ``max_age_days``."""
  cutoff = (time.time() if now is None else now) - max_age_days * 86400
  return _stale_claude_sessions(data_dir, cutoff, frozenset())


_RELEASED = ".released-"


def detach_claude_session(transcript: Path, side: Path | None) -> list[Path] | None:
  """Move one session aside under hidden names, so Claude can no longer resume it.

  Renames are instant, so the caller can do this inside the same uninterrupted
  step as its "is this chat idle" check; the slow delete happens later through
  ``reclaim_released_claude_sessions``. ``None`` means the session stayed.
  """
  def released(path: Path) -> Path:
    return path.with_name(f".{path.name}{_RELEASED}{uuid.uuid4().hex}")

  try:
    moved = [transcript.rename(released(transcript))]
  except OSError:
    return None
  if side is not None:
    try:
      moved.append(side.rename(released(side)))
    except OSError:
      pass  # The transcript is gone, so nothing can resume this folder.
  return moved


def reclaim_released_claude_sessions(data_dir: str | Path) -> int:
  """Delete every session moved aside, including leftovers of an interrupted clear."""
  root = Path(data_dir) / "cli-auth" / "claude" / "projects"
  if root.is_symlink() or not root.is_dir():
    return 0
  freed = 0
  for project in root.iterdir():
    if project.is_symlink() or not project.is_dir():
      continue
    for entry in project.iterdir():
      if not (entry.name.startswith(".") and _RELEASED in entry.name):
        continue
      try:
        if entry.is_dir() and not entry.is_symlink():
          size = app_dir_usage(entry)
          shutil.rmtree(entry)
        else:
          size = entry.lstat().st_size
          entry.unlink()
      except OSError:
        continue
      freed += size
  return freed
