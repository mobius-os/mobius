"""Narrow compatibility boundary around Claude SDK private transport state."""

from __future__ import annotations

from typing import Any


def transport_process_pid(client: Any) -> int | None:
  """Return the private CLI child PID when the pinned SDK exposes it."""
  transport = getattr(client, "_transport", None)
  process = getattr(transport, "_process", None)
  pid = getattr(process, "pid", None)
  return pid if isinstance(pid, int) and pid > 1 else None
