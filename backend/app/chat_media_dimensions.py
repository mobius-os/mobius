"""Project stored image dimensions into chat messages for stable first layout."""

import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException

from app.image_previews import stored_image_dimensions
from app.path_utils import validate_path_within_base


def _media_path_pattern(chat_id: str) -> re.Pattern:
  # Filenames written by the upload and generated-media paths contain no
  # whitespace. Stop before Markdown/HTML delimiters and before query/hash data;
  # dimensions are keyed by pathname so auth/preview parameters never matter.
  return re.compile(
    rf"(?P<path>/api/chats/{re.escape(chat_id)}/"
    rf"(?P<kind>uploads|media)/(?P<filename>[^\s?#)>\]\"']+))"
  )


def _message_markdown(message: dict):
  content = message.get("content")
  if isinstance(content, str):
    yield content
  blocks = message.get("blocks")
  if not isinstance(blocks, list):
    return
  for block in blocks:
    if not isinstance(block, dict) or block.get("type") != "text":
      continue
    text = block.get("content")
    if isinstance(text, str):
      yield text


def project_message_image_dimensions(
  messages: list[dict],
  *,
  chat_id: str,
  data_dir: str,
) -> list[dict]:
  """Attach intrinsic dimensions to messages that reference local images.

  This is a response projection: persisted transcript JSON stays untouched.
  Each message owns only the paths it renders, which means pagination and live
  detail refreshes naturally carry the right metadata without a second cache
  merge protocol.
  """
  pattern = _media_path_pattern(chat_id)
  chat_root = Path(data_dir) / "chats" / chat_id
  projected: list[dict] | None = None

  for message_index, message in enumerate(messages):
    references: dict[str, tuple[str, str]] = {}
    for markdown in _message_markdown(message):
      for match in pattern.finditer(markdown):
        references.setdefault(
          match.group("path"),
          (match.group("kind"), unquote(match.group("filename"))),
        )
    if not references:
      continue

    dimensions = {}
    for url_path, (kind, filename) in references.items():
      base = chat_root / kind
      try:
        file_path = validate_path_within_base(filename, base)
      except HTTPException:
        # Validation failures are represented by absent metadata. The frontend
        # then shows the same explicit image error as an unreadable file.
        continue
      if not file_path.is_file():
        continue
      size = stored_image_dimensions(file_path, base)
      if size is not None:
        dimensions[url_path] = size

    if projected is None:
      projected = list(messages)
    next_message = dict(message)
    # An empty map is meaningful: the response understood this local image but
    # could not read valid dimensions, so the renderer errors instead of
    # guessing. Absence of the field is reserved for old cached/backend payloads
    # during a rolling frontend/backend reload.
    next_message["media_dimensions"] = dimensions
    projected[message_index] = next_message

  return projected if projected is not None else messages
