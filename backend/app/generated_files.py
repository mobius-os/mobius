"""Capture immutable files that an agent deliberately offers for download.

Each chat gets a small inbox through ``MOBIUS_GENERATED_DIR``. At the end of a
turn, the runner copies each finished file into an immutable managed store,
persists its metadata, and consumes the inbox entry. A crash leaves the inbox
entry for the next turn; provider working paths never become an authorization
boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
import stat
import uuid
from urllib.parse import quote

log = logging.getLogger(__name__)

INLINE_PREVIEW_MIME_TYPES = frozenset({
  "application/pdf",
  "audio/mpeg",
  "audio/wav",
  "audio/x-wav",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/avif",
  "image/bmp",
  "video/mp4",
})

VIEWED_RASTER_MIME_TYPES = frozenset({
  "image/avif", "image/bmp", "image/gif", "image/jpeg", "image/png", "image/webp",
})


def previewable_mime_type(mime_type: str) -> bool:
  return mime_type in INLINE_PREVIEW_MIME_TYPES

MAX_RECORDED_BYTES = 100 * 1024 * 1024
MAX_CANDIDATES_PER_TURN = 20
MAX_RECORDED_ROWS_PER_CHAT = 500

# A timed-out writer acknowledgement is not a rejection: the serialized
# command may already be running and can still commit. Keep both the inbox
# source and frozen copy until a later turn can reconcile/retry rather than
# deleting bytes that a late durable row may reference.
PUBLICATION_UNCERTAIN = object()

def _chat_root(data_dir: str, chat_id: str) -> Path:
  segment = quote(str(chat_id), safe="-_")
  if not segment or segment in {".", ".."}:
    raise ValueError("invalid chat id for deliverable directory")
  return Path(data_dir) / "chats" / segment / "deliverables"


def _open_directory(directory: Path, *, create: bool) -> int:
  """Open a directory without following any symlinked path component.

  The provider can write the inbox, so neither that inbox nor its managed
  sibling may redefine the storage root with a symlink. Walking from the
  configured data root with ``O_NOFOLLOW`` makes confinement an OS-enforced
  property rather than a path-string check.
  """
  parts = directory.parts
  if not directory.is_absolute() or len(parts) < 2:
    raise OSError("generated-file directory must be absolute")
  flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
  fd = os.open(parts[0], flags)
  try:
    for part in parts[1:]:
      if create:
        try:
          os.mkdir(part, mode=0o700, dir_fd=fd)
        except FileExistsError:
          pass
      child = os.open(part, flags, dir_fd=fd)
      os.close(fd)
      fd = child
  except Exception:
    os.close(fd)
    raise
  return fd


def _verified_directory(directory: Path, *, create: bool) -> Path:
  """Verify a directory path for callers that only need its display path."""
  fd = _open_directory(directory, create=create)
  os.close(fd)
  return directory


def output_dir(data_dir: str, chat_id: str, *, create: bool = False) -> Path:
  """Directory exposed to the provider for finished deliverables."""
  directory = _chat_root(data_dir, chat_id) / "inbox"
  return _verified_directory(directory, create=create)


def stored_dir(data_dir: str, chat_id: str, *, create: bool = False) -> Path:
  """Private immutable store used by the authenticated download route."""
  directory = _chat_root(data_dir, chat_id) / "files"
  return _verified_directory(directory, create=create)


def delivery_instruction(directory: Path) -> str:
  return (
    "Respond in chat by default, including reports, reviews, plans, and summaries. "
    "Create downloadable deliverables only when the owner explicitly requests "
    "a file, download, export, or a file-based output such as a PDF, spreadsheet, "
    "presentation, image, archive, audio, or video. A request for a report or plan "
    "alone is not a request for an attachment; neither is a long or substantial "
    "answer. Do not also create a Markdown document or other downloadable copy "
    "of an in-chat answer unless requested. When a deliverable is requested, "
    f"save the finished file directly in {directory}. That path is also available as "
    "$MOBIUS_GENERATED_DIR. Keep temporary and source files outside it."
  )


def _inbox_names(data_dir: str, chat_id: str) -> list[str]:
  """List a bounded batch of regular files without guessing file formats."""
  directory_fd = None
  try:
    directory_fd = _open_directory(
      _chat_root(data_dir, chat_id) / "inbox", create=False,
    )
    with os.scandir(directory_fd) as entries:
      names = []
      for entry in entries:
        try:
          info = entry.stat(follow_symlinks=False)
        except OSError:
          continue
        if (
          stat.S_ISREG(info.st_mode)
          and info.st_size <= MAX_RECORDED_BYTES
        ):
          names.append(entry.name)
  except OSError:
    return []
  finally:
    if directory_fd is not None:
      os.close(directory_fd)
  return sorted(names)[:MAX_CANDIDATES_PER_TURN]


def _freeze_file(data_dir: str, chat_id: str, name: str) -> dict | None:
  """Copy one flat-inbox file into immutable storage without following links."""
  source_fd = destination_fd = inbox_fd = stored_fd = None
  temporary_name = None
  try:
    if Path(name).name != name or name in {".", ".."}:
      return None
    root = _chat_root(data_dir, chat_id)
    inbox_fd = _open_directory(root / "inbox", create=False)
    stored_fd = _open_directory(root / "files", create=True)
    # The display name and MIME type live in the row. Keeping the private key
    # extensionless avoids treating an agent-controlled suffix as storage
    # metadata and remains valid even for long or unfamiliar file names.
    stored_name = uuid.uuid4().hex
    temporary_name = f".{stored_name}.tmp"
    source_fd = os.open(
      name,
      os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
      dir_fd=inbox_fd,
    )
    initial = os.fstat(source_fd)
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > MAX_RECORDED_BYTES:
      return None
    destination_fd = os.open(
      temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
      dir_fd=stored_fd,
    )
    with os.fdopen(source_fd, "rb", closefd=False) as src, os.fdopen(
      destination_fd, "wb", closefd=False,
    ) as dst:
      remaining = MAX_RECORDED_BYTES + 1
      digest = hashlib.sha256()
      while remaining > 0:
        chunk = src.read(min(1024 * 1024, remaining))
        if not chunk:
          break
        dst.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    final = os.fstat(source_fd)
    copied = os.fstat(destination_fd)
    if (
      (initial.st_mtime_ns, initial.st_size) != (final.st_mtime_ns, final.st_size)
      or copied.st_size > MAX_RECORDED_BYTES
      or copied.st_size != final.st_size
    ):
      return None
    os.close(destination_fd)
    destination_fd = None
    os.replace(
      temporary_name, stored_name,
      src_dir_fd=stored_fd, dst_dir_fd=stored_fd,
    )
    return {
      "name": name,
      "path": stored_name,
      "size": final.st_size,
      "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
      "sha256": digest.hexdigest(),
      "_source_identity": (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_size),
    }
  except OSError:
    log.debug("generated-file capture failed for %s", name, exc_info=True)
    return None
  finally:
    if source_fd is not None:
      os.close(source_fd)
    if destination_fd is not None:
      os.close(destination_fd)
    if temporary_name is not None and stored_fd is not None:
      try:
        os.unlink(temporary_name, dir_fd=stored_fd)
      except FileNotFoundError:
        pass
    if inbox_fd is not None:
      os.close(inbox_fd)
    if stored_fd is not None:
      os.close(stored_fd)


def _settle_capture(
  data_dir: str, chat_id: str, captured: dict, *, accepted: bool,
) -> bool:
  root = _chat_root(data_dir, chat_id)
  if not accepted:
    try:
      stored_fd = _open_directory(root / "files", create=False)
    except OSError:
      return False
    try:
      try:
        os.unlink(captured["path"], dir_fd=stored_fd)
      except FileNotFoundError:
        pass
    finally:
      os.close(stored_fd)
    return True

  try:
    inbox_fd = _open_directory(root / "inbox", create=False)
  except OSError:
    return False
  try:
    try:
      current = os.stat(
        captured["name"], dir_fd=inbox_fd, follow_symlinks=False,
      )
    except OSError:
      return True
    identity = (
      current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size,
    )
    if identity == captured["_source_identity"] and stat.S_ISREG(current.st_mode):
      try:
        os.unlink(captured["name"], dir_fd=inbox_fd)
      except FileNotFoundError:
        pass
    return True
  finally:
    os.close(inbox_fd)


def open_stored_file(
  data_dir: str, chat_id: str, stored_name: str,
) -> tuple[int, os.stat_result]:
  """Open one immutable file relative to a held, symlink-free store handle."""
  return _open_deliverable_file(data_dir, chat_id, "files", stored_name)


def open_inbox_file(
  data_dir: str, chat_id: str, name: str,
) -> tuple[int, os.stat_result]:
  """Open a live flat-inbox file without following provider-controlled links."""
  return _open_deliverable_file(data_dir, chat_id, "inbox", name)


def sha256_open_file(file_fd: int, opened: os.stat_result) -> str | None:
  """Hash a held regular file, rejecting oversized or changing content."""
  if opened.st_size > MAX_RECORDED_BYTES:
    return None
  digest = hashlib.sha256()
  try:
    os.lseek(file_fd, 0, os.SEEK_SET)
    remaining = MAX_RECORDED_BYTES + 1
    while remaining > 0:
      chunk = os.read(file_fd, min(1024 * 1024, remaining))
      if not chunk:
        break
      digest.update(chunk)
      remaining -= len(chunk)
    final = os.fstat(file_fd)
    if (
      remaining == 0
      or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
      != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
      or final.st_size != MAX_RECORDED_BYTES + 1 - remaining
    ):
      return None
    os.lseek(file_fd, 0, os.SEEK_SET)
    return digest.hexdigest()
  except OSError:
    return None


def viewed_inbox_sha256(data_dir: str, chat_id: str, path: str) -> str | None:
  """Fingerprint only a raster file in this chat's flat deliverables inbox."""
  if not isinstance(path, str):
    return None
  source = Path(path)
  if (
    source.parent != _chat_root(data_dir, chat_id) / "inbox"
    or source.name in {"", ".", ".."}
    or mimetypes.guess_type(source.name)[0] not in VIEWED_RASTER_MIME_TYPES
  ):
    return None
  try:
    file_fd, opened = open_inbox_file(data_dir, chat_id, source.name)
  except OSError:
    return None
  try:
    return sha256_open_file(file_fd, opened)
  finally:
    os.close(file_fd)


def _open_deliverable_file(
  data_dir: str, chat_id: str, collection: str, name: str,
) -> tuple[int, os.stat_result]:
  if Path(name).name != name or name in {".", ".."}:
    raise OSError("invalid generated-file path")
  directory_fd = _open_directory(
    _chat_root(data_dir, chat_id) / collection, create=False,
  )
  file_fd = None
  try:
    file_fd = os.open(
      name,
      os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
      dir_fd=directory_fd,
    )
    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode):
      raise OSError("generated-file path is not a regular file")
    return file_fd, info
  except Exception:
    if file_fd is not None:
      os.close(file_fd)
    raise
  finally:
    os.close(directory_fd)


async def publish_inbox_files(sink, *, data_dir: str, chat_id: str) -> dict[str, str]:
  """Freeze, persist, and consume a batch; return original->recorded names.

  Native provider artifacts use the receipt to distinguish actual delivery
  from capacity limits, rejected writes, or an uncertain acknowledgement.
  """
  published: dict[str, str] = {}
  capacity = await sink.generated_file_capacity()
  if capacity <= 0:
    return published
  names = (await asyncio.to_thread(_inbox_names, data_dir, chat_id))[:capacity]
  for name in names:
    captured = await asyncio.to_thread(_freeze_file, data_dir, chat_id, name)
    if captured is None:
      continue
    event = {
      "type": "generated_file",
      **{key: value for key, value in captured.items() if not key.startswith("_")},
      "previewable": previewable_mime_type(captured["mime_type"]),
      # Private in-process settlement evidence. ChatEventSink removes these
      # keys before reduction, persistence, or broadcast.
      "_capture_data_dir": data_dir,
      "_source_identity": captured["_source_identity"],
    }
    outcome = await sink.publish_generated_file(event)
    if outcome is PUBLICATION_UNCERTAIN:
      # Do not enqueue a later file behind an unresolved publication: its
      # snapshot cannot yet include the first file's collision-resolved name.
      return published
    await asyncio.to_thread(
      _settle_capture, data_dir, chat_id, captured, accepted=bool(outcome),
    )
    if isinstance(outcome, str) and outcome:
      published[name] = outcome
  return published
