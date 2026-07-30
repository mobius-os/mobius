"""Chat naming policy for the immediate first-message fallback."""

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
  return content.strip()[:FIRST_MESSAGE_TITLE_MAX_CHARS]


def first_user_message_title(messages: list[object] | None) -> str:
  """Derive the fallback title from the first ordinary owner message."""
  for message in messages or []:
    if (
      not isinstance(message, dict)
      or message.get("role") != "user"
      or message.get("kind") == "auto_continuation"
    ):
      continue
    title = first_message_title(message.get("content"))
    if title:
      return title
  return ""
