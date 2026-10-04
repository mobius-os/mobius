"""Chat-owned snapshots of the images an agent views.

A viewed path names a mutable file (often under the global ``/tmp``), so it
cannot prove which bytes the provider saw. The image tool therefore reads the
file once, stores those bytes under the viewing chat's media as an immutable
content-addressed snapshot, and hands the provider that same payload. The
transcript then binds the view to the snapshot whose digest matches the
payload the provider received, and the owner's preview serves exactly it.

Standard library only: the control MCP server loads this file directly rather
than importing the backend application.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Any

MAX_VIEWED_IMAGE_BYTES = 20 * 1024 * 1024
_SIGNATURES = (
  (b"\x89PNG\r\n\x1a\n", "image/png"),
  (b"\xff\xd8\xff", "image/jpeg"),
  (b"GIF87a", "image/gif"),
  (b"GIF89a", "image/gif"),
)
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
SNAPSHOT_NAME = re.compile(r"^viewed-[0-9a-f]{64}\.(?:png|jpg|gif|webp)$")


def image_type(data: bytes) -> str | None:
  """The raster type the bytes really are, never the type the name claims."""
  for signature, mime in _SIGNATURES:
    if data.startswith(signature):
      return mime
  if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
    return "image/webp"
  return None


def snapshot_name(data: bytes, mime: str) -> str:
  return f"viewed-{hashlib.sha256(data).hexdigest()}.{_EXTENSIONS[mime]}"


def chat_media_dir(data_dir: str | os.PathLike, chat_id: str) -> Path:
  return Path(data_dir) / "chats" / chat_id / "media"


def read_viewed_image(path: str) -> tuple[bytes, str]:
  """Read one raster image in a single bounded read, or raise ValueError."""
  if not isinstance(path, str) or not os.path.isabs(path):
    raise ValueError("path must be an absolute file path")
  try:
    with open(path, "rb") as handle:
      data = handle.read(MAX_VIEWED_IMAGE_BYTES + 1)
  except OSError as exc:
    raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from exc
  if len(data) > MAX_VIEWED_IMAGE_BYTES:
    raise ValueError("image is larger than 20 MB")
  mime = image_type(data)
  if mime is None:
    raise ValueError(f"{path} is not a PNG, JPEG, GIF, or WebP image")
  return data, mime


def store_snapshot(media_dir: Path, data: bytes, mime: str) -> str:
  """Publish the bytes once under their digest; an existing name is the same image.

  The temporary file is linked into place, so a reader never observes a
  partial snapshot and no writer can replace a published one.
  """
  name = snapshot_name(data, mime)
  media_dir.mkdir(parents=True, exist_ok=True)
  target = media_dir / name
  if target.is_file():
    return name
  temporary = media_dir / f".{name}.{os.getpid()}.tmp"
  try:
    with open(temporary, "xb") as handle:
      handle.write(data)
    try:
      os.link(temporary, target)
    except FileExistsError:
      pass
  finally:
    temporary.unlink(missing_ok=True)
  return name


def bound_snapshot(
  data_dir: str | os.PathLike, chat_id: str, result: Any,
) -> str:
  """Name the chat snapshot holding exactly the image the provider received.

  ``result`` is the tool's MCP result. The name is derived from the payload
  itself, so only a stored snapshot of those very bytes can be bound; anything
  else yields ``""`` and the view has no preview.
  """
  content = result.get("content") if isinstance(result, dict) else None
  images = [
    block for block in content or ()
    if isinstance(block, dict) and block.get("type") == "image"
  ]
  if len(images) != 1:
    return ""
  try:
    data = base64.b64decode(images[0].get("data") or "", validate=True)
  except (ValueError, TypeError):
    return ""
  mime = image_type(data)
  if mime is None or mime != images[0].get("mimeType"):
    return ""
  name = snapshot_name(data, mime)
  if (chat_media_dir(data_dir, chat_id) / name).is_file():
    return name
  return ""
