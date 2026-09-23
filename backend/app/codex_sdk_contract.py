"""Narrow compatibility boundary around openai-codex private client seams.

The async SDK intentionally does not expose every control operation Möbius
needs. Keep knowledge of its private object graph here so a future SDK update
has one contract probe and one failure message instead of scattered attribute
access throughout the turn lifecycle.
"""

from __future__ import annotations

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


async def start_turn_for_handle(
  codex: Any,
  thread_id: str,
  wire_input: Any,
  *,
  params: Any,
) -> tuple[Any, Any]:
  """Start a turn with the subscription reserved for its handle.

  The pinned SDK buffers notifications while ``turn/start`` is in flight only
  when its private ``_start_turn(..., for_handle=True)`` seam is used. Calling
  the public low-level ``turn_start`` first and subscribing afterward loses a
  fast completion and leaves the low-level default subscription registered.
  Keep that version-specific ownership rule inside this contract boundary.
  """
  client = control_client(codex)
  start = getattr(client, "_start_turn", None)
  if not callable(start):
    raise CodexSdkContractError(
      "openai-codex API broken: AsyncCodexClient._start_turn missing — "
      "pin a known-good version"
    )
  started, subscription = await start(
    thread_id,
    wire_input,
    params=params,
    for_handle=True,
  )
  if subscription is None:
    raise CodexSdkContractError(
      "openai-codex API broken: handle turn returned no subscription — "
      "pin a known-good version"
    )
  return started, subscription


def app_server_pid(codex: Any) -> int | None:
  """Return the private app-server child PID when the pinned SDK exposes it."""
  client = getattr(codex, "_client", None)
  sync_client = getattr(client, "_sync", None)
  process = getattr(sync_client, "_proc", None)
  pid = getattr(process, "pid", None)
  return pid if isinstance(pid, int) and pid > 1 else None


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
