"""In-memory relay that lets the owner's chat agents use folders shared from a browser.

The browser keeps the folder permission and does every file operation. This
module only remembers which live shell system-stream subscription reported
which folders, forwards one command to that exact subscription, and waits for
its answer. It also tells every owner shell what agents are working in, and
holds a Stop from any device until the browser holding the folder confirms
it. It never reads or stores file contents, and it forgets everything on
restart: a shell re-reports its folders whenever its stream reconnects.

Every method runs on the event loop that owns the system broadcast (the routes
are ``async``), and no mutation awaits, so the maps need no lock.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


FolderMode = Literal["read", "edit"]
FolderAccess = Literal["remembered", "visit"]
FolderState = Literal["ready", "needs_permission", "needs_pick", "paused"]

READ_OPERATIONS = frozenset({"list", "info", "read", "image"})
EDIT_OPERATIONS = frozenset({"write", "move", "mkdir", "bin"})

COMMAND_TIMEOUT_SECONDS = 30.0
# A change still queued behind earlier ones this long after it reached the
# browser is dropped there. The margin before the timeout covers delivery, so a
# change never starts after the agent was told it timed out and retried.
COMMAND_START_WITHIN_SECONDS = 25.0
LIVENESS_POLL_SECONDS = 2.0
MAX_PENDING_PER_STREAM = 16
MAX_REMEMBERED_DEVICES = 32
COMMAND_EVENT_TYPE = "shared_folder_command"
ACTIVITY_EVENT_TYPE = "shared_folder_activity"
PAUSE_EVENT_TYPE = "shared_folder_pause"

# Folder states that are not ready, mapped to the stable code and the
# owner-actionable message an agent relays.
_UNREADY = {
  "needs_permission": (
    "needs_permission",
    "The browser needs the owner to tap Reconnect for this folder in Settings → Shared folders.",
  ),
  "needs_pick": (
    "needs_pick",
    "This browser forgets folders between visits. Ask the owner to tap Pick again in Settings → Shared folders.",
  ),
  "paused": (
    "paused",
    "The owner paused this folder. Ask them to resume it in Settings → Shared folders.",
  ),
}

_CHANGE_MAY_HAVE_HAPPENED = (
  "The change may still have happened: check with `folders info` or `folders ls` before trying it again."
)


@dataclass(frozen=True)
class SharedFolder:
  id: str
  name: str
  mode: FolderMode
  access: FolderAccess
  state: FolderState


@dataclass(frozen=True)
class DeviceReport:
  subscription_id: str
  device_id: str
  device_label: str
  sequence: int
  folders: tuple[SharedFolder, ...]
  reported_at: float = field(default_factory=time.time)


@dataclass
class _PendingCommand:
  subscription_id: str
  future: asyncio.Future
  folder: SharedFolder
  device: DeviceReport


def failure(code: str, message: str) -> dict[str, Any]:
  return {"ok": False, "code": code, "error": message}


class SharedFolderRegistry:
  """Process-lifetime owner of device folder reports and in-flight commands."""

  def __init__(
    self,
    is_live: Callable[[str], bool],
    deliver: Callable[[str, dict[str, Any]], bool],
  ) -> None:
    self._is_live = is_live
    self._deliver = deliver
    self._reports: dict[str, DeviceReport] = {}
    self._pending: dict[str, _PendingCommand] = {}
    # Folders the owner stopped from some device that a live holder has not yet
    # confirmed paused. Refused here meanwhile, on every device.
    self._stopped: set[str] = set()

  def report(self, report: DeviceReport) -> bool:
    """Record a live stream's folders; False when that stream is gone."""
    if not self._is_live(report.subscription_id):
      return False
    current = self._reports.get(report.subscription_id)
    if current is None or report.sequence > current.sequence:
      self._reports[report.subscription_id] = report
      self._release_confirmed_stops()
      # A holder that missed the Stop (closed then) is told again.
      unpaused = {folder.id for folder in report.folders if folder.state != "paused"}
      missed = sorted(self._stopped & unpaused)
      if missed:
        self._deliver(report.subscription_id, {"type": PAUSE_EVENT_TYPE, "folderIds": missed})
    self._prune()
    return True

  def _release_confirmed_stops(self) -> None:
    """Lift a Stop only once a live holder keeps the pause and none lacks it.

    Several tabs of one browser hold the same remembered folder. Releasing on
    the first one's confirmation would let a command reach another tab that
    has not paused yet. With no live holder at all, nothing has confirmed the
    pause, so the Stop stays and a holder that returns unpaused is told again.
    From release on, the owner resumes in the browser.
    """
    paused: set[str] = set()
    unpaused: set[str] = set()
    for report in self._reports.values():
      if self._is_live(report.subscription_id):
        for folder in report.folders:
          (paused if folder.state == "paused" else unpaused).add(folder.id)
    self._stopped -= paused - unpaused

  def pause(self, folder_ids: list[str] | None = None) -> list[str]:
    """Stop folders on every device at once: refuse here, and tell their browsers."""
    known = {folder.id for _, folder in self._best_folders()}
    chosen = known if folder_ids is None else known & set(folder_ids)
    self._stopped |= chosen
    if chosen:
      event = {"type": PAUSE_EVENT_TYPE, "folderIds": sorted(chosen)}
      for subscription_id in self._owner_streams():
        self._deliver(subscription_id, event)
    return sorted(chosen)

  def folders(self) -> list[dict[str, Any]]:
    """Every remembered folder, one entry per folder, with liveness."""
    listed = []
    for report, folder in self._best_folders():
      online = self._is_live(report.subscription_id)
      state = "paused" if folder.id in self._stopped else folder.state
      listed.append({
        "id": folder.id,
        "name": folder.name,
        "mode": folder.mode,
        "access": folder.access,
        "state": state if online else "offline",
        "device": {"id": report.device_id, "label": report.device_label},
        "online": online,
      })
    return listed

  async def issue(self, folder_id: str, command: dict[str, Any]) -> dict[str, Any]:
    """Send one command to the browser holding ``folder_id`` and await its answer."""
    target = self._live_folder(folder_id)
    if isinstance(target, dict):
      return target
    report, folder = target
    if folder.id in self._stopped:
      return failure(*_UNREADY["paused"])
    refusal = _refusal(folder, command["op"])
    if refusal is not None:
      return refusal
    if self._pending_for(report.subscription_id) >= MAX_PENDING_PER_STREAM:
      return failure("busy", "The browser is still working on earlier commands. Try again shortly.")
    command_id = secrets.token_urlsafe(12)
    future = asyncio.get_running_loop().create_future()
    self._pending[command_id] = _PendingCommand(report.subscription_id, future, folder, report)
    event = {
      "type": COMMAND_EVENT_TYPE, "commandId": command_id, "folderId": folder.id,
      "startWithinMs": int(COMMAND_START_WITHIN_SECONDS * 1000), **command,
    }
    if not self._deliver(report.subscription_id, event):
      self._pending.pop(command_id, None)
      return _offline(report)
    self._announce_activity()
    try:
      outcome = await self._await_answer(command_id, report)
    finally:
      self._pending.pop(command_id, None)
      self._announce_activity()
    if command["op"] in EDIT_OPERATIONS and outcome.get("code") in ("timeout", "offline"):
      # The browser may still finish a change it already started.
      outcome = {**outcome, "error": f"{outcome['error']} {_CHANGE_MAY_HAVE_HAPPENED}"}
    return outcome

  def answer(self, subscription_id: str, command_id: str, outcome: dict[str, Any]) -> bool:
    """Settle a pending command, only from the stream it was sent to."""
    pending = self._pending.get(command_id)
    if pending is None or pending.subscription_id != subscription_id or pending.future.done():
      return False
    pending.future.set_result(outcome)
    return True

  async def _await_answer(self, command_id: str, report: DeviceReport) -> dict[str, Any]:
    # Poll liveness while waiting, so a tab that closes mid-command fails fast
    # instead of holding the agent for the whole timeout.
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    future = self._pending[command_id].future
    while True:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        return failure("timeout", "The browser did not answer in time. Check that Möbius is still open there.")
      try:
        return await asyncio.wait_for(asyncio.shield(future), min(remaining, LIVENESS_POLL_SECONDS))
      except TimeoutError:
        if not self._is_live(report.subscription_id):
          return _offline(report)

  def _live_folder(self, folder_id: str) -> tuple[DeviceReport, SharedFolder] | dict[str, Any]:
    match = next(((r, f) for r, f in self._best_folders() if f.id == folder_id), None)
    if match is None:
      return failure("not_found", "No shared folder has that id. Run `folders list` to see shared folders.")
    if not self._is_live(match[0].subscription_id):
      return failure(
        "offline",
        "The browser that shares this folder is not open. Ask the owner to open Möbius there.",
      )
    return match

  def _best_folders(self) -> list[tuple[DeviceReport, SharedFolder]]:
    """Each folder from the report best able to serve it.

    Several tabs of one browser report the same remembered folders, while a
    Safari or Firefox folder is held only by the tab where it was picked. So a
    live report wins over a closed one, a ready copy over an unready one, and
    otherwise the newest report.
    """
    best: dict[str, tuple[tuple[bool, bool, float], DeviceReport, SharedFolder]] = {}
    for report in self._reports.values():
      live = self._is_live(report.subscription_id)
      for folder in report.folders:
        rank = (live, folder.state == "ready", report.reported_at)
        current = best.get(folder.id)
        if current is None or rank > current[0]:
          best[folder.id] = (rank, report, folder)
    ordered = sorted(best.values(), key=lambda item: item[0], reverse=True)
    return [(report, folder) for _, report, folder in ordered]

  def _owner_streams(self) -> list[str]:
    """Live shells that reported folders: only the owner's own browsers do."""
    return [sid for sid in self._reports if self._is_live(sid)]

  def _announce_activity(self) -> None:
    """Show every owner tab, on every device, what agents are working in now.

    Sent to the reporting shells only, never broadcast: a guest's shell on
    the same system stream must not learn the owner's folder names.
    """
    working = list(self._pending.values())
    latest = working[-1] if working else None
    event: dict[str, Any] = {"type": ACTIVITY_EVENT_TYPE, "commands": len(working)}
    if latest is not None:
      event |= {
        "folderId": latest.folder.id,
        "folderName": latest.folder.name,
        "deviceId": latest.device.device_id,
        "deviceLabel": latest.device.device_label,
      }
    for subscription_id in self._owner_streams():
      self._deliver(subscription_id, event)

  def _pending_for(self, subscription_id: str) -> int:
    return sum(1 for pending in self._pending.values() if pending.subscription_id == subscription_id)

  def _prune(self) -> None:
    """Forget closed streams once a newer report covers their device, then cap."""
    newest_by_device: dict[str, float] = {}
    for report in self._reports.values():
      newest = newest_by_device.get(report.device_id, 0.0)
      newest_by_device[report.device_id] = max(newest, report.reported_at)
    for subscription_id, report in tuple(self._reports.items()):
      superseded = report.reported_at < newest_by_device[report.device_id]
      if superseded and not self._is_live(subscription_id):
        del self._reports[subscription_id]
    dead = sorted(
      (r for r in self._reports.values() if not self._is_live(r.subscription_id)),
      key=lambda r: r.reported_at,
    )
    while len(self._reports) > MAX_REMEMBERED_DEVICES and dead:
      del self._reports[dead.pop(0).subscription_id]


def _refusal(folder: SharedFolder, operation: str) -> dict[str, Any] | None:
  if folder.state in _UNREADY:
    return failure(*_UNREADY[folder.state])
  if operation in EDIT_OPERATIONS and (folder.mode != "edit" or folder.access != "remembered"):
    return failure(
      "read_only",
      "This folder is shared as Read only. The owner can share it again with Read & edit in Chrome or Edge.",
    )
  return None


def _offline(report: DeviceReport) -> dict[str, Any]:
  return failure(
    "offline",
    f"{report.device_label} is not connected. Ask the owner to open Möbius there.",
  )


def _system_is_live(subscription_id: str) -> bool:
  from app.broadcast import get_system_broadcast
  return get_system_broadcast().subscription(subscription_id) is not None


def _system_deliver(subscription_id: str, event: dict[str, Any]) -> bool:
  from app.broadcast import get_system_broadcast
  subscription = get_system_broadcast().subscription(subscription_id)
  if subscription is None:
    return False
  subscription.offer(event)
  return True


registry = SharedFolderRegistry(_system_is_live, _system_deliver)
