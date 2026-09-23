"""Shared owner-drawer visibility policy for chats.

The drawer list and every derived view of that list (notably full-text search)
must use one predicate. Keeping the legacy JSON coercion here also makes old
text-backed rows behave identically at every caller.
"""

import json

from app import models


def coerce_agent_settings(raw) -> dict:
  """Return a fresh dict from a dict or legacy JSON string; otherwise empty."""
  if raw is None:
    return {}
  if isinstance(raw, dict):
    return dict(raw)
  if isinstance(raw, str):
    try:
      parsed = json.loads(raw)
      return dict(parsed) if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
      return {}
  return {}


def visible_in_owner_drawer(chat: models.Chat) -> bool:
  """Whether an active chat belongs in the owner's primary chat history."""
  settings = coerce_agent_settings(chat.agent_settings_json)
  # Explicit drawer_hidden wins in either direction. Autopilot follow-up chats
  # use it to become visible only while owner attention is required.
  hidden = settings.get("drawer_hidden")
  if hidden is not None:
    return not bool(hidden)
  if chat.created_by_app_id is None:
    return True
  return settings.get("owner_visible") is True


def provider_switch_allowed(chat: models.Chat) -> bool:
  """Whether the owner-facing provider picker may move this chat.

  Hidden chats are autonomous execution records (app work, delegations, or
  background follow-ups) whose provider is fixed when the record is created.
  A chat becomes switchable only while the same durable visibility policy puts
  it in the owner's drawer. Keeping this predicate beside drawer visibility
  gives every route and the writer's final commit gate one answer.
  """
  return visible_in_owner_drawer(chat)
