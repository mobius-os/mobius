"""Provider-neutral admission for messages steered into a live chat turn."""

from app import claude_sdk_runner, codex_sdk_runner
from app.runner_registry import RunnerKind, registry


def has_live_steerable_turn(chat_id: str, provider: str) -> bool:
  """Whether the current provider handle can accept an in-band message."""
  if provider == "claude":
    return isinstance(
      registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK),
      claude_sdk_runner.ActiveClaudeClient,
    )
  handle = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  return (
    isinstance(handle, codex_sdk_runner.ActiveCodexTurn)
    and handle.is_steerable
  )


def steering_preserves_inflight_work(provider: str) -> bool:
  """Whether steering a message into a live turn leaves in-flight tools running.

  Codex injects the steered text natively into the running turn, so a tool call
  already in flight keeps running. Claude has no in-band inject: its only
  mid-turn lever is `client.interrupt()`, which aborts the in-flight step. The
  delegation wake path consults this before steering an unsolicited helper
  result into a running parent, and otherwise defers to after-turn delivery so
  the parent's live tool call is never cut. Owner-authored steers interrupt by
  design regardless.
  """
  if provider == "claude":
    return claude_sdk_runner.STEER_PRESERVES_INFLIGHT_WORK
  return codex_sdk_runner.STEER_PRESERVES_INFLIGHT_WORK


async def steer_into_active_turn(
  provider: str,
  chat_id: str,
  content: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Admit a durably reserved message without awaiting provider settlement."""
  if provider == "claude":
    return await claude_sdk_runner.steer_into_active_turn(
      chat_id, content, user_msgs, consume_pending_cids,
    )
  return await codex_sdk_runner.steer_into_active_turn(
    chat_id, content, user_msgs, consume_pending_cids,
  )
