"""Narrow compatibility boundary around openai-codex private client seams.

The async SDK intentionally does not expose every control operation Möbius
needs. Keep knowledge of its private object graph here so a future SDK update
has one contract probe and one failure message instead of scattered attribute
access throughout the turn lifecycle.
"""

from __future__ import annotations

import time
from typing import Any, Callable


class CodexSdkContractError(RuntimeError):
  """The installed SDK no longer exposes the pinned control contract."""


def control_client(codex: Any) -> Any:
  """Return AsyncCodex's control client or fail with an actionable error."""
  client = getattr(codex, "_client", None)
  if client is None:
    raise CodexSdkContractError(
      "openai-codex API broken: AsyncCodex._client missing — "
      "pin a known-good version"
    )
  return client


def app_server_pid(codex: Any) -> int | None:
  """Return the private app-server child PID when the pinned SDK exposes it."""
  client = getattr(codex, "_client", None)
  sync_client = getattr(client, "_sync", None)
  process = getattr(sync_client, "_proc", None)
  pid = getattr(process, "pid", None)
  return pid if isinstance(pid, int) and pid > 1 else None


def wait_for_goal_snapshot(state: Any, timeout: float) -> Any | None:
  """Wait for the pinned SDK goal route's ordered status snapshot."""
  condition = getattr(state, "_condition", None)
  if condition is None:
    return getattr(state, "status", None)
  deadline = time.monotonic() + timeout
  with condition:
    while getattr(state, "status", None) is None:
      failure = getattr(state, "_failure", None)
      if failure is not None:
        raise failure
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        return None
      condition.wait(remaining)
    return state.status


def goal_notification_stream_type() -> type:
  """Return the pinned SDK's private asynchronous goal stream class."""
  from openai_codex._goal import _AsyncGoalNotificationStream

  return _AsyncGoalNotificationStream


def install_approval_handler(
  codex: Any,
  handler: Callable[[str, dict | None], dict],
) -> bool:
  """Install the sync-client approval callback through AsyncCodex.

  Returns ``False`` only for lightweight test fakes that omit the entire
  private chain. A real chain whose callback slot disappeared fails loudly.
  """
  client = getattr(codex, "_client", None)
  sync_client = getattr(client, "_sync", None)
  if sync_client is None:
    return False
  if not hasattr(sync_client, "_approval_handler"):
    raise CodexSdkContractError(
      "openai-codex API broken: CodexClient._approval_handler missing — "
      "pin a known-good version"
    )
  sync_client._approval_handler = handler
  return True
