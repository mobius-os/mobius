"""Per-chat event broadcast for SSE delivery.

Decouples the CLI subprocess from SSE clients.  The subprocess
publishes events here; any number of SSE clients can subscribe
and receive a catch-up burst of prior events plus live streaming.
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("moebius.broadcast")

# Global registry of active broadcasts, keyed by chat_id.
_broadcasts: dict[str, "ChatBroadcast"] = {}

# The notify endpoint needs to find the running broadcast without
# knowing the chat ID.  Since Möbius is single-owner, there is at
# most one active broadcast at a time.  run_chat() sets this on
# start and clears it in its finally block.
_active_broadcast: "ChatBroadcast | None" = None


def set_active_broadcast(bc: "ChatBroadcast | None") -> None:
  """Track the broadcast for the currently running agent chat."""
  global _active_broadcast
  _active_broadcast = bc


def get_active_broadcast() -> "ChatBroadcast | None":
  """Return the active broadcast, or None if no agent is running."""
  return _active_broadcast


# How long a completed broadcast stays alive for late reconnectors.
_COMPLETED_TTL_SECS = 30


class ChatBroadcast:
  """Event bus for a single chat's agent session."""

  def __init__(self, chat_id: str):
    self.chat_id = chat_id
    self.event_log: list[dict] = []
    self.subscribers: list[asyncio.Queue] = []
    self.running = True
    self.completed_at: Optional[float] = None

  def publish(self, event: dict):
    """Appends event to log and pushes to all subscriber queues."""
    self.event_log.append(event)
    for q in self.subscribers:
      try:
        q.put_nowait(event)
      except asyncio.QueueFull:
        log.warning(
          "subscriber queue full for chat %s, dropping %s event",
          self.chat_id, event.get("type", "?"),
        )

  def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
    """Returns (catch_up_events, live_queue) for a new subscriber."""
    q: asyncio.Queue = asyncio.Queue(maxsize=4096)
    catch_up = list(self.event_log)
    self.subscribers.append(q)
    return catch_up, q

  def unsubscribe(self, q: asyncio.Queue):
    """Removes a subscriber queue."""
    try:
      self.subscribers.remove(q)
    except ValueError:
      pass

  def mark_completed(self):
    """Marks the broadcast as done and schedules cleanup.

    Schedules a delayed pop from the global registry so broadcasts
    don't accumulate for chats whose SSE clients already disconnected
    (otherwise they'd only be cleaned up on next `get_broadcast` for
    the same chat_id — which may never come).
    """
    self.running = False
    self.completed_at = time.time()
    # Push a sentinel so subscribers unblock.
    for q in self.subscribers:
      try:
        q.put_nowait(None)
      except asyncio.QueueFull:
        pass
    log.info(
      "broadcast done chat_id=%s events=%d subscribers=%d",
      self.chat_id, len(self.event_log), len(self.subscribers),
    )
    # Schedule cleanup after TTL so late reconnectors can still
    # replay.  Fire-and-forget — the task is tied to the current
    # event loop and survives until TTL elapses.
    try:
      asyncio.get_running_loop().call_later(
        _COMPLETED_TTL_SECS,
        lambda: _broadcasts.pop(self.chat_id, None),
      )
    except RuntimeError:
      # No running loop (synchronous context) — the reactive
      # get_broadcast TTL check will handle cleanup later.
      pass


def get_all_active_broadcasts() -> list["ChatBroadcast"]:
  """Return all broadcasts that are still running (agent not finished)."""
  return [bc for bc in _broadcasts.values() if bc.running]


def get_broadcast(chat_id: str) -> Optional["ChatBroadcast"]:
  """Returns the active broadcast for a chat, or None."""
  bc = _broadcasts.get(chat_id)
  if bc and not bc.running and bc.completed_at:
    if time.time() - bc.completed_at > _COMPLETED_TTL_SECS:
      _broadcasts.pop(chat_id, None)
      return None
  return bc


def create_broadcast(chat_id: str) -> "ChatBroadcast":
  """Creates and registers a new broadcast for a chat."""
  # Clean up any stale broadcast.
  _broadcasts.pop(chat_id, None)
  bc = ChatBroadcast(chat_id)
  _broadcasts[chat_id] = bc
  log.info("broadcast created chat_id=%s", chat_id)
  return bc


def remove_broadcast(chat_id: str):
  """Removes a broadcast immediately."""
  _broadcasts.pop(chat_id, None)
