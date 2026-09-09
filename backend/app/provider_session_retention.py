"""Bound old Codex resume state without touching chats or credentials.

Möbius keeps provider homes on ``/data`` so chats can resume across container
replacement. Codex does not currently age its rollout JSONL files there, so the
archive grows forever even though Möbius already treats a missing old provider
thread as a normal cold-resume case. This owner applies Möbius's 14-day provider
default to Codex rollout files only.

Concurrency has two layers: web callers close runner admission while idle, and
this filesystem owner takes the exclusive side of the cross-process Codex lock.
Every launcher, including standalone Reflection, holds the shared side for its
full process lifetime. Startup can therefore reclaim before SQLite opens while
still skipping safely if an external Codex process is already active.
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from app.storage_io import atomic_write


DEFAULT_RETENTION_DAYS = {
  "claude": 14,
  "codex": 14,
}
MAX_FILES_PER_SWEEP = 10_000
_status = {
  "last_run_at": None,
  "scanned_files": 0,
  "removed_files": 0,
  "reclaimed_bytes": 0,
  "errors": 0,
  "truncated": False,
}


def sweep_stale_provider_sessions(
  data_dir: str | Path,
  *,
  now: float | None = None,
  max_age_days: int = DEFAULT_RETENTION_DAYS["codex"],
  max_files: int = MAX_FILES_PER_SWEEP,
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
  from app.codex_session_lock import try_acquire_codex_session_sweep

  ownership = try_acquire_codex_session_sweep(data_dir)
  if ownership is None:
    result = {
      "status": "skipped_active",
      "last_run_at": datetime.now(UTC).isoformat(),
      "scanned_files": 0,
      "removed_files": 0,
      "reclaimed_bytes": 0,
      "errors": 0,
      "truncated": False,
    }
    _status.update(result)
    return result
  cutoff = (time.time() if now is None else now) - max(0, max_age_days) * 86400
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
          if not (name.startswith("rollout-") and name.endswith(".jsonl")):
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
  }
  _status.update(result)
  return result


def ensure_claude_retention_default(data_dir: str | Path) -> dict:
  """Set Mobius's Claude default without overriding an owner choice.

  Claude Code natively owns cleanup through ``cleanupPeriodDays``. Its scope is
  broader than transcript JSONL: Claude also applies the horizon to disposable
  task state, shell snapshots, backups, and tool-result files. Mobius supplies
  its 14-day working-state default only while that key is absent, preserving a
  future user/provider-specific setting as well as every unrelated Claude
  preference. Malformed or non-object settings fail loudly rather than being
  replaced.
  """
  path = Path(data_dir) / "cli-auth" / "claude" / "settings.json"
  if path.exists():
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
      raise ValueError("Claude settings must contain a JSON object")
  else:
    decoded = {}
  if "cleanupPeriodDays" in decoded:
    existing = decoded["cleanupPeriodDays"]
    return {
      "changed": False,
      "retention_days": existing,
      "source": "explicit",
    }
  retention_days = DEFAULT_RETENTION_DAYS["claude"]
  decoded["cleanupPeriodDays"] = retention_days
  atomic_write(
    path,
    json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
  )
  return {
    "changed": True,
    "retention_days": retention_days,
    "source": "mobius_default",
  }


def provider_session_retention_status() -> dict:
  return dict(_status)
