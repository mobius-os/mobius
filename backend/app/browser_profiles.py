"""Bounded lifecycle management for per-chat agent-browser profiles."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import models


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
_DEFAULT_LOW_WATER_BYTES = 1536 * 1024**2
_DEFAULT_INACTIVE_DAYS = 30
_status = {
  "last_run_at": None,
  "profile_count": 0,
  "bytes_before": 0,
  "bytes_after": 0,
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


def _tree_bytes(path: Path) -> int:
  total = 0
  try:
    for root, _dirs, files in os.walk(path):
      for name in files:
        try:
          total += (Path(root) / name).stat().st_size
        except OSError:
          pass
  except OSError:
    pass
  return total


def chat_activity_snapshot(db: Session) -> dict[str, dict]:
  rows = db.query(
    models.Chat.id,
    models.Chat.activity_at,
    models.Chat.updated_at,
    models.Chat.deleted_at,
    models.Chat.run_status,
  ).all()
  return {
    str(row.id): {
      "activity_at": row.activity_at or row.updated_at,
      "deleted_at": row.deleted_at,
      "run_status": row.run_status,
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
) -> dict:
  """Prune regenerable caches, then oldest inactive profiles, at high water."""
  root = Path(data_dir) / "agent-browser-profiles"
  now = now or datetime.now(UTC).replace(tzinfo=None)
  max_bytes = max_bytes if max_bytes is not None else _env_int(
    "AGENT_BROWSER_PROFILE_MAX_BYTES", _DEFAULT_MAX_BYTES,
  )
  low_water_bytes = (
    low_water_bytes if low_water_bytes is not None else _env_int(
      "AGENT_BROWSER_PROFILE_LOW_WATER_BYTES", _DEFAULT_LOW_WATER_BYTES,
    )
  )
  low_water_bytes = min(low_water_bytes, max_bytes)
  inactive_days = inactive_days if inactive_days is not None else _env_int(
    "AGENT_BROWSER_PROFILE_INACTIVE_DAYS", _DEFAULT_INACTIVE_DAYS,
  )
  cutoff_seconds = inactive_days * 86400

  profiles = []
  if root.is_dir():
    for path in root.iterdir():
      match = _CHAT_PROFILE.fullmatch(path.name)
      if not match or not path.is_dir():
        continue
      chat_id = match.group(1)
      chat = chats.get(chat_id)
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
        chat_id in active_chat_ids
        or (chat and chat.get("run_status") == "running")
      )
      eligible = not active and (
        chat is None
        or chat.get("deleted_at") is not None
        or age_seconds >= cutoff_seconds
      )
      profiles.append({
        "path": path,
        "chat_id": chat_id,
        "activity": activity,
        "eligible": eligible,
        "active": active,
        "size": _tree_bytes(path),
      })

  bytes_before = sum(profile["size"] for profile in profiles)
  total = bytes_before
  cache_dirs_pruned = 0
  profiles_pruned = 0
  if total > max_bytes:
    # Chromium caches are disposable even for recently used chats. Prune them
    # from every CLOSED profile before considering deletion of any durable
    # profile state. Active profiles are excluded because Chromium may have
    # cache files mapped or locked while a turn is running.
    cache_candidates = sorted(
      (profile for profile in profiles if not profile["active"]),
      key=lambda profile: profile["activity"],
    )
    profile_candidates = sorted(
      (profile for profile in profiles if profile["eligible"]),
      key=lambda profile: profile["activity"],
    )
    for profile in cache_candidates:
      for rel in _CACHE_PATHS:
        cache = profile["path"] / rel
        if not cache.is_dir():
          continue
        before = _tree_bytes(cache)
        shutil.rmtree(cache, ignore_errors=True)
        if not cache.exists():
          total = max(0, total - before)
          cache_dirs_pruned += 1
      if total <= low_water_bytes:
        break

    if total > max_bytes:
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
    "bytes_before": bytes_before,
    "bytes_after": total,
    "reclaimed_bytes": max(0, bytes_before - total),
    "cache_dirs_pruned": cache_dirs_pruned,
    "profiles_pruned": profiles_pruned,
  }
  _status.update(result)
  return result


def browser_profile_status() -> dict:
  return dict(_status)
