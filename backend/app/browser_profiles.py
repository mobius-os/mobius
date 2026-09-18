"""Bounded lifecycle management for per-chat agent-browser profiles."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.browser_processes import (
  BrowserSessionScan, BrowserSessionTarget, scan_browser_processes,
)
from app import config, models
from app.run_state import running_chat_ids


_CHAT_PROFILE = re.compile(r"^chat-([0-9a-fA-F-]{36})$")
_CACHE_PATHS = (
  "Default/Cache",
  "Default/Code Cache",
  "Default/GPUCache",
  "Default/DawnGraphiteCache",
  "Default/DawnWebGPUCache",
  "GrShaderCache",
  "GraphiteDawnCache",
  "ShaderCache",
)
_DEFAULT_MAX_BYTES = 2 * 1024**3
_DEFAULT_LOW_WATER_BYTES = _DEFAULT_MAX_BYTES * 3 // 4
_DEFAULT_INACTIVE_DAYS = 30
_DEFAULT_SWEEP_SECONDS = 60 * 60
_status = {
  "last_run_at": None,
  "profile_count": 0,
  "bytes_before": 0,
  "bytes_after": 0,
  "max_bytes": 0,
  "low_water_bytes": 0,
  "over_quota_bytes": 0,
  "reclaimed_bytes": 0,
  "cache_dirs_pruned": 0,
  "profiles_pruned": 0,
}


def _env_int(name: str, default: int) -> int:
  try:
    value = int(os.environ.get(name, str(default)))
  except ValueError:
    return default
  return value if value >= 0 else default


def default_browser_profile_quota(
  data_dir: str | Path,
) -> tuple[int, int]:
  """Size profile defaults from the stable capacity of the data filesystem."""
  try:
    total_bytes = int(shutil.disk_usage(data_dir).total)
  except (OSError, TypeError, ValueError):
    return _DEFAULT_MAX_BYTES, _DEFAULT_LOW_WATER_BYTES
  if total_bytes <= 0:
    return _DEFAULT_MAX_BYTES, _DEFAULT_LOW_WATER_BYTES
  max_bytes = min(_DEFAULT_MAX_BYTES, total_bytes // 4)
  return max_bytes, max_bytes * 3 // 4


def browser_profile_sweep_seconds() -> int:
  """Return a bounded sweep interval, with an operator override."""
  return max(60, _env_int(
    "AGENT_BROWSER_PROFILE_SWEEP_SECONDS", _DEFAULT_SWEEP_SECONDS,
  ))


def _tree_bytes(path: Path) -> int:
  total = 0
  try:
    for root, _dirs, files in os.walk(path):
      for name in files:
        try:
          candidate = Path(root) / name
          if candidate.is_symlink():
            continue
          total += candidate.stat().st_size
        except OSError:
          pass
  except OSError:
    pass
  return total


def _active_profile_names(root: Path) -> set[str]:
  """Return profile directory names referenced by live Chromium processes.

  Runner registry state covers chat turns but not named browser sessions such
  as Reflection, QA, or settings checks. Reading proc cmdlines keeps those
  profiles out of cache/profile deletion without trusting their directory name.
  """
  active = set()
  proc = Path("/proc")
  if not proc.is_dir():
    return active
  try:
    children = proc.iterdir()
  except OSError:
    return active
  root = root.resolve()
  for child in children:
    if not child.name.isdigit():
      continue
    try:
      args = (child / "cmdline").read_bytes().split(b"\0")
    except OSError:
      continue
    for index, raw in enumerate(args):
      value = raw.decode("utf-8", errors="replace")
      profile_value = None
      if value.startswith("--user-data-dir="):
        profile_value = value.split("=", 1)[1]
      elif value == "--user-data-dir" and index + 1 < len(args):
        profile_value = args[index + 1].decode("utf-8", errors="replace")
      if not profile_value:
        continue
      try:
        profile = Path(profile_value).resolve()
        if profile.parent == root:
          active.add(profile.name)
      except (OSError, RuntimeError):
        pass
  return active


def chat_browser_profile_path(chat_id: str) -> Path:
  """Use the same persistent profile for launching and releasing a chat."""
  safe = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")
  return Path(config.get_settings().data_dir) / "agent-browser-profiles" / f"chat-{safe}"


def browser_session_targets_for_chat(
  chat_id: str, *, proc_root: Path = Path("/proc"),
) -> BrowserSessionScan:
  if not chat_id:
    return BrowserSessionScan(frozenset(), False)
  profile = chat_browser_profile_path(chat_id)
  return scan_browser_processes(chat_id=chat_id, profile=str(profile), proc_root=proc_root)


def chat_activity_snapshot(db: Session) -> dict[str, dict]:
  rows = db.query(
    models.Chat.id,
    models.Chat.activity_at,
    models.Chat.updated_at,
    models.Chat.deleted_at,
  ).all()
  running = running_chat_ids(db, (str(row.id) for row in rows))
  return {
    str(row.id): {
      "activity_at": row.activity_at or row.updated_at,
      "deleted_at": row.deleted_at,
      "running": str(row.id) in running,
    }
    for row in rows
  }


def enforce_browser_profile_quota(
  data_dir: str | Path,
  chats: dict[str, dict],
  active_chat_ids: set[str],
  *,
  now: datetime | None = None,
  max_bytes: int | None = None,
  low_water_bytes: int | None = None,
  inactive_days: int | None = None,
  active_profile_names: set[str] | None = None,
) -> dict:
  """Prune caches, then inactive chat profiles to honor the byte budget.

  ``inactive_days`` is a preferred retention window, not permission for the
  ordinary per-chat tree to grow past ``max_bytes``. Deleted, missing, and
  expired chat profiles yield first. If those cannot restore the low-water
  mark, the oldest remaining inactive chat profiles yield too. Live sessions
  never do, and deliberately named sessions retain their full inactivity grace.
  Any protected overage is reported rather than mislabelled as reclaimed.

  """
  root = Path(data_dir) / "agent-browser-profiles"
  now = now or datetime.now(UTC).replace(tzinfo=None)
  default_max_bytes, default_low_water_bytes = default_browser_profile_quota(
    data_dir,
  )
  max_bytes = max_bytes if max_bytes is not None else _env_int(
    "AGENT_BROWSER_PROFILE_MAX_BYTES", default_max_bytes,
  )
  low_water_bytes = (
    low_water_bytes if low_water_bytes is not None else _env_int(
      "AGENT_BROWSER_PROFILE_LOW_WATER_BYTES", default_low_water_bytes,
    )
  )
  low_water_bytes = min(low_water_bytes, max_bytes)
  inactive_days = inactive_days if inactive_days is not None else _env_int(
    "AGENT_BROWSER_PROFILE_INACTIVE_DAYS", _DEFAULT_INACTIVE_DAYS,
  )
  cutoff_seconds = inactive_days * 86400
  active_profile_names = (
    _active_profile_names(root)
    if active_profile_names is None else active_profile_names
  )

  profiles = []
  if root.is_dir():
    for path in root.iterdir():
      match = _CHAT_PROFILE.fullmatch(path.name)
      if path.is_symlink() or not path.is_dir():
        continue
      chat_id = match.group(1) if match else None
      chat = chats.get(chat_id) if chat_id else None
      activity = chat.get("activity_at") if chat else None
      if activity is not None and activity.tzinfo is not None:
        activity = activity.astimezone(UTC).replace(tzinfo=None)
      try:
        fallback_activity = datetime.fromtimestamp(path.stat().st_mtime)
      except OSError:
        fallback_activity = now
      activity = activity or fallback_activity
      age_seconds = max(0.0, (now - activity).total_seconds())
      active = (
        path.name in active_profile_names
        or (chat_id is not None and chat_id in active_chat_ids)
        or bool(chat and chat.get("running"))
      )
      if chat_id is None:
        # Named/legacy profiles are included in the byte budget and cache
        # pruning, but their durable state receives the full inactivity grace.
        # Named sessions are deliberately long-lived; unlike ordinary chat
        # profiles, a recent one never joins the pressure fallback.
        retire_first = not active and age_seconds >= cutoff_seconds
      else:
        retire_first = not active and (
          chat is None
          or chat.get("deleted_at") is not None
          or age_seconds >= cutoff_seconds
        )
      profiles.append({
        "path": path,
        "chat_id": chat_id,
        "activity": activity,
        "retire_first": retire_first,
        "active": active,
        "size": _tree_bytes(path),
      })

  bytes_before = sum(profile["size"] for profile in profiles)
  total = bytes_before
  pressure_triggered = total > max_bytes
  cache_dirs_pruned = 0
  profiles_pruned = 0
  if pressure_triggered:
    # Chromium caches are disposable even for recently used chats. Prune them
    # from every CLOSED profile before considering deletion of any durable
    # profile state. Active profiles are excluded because Chromium may have
    # cache files mapped or locked while a turn is running.
    cache_candidates = sorted(
      (profile for profile in profiles if not profile["active"]),
      key=lambda profile: profile["activity"],
    )
    profile_candidates = sorted(
      (
        profile for profile in profiles
        if not profile["active"]
        and (
          profile["chat_id"] is not None
          or profile["retire_first"]
        )
      ),
      key=lambda profile: (
        not profile["retire_first"],
        profile["activity"],
      ),
    )
    for profile in cache_candidates:
      for rel in _CACHE_PATHS:
        cache = profile["path"] / rel
        if cache.is_symlink() or not cache.is_dir():
          continue
        before = _tree_bytes(cache)
        shutil.rmtree(cache, ignore_errors=True)
        if not cache.exists():
          total = max(0, total - before)
          cache_dirs_pruned += 1
      if total <= low_water_bytes:
        break

    if total > low_water_bytes:
      for profile in profile_candidates:
        if not profile["path"].exists():
          continue
        before = _tree_bytes(profile["path"])
        shutil.rmtree(profile["path"], ignore_errors=True)
        if not profile["path"].exists():
          total = max(0, total - before)
          profiles_pruned += 1
        if total <= low_water_bytes:
          break

  result = {
    "last_run_at": datetime.now(UTC).isoformat(),
    "profile_count": len(profiles),
    "non_chat_profile_count": sum(
      1 for profile in profiles if profile["chat_id"] is None
    ),
    "bytes_before": bytes_before,
    "bytes_after": total,
    "max_bytes": max_bytes,
    "low_water_bytes": low_water_bytes,
    "over_quota_bytes": max(0, total - max_bytes),
    "reclaimed_bytes": max(0, bytes_before - total),
    "cache_dirs_pruned": cache_dirs_pruned,
    "profiles_pruned": profiles_pruned,
  }
  _status.update(result)
  return result


def browser_profile_status() -> dict:
  return dict(_status)
