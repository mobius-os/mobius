"""Shared execution policy for provider-native helper agents.

Möbius exposes one bounded-task contract while retaining two executors during
the parity period: provider-native helpers for inline work and supervised child
chats for durable work.  This module owns the reversible boundary that can
remove the native executor once the durable path matches its coordination and
resource profile.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


NATIVE_AGENT_TOOLS = (
  "Task", "TaskOutput", "TaskStop", "Workflow", "Workflows", "Agent",
)
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def _enabled(
  name: str, *, default: bool, environ: Mapping[str, str],
) -> bool:
  raw = environ.get(name)
  if raw is None:
    return default
  return raw.strip().lower() not in _OFF_VALUES


def native_subagents_enabled(
  provider: str, *, environ: Mapping[str, str] | None = None,
) -> bool:
  """Whether an owner chat may receive provider-native agent tools.

  ``MOEBIUS_NATIVE_SUBAGENTS=off`` is the provider-neutral retirement switch.
  The older Codex-only switch remains honored during the interface migration so
  an operator's existing rollback setting does not silently change meaning.
  Durable delegated children are unaffected; their own run policy always
  blocks recursive delegation at the provider boundary.
  """
  values = os.environ if environ is None else environ
  if not _enabled(
    "MOEBIUS_NATIVE_SUBAGENTS", default=True, environ=values,
  ):
    return False
  if provider.lower() == "codex":
    return _enabled(
      "MOEBIUS_CODEX_MULTI_AGENT", default=True, environ=values,
    )
  return True
