"""Capture immutable files that an agent deliberately offers for download.

Each chat gets a small inbox through ``MOBIUS_GENERATED_DIR``. At the end of a
turn, the runner copies each finished file into an immutable managed store,
persists its metadata, and consumes the inbox entry. A crash leaves the inbox
entry for the next turn; provider working paths never become an authorization
boundary.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import uuid
from urllib.parse import quote

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = frozenset({
  ".pdf",
  ".doc", ".docx", ".odt",
  ".xls", ".xlsx", ".ods", ".csv",
  ".ppt", ".pptx", ".odp",
  ".png", ".jpg", ".jpeg", ".gif", ".svg",
  ".zip",
  ".mp3", ".wav",
  ".mp4",
})

MAX_RECORDED_BYTES = 100 * 1024 * 1024
MAX_CANDIDATES_PER_TURN = 20
MAX_RECORDED_ROWS_PER_CHAT = 500

def _chat_root(data_dir: str, chat_id: str) -> Path:
  segment = quote(str(chat_id), safe="-_")
  if not segment or segment in {".", ".."}:
    raise ValueError("invalid chat id for deliverable directory")
  return Path(data_dir) / "chats" / segment / "deliverables"


def output_dir(data_dir: str, chat_id: str, *, create: bool = False) -> Path:
  """Directory exposed to the provider for finished deliverables."""
  directory = _chat_root(data_dir, chat_id) / "inbox"
  if create:
    directory.mkdir(parents=True, exist_ok=True)
  return directory


def stored_dir(data_dir: str, chat_id: str, *, create: bool = False) -> Path:
  """Private immutable store used by the authenticated download route."""
  directory = _chat_root(data_dir, chat_id) / "files"
  if create:
    directory.mkdir(parents=True, exist_ok=True)
  return directory


def delivery_instruction(directory: Path) -> str:
  return (
    "When you create a final user-facing deliverable such as a PDF, document, "
    "spreadsheet, presentation, image, archive, audio, or video file, save the "
    f"finished file directly in {directory}. That path is also available as "
    "$MOBIUS_GENERATED_DIR. Keep temporary and source files outside it."
  )


def _inbox_names(data_dir: str, chat_id: str) -> list[str]:
  """List the next bounded batch; skipped entries remain queued for later."""
  directory = output_dir(data_dir, chat_id)
  try:
    with os.scandir(directory) as entries:
      names = sorted(
        entry.name
        for entry in entries
        if (
          not entry.is_symlink()
          and entry.is_file(follow_symlinks=False)
          and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS
        )
      )
  except OSError:
    return []
  return names[:MAX_CANDIDATES_PER_TURN]


def _freeze_file(data_dir: str, chat_id: str, name: str) -> dict | None:
  """Copy one flat-inbox file into immutable storage without following links."""
  source = output_dir(data_dir, chat_id) / name
  destination_dir = stored_dir(data_dir, chat_id, create=True)
  stored_name = f"{uuid.uuid4().hex}{source.suffix.lower()}"
  destination = destination_dir / stored_name
  temporary = destination_dir / f".{stored_name}.tmp"

  source_fd = destination_fd = None
  try:
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    initial = os.fstat(source_fd)
    if initial.st_size > MAX_RECORDED_BYTES:
      return None
    destination_fd = os.open(
      temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
    )
    with os.fdopen(source_fd, "rb", closefd=False) as src, os.fdopen(
      destination_fd, "wb", closefd=False,
    ) as dst:
      shutil.copyfileobj(src, dst, length=1024 * 1024)
    final = os.fstat(source_fd)
    copied = os.fstat(destination_fd)
    if (
      (initial.st_mtime_ns, initial.st_size) != (final.st_mtime_ns, final.st_size)
      or copied.st_size != final.st_size
    ):
      return None
    os.close(destination_fd)
    destination_fd = None
    os.replace(temporary, destination)
    return {
      "name": name,
      "path": stored_name,
      "size": final.st_size,
      "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
      "_source_identity": (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_size),
    }
  except OSError:
    log.debug("generated-file capture failed for %s", source, exc_info=True)
    return None
  finally:
    if source_fd is not None:
      os.close(source_fd)
    if destination_fd is not None:
      os.close(destination_fd)
    temporary.unlink(missing_ok=True)


def _settle_capture(
  data_dir: str, chat_id: str, captured: dict, *, accepted: bool,
) -> None:
  frozen = stored_dir(data_dir, chat_id) / captured["path"]
  if not accepted:
    frozen.unlink(missing_ok=True)
    return

  source = output_dir(data_dir, chat_id) / captured["name"]
  try:
    current = source.stat(follow_symlinks=False)
  except OSError:
    return
  identity = (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
  if identity == captured["_source_identity"] and not source.is_symlink():
    source.unlink(missing_ok=True)


async def publish_inbox_files(sink, *, data_dir: str, chat_id: str) -> None:
  """Freeze, persist, and consume the next batch of completed deliverables."""
  names = await asyncio.to_thread(_inbox_names, data_dir, chat_id)
  for name in names:
    captured = await asyncio.to_thread(_freeze_file, data_dir, chat_id, name)
    if captured is None:
      continue
    event = {
      "type": "generated_file",
      **{key: value for key, value in captured.items() if not key.startswith("_")},
    }
    try:
      accepted = bool(await sink.publish_generated_file(event))
    except Exception:
      await asyncio.to_thread(
        _settle_capture, data_dir, chat_id, captured, accepted=False,
      )
      raise
    await asyncio.to_thread(
      _settle_capture, data_dir, chat_id, captured, accepted=accepted,
    )
