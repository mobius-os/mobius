"""Project stored image dimensions into chat messages for stable first layout."""

import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import HTTPException

from app.image_previews import stored_image_dimensions
from app.generated_files import open_stored_file, stored_dir
from app.models import GeneratedFile
from app.path_utils import validate_path_within_base


def _media_path_pattern(chat_id: str) -> re.Pattern:
  # Filenames written by the upload and generated-media paths contain no
  # whitespace. Stop before Markdown/HTML delimiters and before query/hash data;
  # dimensions are keyed by pathname so auth/preview parameters never matter.
  return re.compile(
    rf"(?P<path>/api/chats/{re.escape(chat_id)}/"
    rf"(?P<kind>uploads|media|generated-files)/(?P<filename>[^\s?#)>\]\"']+))"
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


def _stored_dimensions(base: Path, filename: str) -> dict | None:
  try:
    file_path = validate_path_within_base(filename, base)
  except HTTPException:
    return None
  if not file_path.is_file():
    return None
  return stored_image_dimensions(file_path, base)


def _message_image_references(message: dict, pattern, chat_id: str):
  references = {}
  for markdown in _message_markdown(message):
    for match in pattern.finditer(markdown):
      references.setdefault(match.group("path"),
        (match.group("kind"), unquote(match.group("filename"))))
  for block in message.get("blocks") or []:
    if not isinstance(block, dict):
      continue
    files = (
      block.get("files") if block.get("type") == "generated_files"
      else block.get("generated_files") if block.get("type") == "tool"
      else None
    )
    for file in files if isinstance(files, list) else []:
      if (isinstance(file, dict) and isinstance(file.get("name"), str)
          and str(file.get("mime_type", "")).startswith("image/")
          and file.get("previewable") is True):
        name = file["name"]
        encoded_name = quote(name, safe="~!*'()")
        references.setdefault(f"/api/chats/{chat_id}/generated-files/{encoded_name}",
          ("generated-files", name))
  return references


def _generated_dimensions(row, data_dir: str, chat_id: str):
  if row is None or not row.mime_type.startswith("image/"):
    return None
  try:
    base = stored_dir(data_dir, chat_id)
    fd, _ = open_stored_file(data_dir, chat_id, row.path)
  except (OSError, ValueError):
    return None
  try:
    # Read/cache measurements from the exact authorized inode. The procfs path
    # remains anchored while the descriptor is held, including during swaps.
    return stored_image_dimensions(Path(f"/proc/self/fd/{fd}"), base)
  finally:
    os.close(fd)


def project_message_image_dimensions(
  messages: list[dict],
  *,
  chat_id: str,
  data_dir: str,
  db=None,
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
  message_references = [_message_image_references(message, pattern, chat_id) for message in messages]
  generated_names = {filename for references in message_references
    for kind, filename in references.values() if kind == "generated-files"}
  generated_rows = {
    row.name: row for row in db.query(GeneratedFile).filter(
      GeneratedFile.chat_id == chat_id,
      GeneratedFile.name.in_(generated_names),
    ).all()
  } if generated_names and db is not None else {}
  generated_dimensions = {
    name: _generated_dimensions(row, data_dir, chat_id)
    for name, row in generated_rows.items()
  }

  for message_index, message in enumerate(messages):
    references = message_references[message_index]
    if not references:
      continue

    # Every referenced path gets an entry. ``None`` says the server looked and
    # the image is unreadable (invalid path, missing file or undecodable
    # bytes), so the renderer shows an error. A path with no entry is merely
    # unknown to this map, e.g. text streamed after the response was built,
    # and keeps the default frame.
    dimensions = {}
    for url_path, (kind, filename) in references.items():
      dimensions[url_path] = (
        generated_dimensions.get(filename)
        if kind == "generated-files"
        else _stored_dimensions(chat_root / kind, filename)
      )

    if projected is None:
      projected = list(messages)
    next_message = dict(message)
    next_message["media_dimensions"] = dimensions
    projected[message_index] = next_message

  return projected if projected is not None else messages
