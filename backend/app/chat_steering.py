"""Provider-neutral admission for messages steered into a live chat turn."""

from app.runner_registry import RunnerKind, registry


def _claude_engine(provider: str) -> bool:
  # App providers on the Messages protocol run on the Claude engine too.
  from app.providers import provider_runtime_kind
  return provider_runtime_kind(provider) == "claude_sdk"


def has_live_steerable_turn(chat_id: str, provider: str) -> bool:
  """Whether the current provider handle can accept an in-band message."""
  if _claude_engine(provider):
    handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
    if handle is None:
      return False
    from app import claude_sdk_runner

    return isinstance(handle, claude_sdk_runner.ActiveClaudeClient) and handle.is_steerable
  handle = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  if handle is None:
    return False
  from app import codex_sdk_runner

  return (
    isinstance(handle, codex_sdk_runner.ActiveCodexTurn)
    and handle.is_steerable
  )


async def steer_into_active_turn(
  provider: str,
  chat_id: str,
  content: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Admit a durably reserved message without awaiting provider settlement."""
  if _claude_engine(provider):
    from app import claude_sdk_runner

    return await claude_sdk_runner.steer_into_active_turn(
      chat_id, content, user_msgs, consume_pending_cids,
    )
  from app import codex_sdk_runner

  return await codex_sdk_runner.steer_into_active_turn(
    chat_id, content, user_msgs, consume_pending_cids,
  )
