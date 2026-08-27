"""One provider decision shared by chat display and execution."""

from app import models, providers
from app.chat_visibility import coerce_agent_settings


def resolve_chat_provider(
  chat: models.Chat,
  *,
  data_dir: str,
  running: bool,
  draining: bool,
) -> str:
  """Return the provider this chat should display and execute next.

  An explicit per-chat model is the strongest durable choice. Without one, a
  genuinely pristine owner chat follows the owner's latest picker default;
  app-owned, queued, running, draining, and already-started chats keep the
  provider committed on the chat row.
  """
  settings = coerce_agent_settings(chat.agent_settings_json)
  model_provider = providers.provider_of_model(settings.get("model"))
  if model_provider is not None:
    return model_provider

  follows_owner_default = (
    chat.created_by_app_id is None
    and not (chat.messages or [])
    and not (chat.pending_messages or [])
    and not running
    and not draining
  )
  if follows_owner_default:
    return providers.owner_default_provider(data_dir, chat.provider)
  return chat.provider or "claude"
