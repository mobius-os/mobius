"""Bounded lifecycle management for per-chat agent-browser profiles."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.browser_processes import (
  BrowserSessionScan, BrowserSessionTarget, scan_browser_processes,
)
from app import config, models
from app.run_state import running_chat_ids


_CHAT_PROFILE = re.compile(r"^chat-([0-9a-fA-F-]{36})$")
# An agent that needs a second browser often names its extra profile after its
# chat with a short prefix (for example `sd-<chat id>` or
# `qa-refresh-<chat id>`). Such a profile is that chat's scratch, not a
# deliberately long-lived named session, so it retires with the chat. Only a
# lowercase, hyphen-separated prefix followed by the chat id at the very end
# qualifies; any other name keeps the named-session grace.
_EMBEDDED_CHAT_ID = re.compile(
  r"^[a-z0-9]+(?:-[a-z0-9]+)*"
  r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
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
# Per-chat profiles only mirror the owner's warm PWA state for a chat's own
# repeated screenshots; after this idle horizon re-warming costs one capture.
_DEFAULT_INACTIVE_DAYS = 2
# Deliberately named profiles (e.g. a long-lived touch session) are not
# per-chat caches and yield only to byte pressure after this grace.
_NAMED_PROFILE_GRACE_DAYS = 30
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


def _remove_profile(path: Path) -> bool:
  shutil.rmtree(path, ignore_errors=True)
  return not path.exists()


def _profile_chat_id(path: Path, chats: dict[str, dict]) -> str | None:
  match = _CHAT_PROFILE.fullmatch(path.name)
  if match:
    return match.group(1)
  embedded = _EMBEDDED_CHAT_ID.fullmatch(path.name)
  # Only a known chat claims an ad-hoc profile; an unrecognised id may
  # be anything, so that profile keeps the named-session grace.
  if embedded and embedded.group(1) in chats:
    return embedded.group(1)
  return None


def _profile_activity(path: Path, chat: dict | None, now: datetime) -> datetime:
  activity = chat.get("activity_at") if chat else None
  if activity is not None and activity.tzinfo is not None:
    activity = activity.astimezone(UTC).replace(tzinfo=None)
  if activity is not None:
    return activity
  try:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(tzinfo=None)
  except OSError:
    return now


def _profile_entry(
  path: Path,
  chats: dict[str, dict],
  active_chat_ids: set[str],
  *,
  now: datetime,
  cutoff_seconds: int,
  active_profile_names: set[str],
) -> dict:
  chat_id = _profile_chat_id(path, chats)
  chat = chats.get(chat_id) if chat_id else None
  activity = _profile_activity(path, chat, now)
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
    retire_first = not active and age_seconds >= _NAMED_PROFILE_GRACE_DAYS * 86400
  else:
    # A profile with no chat row ages by its own mtime, so one minted just
    # before its chat becomes visible is never mistaken for an orphan.
    retire_first = not active and (
      bool(chat and chat.get("deleted_at") is not None)
      or age_seconds >= cutoff_seconds
    )
  return {
    "path": path,
    "chat_id": chat_id,
    "activity": activity,
    "retire_first": retire_first,
    "active": active,
    "size": _tree_bytes(path),
  }


def _profile_inventory(
  root: Path,
  chats: dict[str, dict],
  active_chat_ids: set[str],
  *,
  now: datetime,
  cutoff_seconds: int,
  active_profile_names: set[str],
) -> list[dict]:
  """Every profile directory with its owner, activity, size and retirement."""
  if not root.is_dir():
    return []
  return [
    _profile_entry(
      path, chats, active_chat_ids, now=now, cutoff_seconds=cutoff_seconds,
      active_profile_names=active_profile_names,
    )
    for path in root.iterdir()
    if not path.is_symlink() and path.is_dir()
  ]


# The owner's Clear keeps a chat's browser for this long after the chat was last
# active, the same grace as Chat work files, so a chat that just finished or is
# about to continue keeps its sign-ins.
OWNER_CLEAR_GRACE_SECONDS = 15 * 60


def _owner_clearable_profiles(
  root: Path, chats: dict[str, dict], active_chat_ids: set[str],
) -> list[dict]:
  profiles = _profile_inventory(
    root, chats, active_chat_ids, now=datetime.now(UTC).replace(tzinfo=None),
    cutoff_seconds=OWNER_CLEAR_GRACE_SECONDS,
    active_profile_names=_active_profile_names(root),
  )
  return [p for p in profiles if p["chat_id"] is not None and p["retire_first"]]


def closed_chat_profile_usage(
  data_dir: str | Path, chats: dict[str, dict], active_chat_ids: set[str],
) -> dict:
  """What the owner's "Clear agent browser data" deletes, for the confirm step."""
  retired = _owner_clearable_profiles(
    Path(data_dir) / "agent-browser-profiles", chats, active_chat_ids,
  )
  return {"bytes": sum(p["size"] for p in retired), "profiles": len(retired)}


def clear_closed_chat_profiles(
  data_dir: str | Path,
  chats: dict[str, dict],
  active_chat_ids: set[str],
  *,
  still_in_use: Callable[[str], bool],
) -> dict:
  """Delete exactly the profiles the preview counts, never a cache or named one.

  Measuring sizes takes seconds, so each profile is checked again just before
  it is removed: against a fresh scan of open browsers and ``still_in_use``
  (a chat that started meanwhile).
  """
  root = Path(data_dir) / "agent-browser-profiles"
  candidates = _owner_clearable_profiles(root, chats, active_chat_ids)
  open_now = _active_profile_names(root)
  removed = reclaimed = 0
  for profile in candidates:
    if profile["path"].name in open_now or still_in_use(profile["chat_id"]):
      continue
    if _remove_profile(profile["path"]):
      removed += 1
      reclaimed += profile["size"]
  return {"bytes": reclaimed, "profiles": removed}


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
  """Retire expired chat profiles, then honor the byte budget.

  This is the only retention owner for agent-browser profiles. Every sweep
  deletes each closed chat profile whose chat is deleted, or whose chat (for
  an orphan, the profile itself) has been idle for ``inactive_days``. If the
  remaining tree still exceeds ``max_bytes``, caches go first, then the oldest
  inactive chat profiles, until the low-water mark. Live sessions never yield,
  and deliberately named sessions yield only to pressure after their full
  grace. Any protected overage is reported rather than mislabelled as
  reclaimed.
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

  profiles = _profile_inventory(
    root, chats, active_chat_ids, now=now, cutoff_seconds=cutoff_seconds,
    active_profile_names=active_profile_names,
  )

  bytes_before = sum(profile["size"] for profile in profiles)
  total = bytes_before
  cache_dirs_pruned = 0
  profiles_pruned = 0
  retained = []
  for profile in profiles:
    if profile["chat_id"] is not None and profile["retire_first"]:
      if _remove_profile(profile["path"]):
        total = max(0, total - profile["size"])
        profiles_pruned += 1
        continue
    retained.append(profile)

  pressure_triggered = total > max_bytes
  if pressure_triggered:
    # Chromium caches are disposable even for recently used chats. Prune them
    # from every CLOSED profile before considering deletion of any durable
    # profile state. Active profiles are excluded because Chromium may have
    # cache files mapped or locked while a turn is running.
    cache_candidates = sorted(
      (profile for profile in retained if not profile["active"]),
      key=lambda profile: profile["activity"],
    )
    profile_candidates = sorted(
      (
        profile for profile in retained
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
        if _remove_profile(profile["path"]):
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
