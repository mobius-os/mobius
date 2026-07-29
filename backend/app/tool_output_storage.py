"""Compact, self-describing storage for full tool outputs.

Tool output is text at every product boundary, but large results live in the
``tool_outputs`` side table for lazy expansion.  Keep the database column as
portable TEXT (SQLite and PostgreSQL) while compressing its payload behind a
versioned frame. Legacy plain-text rows remain readable while a bounded
background fix-forward migrates them without a schema change.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import zlib

from sqlalchemy import Text, text as sql_text
from sqlalchemy.types import TypeDecorator


TOOL_OUTPUT_STORAGE_PREFIX = "MOBIUS_TOOL_OUTPUT_ZLIB_V1:"
_COMPRESSION_LEVEL = 3


class ToolOutputDecodeError(ValueError):
  """The stored frame is malformed or no longer round-trips exactly."""


class CompressedToolOutputText(TypeDecorator):
  """SQLAlchemy TEXT type that compresses only at the database boundary."""

  impl = Text
  cache_ok = True

  def process_bind_param(self, value, _dialect):
    return encode_tool_output(value)

  def process_result_value(self, value, _dialect):
    return decode_tool_output(value)


def encode_tool_output(output: str | None) -> str:
  """Return one versioned compressed TEXT value for ``output``."""
  text = output or ""
  if not text:
    return ""
  raw = text.encode("utf-8")
  compressed = zlib.compress(raw, level=_COMPRESSION_LEVEL)
  payload = base64.b64encode(compressed).decode("ascii")
  return f"{TOOL_OUTPUT_STORAGE_PREFIX}{len(text)}:{payload}"


def is_compressed_tool_output(stored: object) -> bool:
  return isinstance(stored, str) and stored.startswith(
    TOOL_OUTPUT_STORAGE_PREFIX
  )


def _parse_frame(stored: str) -> tuple[int, bytes]:
  framed = stored[len(TOOL_OUTPUT_STORAGE_PREFIX):]
  length_text, separator, payload = framed.partition(":")
  if not separator or not length_text.isdigit():
    raise ToolOutputDecodeError("invalid tool-output compression frame")
  try:
    compressed = base64.b64decode(payload, validate=True)
  except (binascii.Error, ValueError) as exc:
    raise ToolOutputDecodeError(
      "invalid tool-output compression payload"
    ) from exc
  return int(length_text), compressed


def tool_output_length(stored: str | None) -> int:
  """Return the original character length without inflating a valid frame."""
  value = stored or ""
  if not is_compressed_tool_output(value):
    return len(value)
  length, _compressed = _parse_frame(value)
  return length


def decode_tool_output(
  stored: str | None,
  *,
  max_chars: int | None = None,
) -> str:
  """Decode exact text, optionally inflating only a bounded character prefix."""
  value = stored or ""
  if not is_compressed_tool_output(value):
    return value if max_chars is None else value[:max_chars]

  expected_chars, compressed = _parse_frame(value)
  try:
    if max_chars is None:
      raw = zlib.decompress(compressed)
    else:
      if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
      # A Unicode code point occupies at most four UTF-8 bytes.  The small
      # guard keeps a boundary code point whole while still bounding inflated
      # memory by the requested preview rather than the full tool result.
      raw = zlib.decompressobj().decompress(
        compressed,
        max_chars * 4 + 4,
      )
  except zlib.error as exc:
    raise ToolOutputDecodeError(
      "invalid tool-output compressed stream"
    ) from exc

  try:
    if max_chars is None:
      text = raw.decode("utf-8")
    else:
      # ``max_length`` may stop in the middle of a multibyte code point.
      # Preview reads need only the complete prefix; an incremental decoder
      # keeps that trailing fragment buffered rather than rejecting valid text.
      text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)
  except UnicodeDecodeError as exc:
    raise ToolOutputDecodeError(
      "invalid tool-output UTF-8 payload"
    ) from exc

  if max_chars is not None:
    return text[:max_chars]
  if len(text) != expected_chars:
    raise ToolOutputDecodeError(
      "tool-output compression length mismatch"
    )
  return text


def compress_legacy_tool_output_batch(
  session_factory,
  *,
  batch_size: int = 16,
  after_chat_id: str | None = None,
  after_tool_use_id: str | None = None,
) -> dict[str, object]:
  """Compress one old plain-text batch with an optimistic exact-value CAS.

  New ORM writes already cross ``CompressedToolOutputText``. The comparison in
  this one-time fix-forward prevents a concurrently refreshed tool result from
  being overwritten by the older value selected for compression.
  """
  limit = max(1, min(int(batch_size), 128))
  cursor_sql = ""
  params: dict[str, object] = {
    "prefix": TOOL_OUTPUT_STORAGE_PREFIX,
    "prefix_length": len(TOOL_OUTPUT_STORAGE_PREFIX),
    "limit": limit,
  }
  if after_chat_id is not None and after_tool_use_id is not None:
    cursor_sql = (
      "AND (chat_id > :after_chat_id OR "
      "(chat_id = :after_chat_id AND tool_use_id > :after_tool_use_id)) "
    )
    params["after_chat_id"] = after_chat_id
    params["after_tool_use_id"] = after_tool_use_id

  with session_factory() as db:
    rows = db.execute(sql_text(
      "SELECT chat_id, tool_use_id, output FROM tool_outputs "
      "WHERE output <> '' "
      "AND substr(output, 1, :prefix_length) <> :prefix "
      f"{cursor_sql}"
      "ORDER BY chat_id ASC, tool_use_id ASC LIMIT :limit"
    ), params).mappings().all()
    if not rows:
      return {
        "scanned": 0,
        "compressed": 0,
        "raw_chars": 0,
        "stored_chars": 0,
        "last_chat_id": after_chat_id,
        "last_tool_use_id": after_tool_use_id,
      }

    compressed_count = 0
    raw_chars = 0
    stored_chars = 0
    for row in rows:
      original = row["output"] or ""
      stored = encode_tool_output(original)
      result = db.execute(sql_text(
        "UPDATE tool_outputs SET output = :stored "
        "WHERE chat_id = :chat_id AND tool_use_id = :tool_use_id "
        "AND output = :original"
      ), {
        "stored": stored,
        "chat_id": row["chat_id"],
        "tool_use_id": row["tool_use_id"],
        "original": original,
      })
      if result.rowcount:
        compressed_count += 1
        raw_chars += len(original)
        stored_chars += len(stored)
    db.commit()
    return {
      "scanned": len(rows),
      "compressed": compressed_count,
      "raw_chars": raw_chars,
      "stored_chars": stored_chars,
      "last_chat_id": rows[-1]["chat_id"],
      "last_tool_use_id": rows[-1]["tool_use_id"],
    }
