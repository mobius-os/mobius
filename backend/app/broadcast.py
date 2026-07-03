"""Per-chat event broadcast for SSE delivery.

Decouples the runner (Claude SDK / Codex subprocess) from SSE
subscribers.  The runner publishes events here; any number of SSE
clients can subscribe and receive a catch-up burst of prior events
plus live streaming.
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("moebius.broadcast")

# Hard cap on event_log entries. A long turn with rapid streaming-text events
# would otherwise grow the log without bound, consuming memory for the
# process lifetime of that broadcast (30s TTL after completion). When the
# cap is reached the OLDEST event is dropped so the tail (most recent
# state) is preserved for reconnect catch-up. The cap is generous — a
# typical turn produces far fewer events — and coalescing text chunks below
# keeps the effective count well under it.
_EVENT_LOG_MAX = 10_000

# Global registry of active broadcasts, keyed by chat_id.
_broadcasts: dict[str, "ChatBroadcast"] = {}

# A best-effort pointer to the most-recently-started chat's broadcast, for
# the notify endpoint which has no chat ID to target. This is NOT a "there is
# only one" guarantee: single-owner does not mean single-stream — two chats
# can stream concurrently, and `_broadcasts` holds them all per chat. The
# robust delivery path is the always-live system broadcast (notify always
# publishes there); this scalar is only the secondary "active chat" hint, so
# under concurrent turns it points at whichever started last. run_chat() sets
# it on start and clears it in its finally block.
_active_broadcast: "ChatBroadcast | None" = None


def set_active_broadcast(bc: "ChatBroadcast | None") -> None:
  """Track the broadcast for the currently running agent chat."""
  global _active_broadcast
  _active_broadcast = bc


def clear_active_broadcast_if(bc: "ChatBroadcast") -> bool:
  """Clear the process active-broadcast pointer ONLY if it still points at
  `bc`; return whether it did.

  The pointer is a single global tracking the one currently-streaming turn.
  A superseded run must not blindly clear it: a fresh turn that already called
  `set_active_broadcast` with its own broadcast owns the pointer now, and
  `set_active_broadcast(None)` would erase the live turn's pointer. But if NO
  fresh turn took over, the pointer is still this run's and must be cleared or
  it leaks. This identity-keyed compare-and-clear settles both: it clears (and
  returns True) only when `bc` is still the active broadcast.
  """
  global _active_broadcast
  if _active_broadcast is bc:
    _active_broadcast = None
    return True
  return False


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
    # The public watcher-presence contract is `app.presence.has_watchers`
    # — do NOT read this list from outside `broadcast.py` except for
    # observability (currently `routes/debug.py`'s subscriber_count
    # field, which is an inline read, not a presence check).
    self.subscribers: list[asyncio.Queue] = []
    self.running = True
    self.completed_at: Optional[float] = None

  def publish(self, event: dict):
    """Appends event to log and pushes to all subscriber queues.

    Coalesces adjacent streaming-text events in the log to bound memory use
    during long turns with many rapid chunks. The live push to subscribers
    is always the raw event — coalescing is log-only so in-flight streaming
    clients receive every chunk verbatim. A reconnecting client replays the
    coalesced log, which is semantically equivalent (same final text) with
    fewer entries.

    When the log reaches _EVENT_LOG_MAX the oldest entry is dropped to keep
    the cap. The tail (most recent state) is always preserved.
    """
    # Coalesce: if the last log entry is also a streaming-text chunk, merge
    # the content rather than appending a new entry. This keeps the log size
    # proportional to contiguous-text-run count rather than character count.
    # The runners publish streaming text as {"type": "text", "content": ...}
    # (see claude_sdk_runner / codex_sdk_runner and the wire contract in
    # CLAUDE.md); there is no "text_delta"/"index" event on the wire, so we
    # key on the real shape. A text_boundary event breaks the run, so distinct
    # assistant text blocks stay separate log entries.
    event_type = event.get("type")
    if (
      event_type == "text"
      and self.event_log
      and self.event_log[-1].get("type") == "text"
    ):
      # Merge into the last entry in-place (the entry is already in the log;
      # mutating it here is safe — subscribers got the original chunk live and
      # reconnect catch-up replays the coalesced form).
      prev = self.event_log[-1]
      self.event_log[-1] = dict(
        prev, content=(prev.get("content") or "") + (event.get("content") or "")
      )
    else:
      self.event_log.append(event)
      # Drop the oldest entry when the cap is exceeded to bound memory.
      if len(self.event_log) > _EVENT_LOG_MAX:
        self.event_log.pop(0)
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
    # Drop subscriber references so a completed broadcast doesn't look
    # "watched" to push.notify_owner's suppression check. An SSE client
    # whose generator never ran its `finally: unsubscribe(queue)` (server
    # SIGKILL, mid-flight disconnect that bypassed the finally) would
    # otherwise leave a stale queue in this list and silently suppress
    # push delivery for the next notification on this chat.
    self.subscribers = []
    # Schedule cleanup after TTL so late reconnectors can still
    # replay.  Fire-and-forget — the task is tied to the current
    # event loop and survives until TTL elapses.
    try:
      bc_ref = self
      asyncio.get_running_loop().call_later(
        _COMPLETED_TTL_SECS,
        lambda: (
          _broadcasts.pop(bc_ref.chat_id, None)
          if _broadcasts.get(bc_ref.chat_id) is bc_ref
          else None
        ),
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


class SystemBroadcast:
  """Process-lifetime event bus for shell-level system events
  (theme_updated, app_updated, shell_rebuild_*).

  Why this exists separately from ChatBroadcast: shell-level state
  (which app version is current, which theme is active) needs to
  reach the Shell regardless of which view the user is currently on.
  ChatBroadcasts are scoped to a single chat session — when the user
  is on the canvas (mini-app), settings, or a different chat than
  the one whose agent emitted the update, the per-chat broadcast
  has no shell-side subscriber and the event is dropped. Result: the
  iframe URL never bumps version, the SW serves the stale bundle,
  the user sees a spinner that never resolves.

  No catch-up: subscribers see only live events. Past system events
  are reconciled by polling the underlying state (GET /api/apps/,
  GET /api/theme) — there's no per-event-log replay because system
  events are notifications about state changes, not the state itself.
  """

  def __init__(self):
    self.subscribers: list[asyncio.Queue] = []

  def publish(self, event: dict) -> None:
    """Push an event to every live subscriber. Failures (queue full,
    closed) are logged + dropped — the publisher is the file watcher
    or the agent's POST /api/notify, neither of which can usefully
    block on a stuck subscriber."""
    for q in self.subscribers:
      try:
        q.put_nowait(event)
      except asyncio.QueueFull:
        log.warning(
          "system subscriber queue full, dropping %s",
          event.get("type", "?"),
        )

  def subscribe(self) -> asyncio.Queue:
    """Returns a queue that receives live events. The caller MUST
    call unsubscribe() in a finally block — a leaked queue keeps
    the subscriber list growing and silently consumes events that
    no one will read."""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    self.subscribers.append(q)
    return q

  def unsubscribe(self, q: asyncio.Queue) -> None:
    try:
      self.subscribers.remove(q)
    except ValueError:
      pass


_system_broadcast = SystemBroadcast()


def get_system_broadcast() -> SystemBroadcast:
  """Returns the process-wide system broadcast singleton."""
  return _system_broadcast
