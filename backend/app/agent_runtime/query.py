"""`query()` — the entry point for the agent runtime.

Mirrors `claude_agent_sdk.query()`. Today it dispatches to the local
CLI backend (`cli_backend.query_cli`). Mid-June it will dispatch to
the SDK backend, with no change to chat.py's call shape.
"""

from __future__ import annotations

from typing import AsyncIterator

from app.agent_runtime.cli_backend import query_cli
from app.agent_runtime.options import AgentOptions
from app.agent_runtime.types import Message


async def query(
  prompt: str,
  options: AgentOptions | None = None,
  *,
  proc_handle: dict | None = None,
) -> AsyncIterator[Message]:
  """Run one agent turn; yield typed SDK-shaped messages until the
  turn ends (ResultMessage or ResultMessage-equivalent synthetic event
  for AskUserQuestion).

  The `proc_handle` dict is a mobius-specific escape hatch: the wrapper
  writes `proc_handle["proc"]` after spawn so chat.py can register the
  live subprocess in `_active_procs` for the Stop button. When the SDK
  backend lands, this becomes a no-op (the SDK exposes its own cancel
  primitive).
  """
  opts = options or AgentOptions()

  # Provider selection. Today only the Claude CLI is routed through
  # the wrapper; Codex continues to use providers.py's CodexProvider
  # directly until parity is verified.
  if opts.provider not in (None, "claude"):
    raise NotImplementedError(
      f"agent_runtime currently wraps only 'claude'; got {opts.provider!r}."
      " Use providers.py directly for other providers until parity work lands."
    )

  async for message in query_cli(prompt, opts, proc_handle=proc_handle):
    yield message
