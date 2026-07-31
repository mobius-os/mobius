"""Narrow compatibility boundary around Claude SDK private transport state."""

from __future__ import annotations

from typing import Any


def transport_process_pid(client: Any) -> int | None:
  """Return the private CLI child PID when the pinned SDK exposes it."""
  transport = getattr(client, "_transport", None)
  process = getattr(transport, "_process", None)
  pid = getattr(process, "pid", None)
  return pid if isinstance(pid, int) and pid > 1 else None


def transport_exit_error(client: Any) -> Any | None:
  """Return the typed transport outcome retained by the pinned SDK."""
  transport = getattr(client, "_transport", None)
  return getattr(transport, "_exit_error", None)
