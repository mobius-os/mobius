"""Name the chat-media picture a screenshot step saved.

The Möbius screenshot control tool saves each capture in its chat's media and
names that file in a fixed note after the image (`_call_screenshot` in
scripts/mobius_control_mcp.py): "Saved <data>/chats/<id>/media/<file>. To show
the owner, …". A screenshot step's input is a route or app id, never a path, so
the step records that file name as ``saved_image``. The chat then renders the
picture straight from chat media instead of downloading the step's whole stored
result (a base64 copy larger than the picture) just to read the note.

New steps are stamped by the event sink from the full result. Steps stored
before the field existed recover it on read from their persisted excerpt, which
keeps the note because it is the result's final line.
"""

from __future__ import annotations

import re

SAVED_IMAGE_FIELD = "saved_image"

# How each provider names the control server's screenshot tool.
SCREENSHOT_TOOLS = frozenset({
  "mcp__mobius_control__screenshot",
  "mobius_control:screenshot",
})

# Claude stores the image block followed by the note as plain text; Codex
# stores the MCP result as JSON, so there the note follows a quote. Base64 data
# contains neither a space nor a quote, so image bytes can never match.
_SAVED_NOTE = re.compile(
  r'(?:^|[\s"])Saved /\S*?/chats/(?P<chat>[A-Za-z0-9_-]+)/media/'
  r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*?)\.(?=\s|$)"
)
# The note follows the image, so only the result's end needs scanning.
_NOTE_SCAN_CHARS = 8192


def saved_screenshot_file(tool: object, output: object, chat_id: object) -> str | None:
  """Return the media file name a screenshot step saved in ``chat_id``.

  Only the screenshot tool's own note counts, and only for a file in this
  chat's media; a failed capture or a file saved elsewhere yields None.
  """
  if tool not in SCREENSHOT_TOOLS or not isinstance(output, str):
    return None
  if not isinstance(chat_id, str) or not chat_id:
    return None
  for match in _SAVED_NOTE.finditer(output[-_NOTE_SCAN_CHARS:]):
    if match["chat"] == chat_id:
      return match["name"]
  return None
