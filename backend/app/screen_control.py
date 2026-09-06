"""In-memory relay for an owner-granted live browser control session.

The browser remains the authority and executor: this module never evaluates
JavaScript or exposes a browser-debugging port.  It only pairs one selected
browser surface with the ordinary agent token for the exact chat the owner
named, relays a closed command vocabulary, and forgets everything on stop,
expiry, or server restart.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any


SESSION_TTL_SECONDS = 15 * 60
COMMAND_TIMEOUT_SECONDS = 45


@dataclass
class ScreenControlSession:
  id: str
  owner_username: str
  app_id: int
  chat_id: str
  route: str
  viewport: dict[str, float]
  created_at: float
  expires_at: float
  commands: asyncio.Queue[dict[str, Any] | None] = field(
    default_factory=asyncio.Queue,
  )
  pending: dict[str, asyncio.Future] = field(default_factory=dict)
  browser_connections: int = 0
  closed_reason: str | None = None

  @property
  def active(self) -> bool:
    return self.closed_reason is None and self.expires_at > time.time()


class ScreenControlRegistry:
  """One-process owner of transient live-control sessions and responses."""

  def __init__(self) -> None:
    self._lock = asyncio.Lock()
    self._by_id: dict[str, ScreenControlSession] = {}
    self._by_chat: dict[str, str] = {}

  def _close_locked(self, session: ScreenControlSession, reason: str) -> None:
    if session.closed_reason is not None:
      return
    session.closed_reason = reason
    self._by_chat.pop(session.chat_id, None)
    try:
      session.commands.put_nowait(None)
    except asyncio.QueueFull:  # pragma: no cover - the queue is unbounded
      pass
    outcome = {"ok": False, "error": reason}
    for future in session.pending.values():
      if not future.done():
        future.set_result(outcome)
    session.pending.clear()

  def _prune_locked(self) -> None:
    now = time.time()
    for session in tuple(self._by_id.values()):
      if session.closed_reason is None and session.expires_at <= now:
        self._close_locked(session, "The shared-screen session expired.")
    # Closed sessions carry no authority. Retain no registry history—the chat
    # transcript is the owner's audit surface and the relay is intentionally
    # restart-ephemeral.
    for session_id, session in tuple(self._by_id.items()):
      if session.closed_reason is not None:
        self._by_id.pop(session_id, None)

  async def start(
    self,
    *,
    owner_username: str,
    app_id: int,
    chat_id: str,
    route: str,
    viewport: dict[str, float],
  ) -> ScreenControlSession:
    async with self._lock:
      self._prune_locked()
      prior_id = self._by_chat.get(chat_id)
      prior = self._by_id.get(prior_id) if prior_id else None
      if prior is not None:
        self._close_locked(prior, "A newer shared-screen session replaced this one.")
        self._by_id.pop(prior.id, None)
      now = time.time()
      session = ScreenControlSession(
        id=secrets.token_urlsafe(24),
        owner_username=owner_username,
        app_id=app_id,
        chat_id=chat_id,
        route=route,
        viewport=viewport,
        created_at=now,
        expires_at=now + SESSION_TTL_SECONDS,
      )
      self._by_id[session.id] = session
      self._by_chat[chat_id] = session.id
      return session

  async def get_for_browser(
    self, session_id: str, owner_username: str,
  ) -> ScreenControlSession | None:
    async with self._lock:
      self._prune_locked()
      session = self._by_id.get(session_id)
      if session is None or session.owner_username != owner_username:
        return None
      return session

  async def get_for_chat(
    self, chat_id: str, owner_username: str,
  ) -> ScreenControlSession | None:
    async with self._lock:
      self._prune_locked()
      session_id = self._by_chat.get(chat_id)
      session = self._by_id.get(session_id) if session_id else None
      if session is None or session.owner_username != owner_username:
        return None
      return session

  async def connect_browser(
    self, session_id: str, owner_username: str,
  ) -> ScreenControlSession | None:
    async with self._lock:
      self._prune_locked()
      session = self._by_id.get(session_id)
      if session is None or session.owner_username != owner_username:
        return None
      session.browser_connections += 1
      return session

  async def disconnect_browser(self, session_id: str) -> None:
    async with self._lock:
      session = self._by_id.get(session_id)
      if session is None:
        return
      session.browser_connections = max(0, session.browser_connections - 1)
      if session.browser_connections == 0:
        # The browser client does not reconnect a capture stream after its
        # event channel disappears. Retaining the session as active here made
        # agents see a live-looking but permanently unusable grant until TTL.
        self._close_locked(session, "The shared browser disconnected.")

  async def stop(
    self, session: ScreenControlSession, reason: str = "Screen sharing stopped.",
  ) -> None:
    async with self._lock:
      current = self._by_id.get(session.id)
      if current is None:
        return
      self._close_locked(current, reason)
      self._by_id.pop(current.id, None)

  async def issue_command(
    self,
    session: ScreenControlSession,
    command: dict[str, Any],
  ) -> dict[str, Any]:
    command_id = secrets.token_urlsafe(12)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    async with self._lock:
      current = self._by_id.get(session.id)
      if current is None or not current.active:
        return {"ok": False, "error": "No active shared-screen session."}
      if current.browser_connections < 1:
        return {"ok": False, "error": "The shared browser is not connected."}
      current.pending[command_id] = future
      current.commands.put_nowait({"commandId": command_id, **command})
    try:
      return await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
      async with self._lock:
        current = self._by_id.get(session.id)
        if current is not None:
          current.pending.pop(command_id, None)
      return {"ok": False, "error": "The shared browser did not answer in time."}

  async def answer(
    self,
    session: ScreenControlSession,
    command_id: str,
    outcome: dict[str, Any],
  ) -> bool:
    async with self._lock:
      current = self._by_id.get(session.id)
      if current is None:
        return False
      future = current.pending.pop(command_id, None)
      if future is None or future.done():
        return False
      future.set_result(outcome)
      return True

  async def reset_for_tests(self) -> None:
    async with self._lock:
      for session in tuple(self._by_id.values()):
        self._close_locked(session, "Test reset.")
      self._by_id.clear()
      self._by_chat.clear()


registry = ScreenControlRegistry()
