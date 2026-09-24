"""Codex command-string helpers reused by codex_events.

`codex_events` reuses `_extract_bash_command` to render the Bash
command a `CommandExecutionThreadItem` carries (Codex wraps it in a
`/bin/bash -lc '…'` shell invocation). This module keeps that helper
side-effect-free so it stays trivially unit-testable.
"""

from __future__ import annotations


def _extract_bash_command(raw: str) -> str:
  """Strips Codex's /bin/bash -lc wrapper from a command string."""
  prefix = "/bin/bash -lc '"
  if raw.startswith(prefix) and raw.endswith("'"):
    return raw[len(prefix):-1]
  return raw
