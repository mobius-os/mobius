"""Chat naming policy for the immediate first-message fallback."""

from app.continuations import is_continuation_message
from app.goal_commands import goal_objective

FIRST_MESSAGE_TITLE_MAX_CHARS = 80


def first_message_title(content: object) -> str:
  """Return the bounded plain-text title used before an agent name is ready."""
  if isinstance(content, list):
    content = " ".join(
      part.get("text", "")
      for part in content
      if isinstance(part, dict)
    )
  if not isinstance(content, str):
    return ""
  content = content.strip()
  # The immediate drawer name is visible for the entire first Goal operation,
  # which may span many physical turns. Keep the control syntax out of that
  # user-facing fallback instead of waiting for the first summary publication.
  return (goal_objective(content) or content)[:FIRST_MESSAGE_TITLE_MAX_CHARS]


def first_user_message_title(messages: list[object] | None) -> str:
  """Derive the fallback title from the first ordinary owner message."""
  for message in messages or []:
    if (
      not isinstance(message, dict)
      or message.get("role") != "user"
      or is_continuation_message(message)
    ):
      continue
    title = first_message_title(message.get("content"))
    if title:
      return title
  return ""
