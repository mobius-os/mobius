"""Durable agent-authored chat continuity and its Markdown projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm import load_only

from app import models
from app.chat_notes import extract_cumulative_summary, extract_section
from app.memory import parse_frontmatter


CONTINUITY_VERSION = 2
DEFAULT_ENTRY_LIMIT = 5
MAX_ENTRY_LIMIT = 200


def _canonical_messages(messages: list[dict]) -> bytes:
  return json.dumps(
    messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")


def completed_prefix(messages: list[dict], run_id: str) -> tuple[int, str | None]:
  """Return a whole-prefix proof ending at the prior completed assistant turn.

  Every row after the last non-current assistant stays uncovered. That excludes
  the current turn's initial user row, mutable assistant rows, and any accepted
  steers the model may not yet have consumed.
  """
  boundary = 0
  for index, message in enumerate(messages or []):
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
      # Id-less assistant rows are legacy/ambiguous. They cannot prove a
      # completed boundary while another run is active.
      continue
    current = (
      message_id == run_id or message_id.startswith(f"{run_id}:assistant:")
    )
    if not current:
      boundary = index + 1
  prefix = list(messages[:boundary])
  if not prefix:
    return 0, None
  return boundary, hashlib.sha256(_canonical_messages(prefix)).hexdigest()


def verified_uncovered_messages(
  messages: list[dict], *, covered_count: int, covered_prefix_hash: str | None,
) -> tuple[list[dict], bool]:
  """Return the uncovered suffix, or the complete transcript on proof failure."""
  if covered_count == 0 and covered_prefix_hash is None:
    return list(messages or []), True
  if covered_count < 0 or covered_count > len(messages or []):
    return list(messages or []), False
  prefix = list((messages or [])[:covered_count])
  actual = hashlib.sha256(_canonical_messages(prefix)).hexdigest() if prefix else None
  if actual != covered_prefix_hash:
    return list(messages or []), False
  return list((messages or [])[covered_count:]), True


def legacy_note_path(data_dir: str | Path, chat_id: str) -> Path:
  return Path(data_dir) / "shared" / "memory" / "chats" / chat_id / "index.md"


def read_legacy_note(data_dir: str | Path, chat_id: str) -> str | None:
  try:
    value = legacy_note_path(data_dir, chat_id).read_text(encoding="utf-8")
  except OSError:
    return None
  return value if value.strip() else None


def legacy_parts(text: str) -> tuple[str | None, str | None, str | None]:
  """Return description, short state and full history from a preserved note."""
  metadata = parse_frontmatter(text)
  description = str(metadata.get("description", "")).strip() or None
  digest = extract_section(
    text, "Summary" if metadata.get("continuity_version") == 2 else "Digest",
  )
  summary = extract_cumulative_summary(text)
  if summary is None:
    summary = extract_section(text, "Summary")
  return description, digest, summary


def continuity_wire(
  db: Session,
  chat: models.Chat,
  *,
  after_revision: int | None = None,
  limit: int = DEFAULT_ENTRY_LIMIT,
  full: bool = False,
  data_dir: str | Path | None = None,
) -> dict[str, Any]:
  state = db.get(models.ChatContinuity, chat.id)
  if state is None:
    legacy = read_legacy_note(data_dir, chat.id) if data_dir is not None else None
    description, digest, _summary = legacy_parts(legacy) if legacy else (None, None, None)
    short_summary = digest or description
    entries = []
    if legacy:
      entry = {
        "revision": 0,
        "checkpoint_id": "legacy-unmigrated",
        "run_id": None,
        "digest": (digest or "Historical continuity baseline.").strip(),
        "summary": None,
        "created_at": None,
        "coverage": {"message_count": 0, "prefix_hash": None},
        "legacy_baseline": True,
      }
      if full:
        entry["legacy_markdown"] = legacy
      entries.append(entry)
    return {
      "continuity_version": CONTINUITY_VERSION,
      "chat_id": chat.id,
      "revision": 0,
      "title": chat.title,
      "title_locked": bool(chat.title_locked),
      "summary": short_summary,
      "updated_at": None,
      "coverage": {"message_count": 0, "prefix_hash": None},
      "entries": entries,
      "next_after_revision": 0,
      "has_more": False,
      "legacy_unmigrated": bool(legacy),
    }

  query = db.query(models.ChatContinuityEntry).filter(
    models.ChatContinuityEntry.chat_id == chat.id,
  )
  if not full:
    query = query.options(load_only(
      models.ChatContinuityEntry.chat_id,
      models.ChatContinuityEntry.revision,
      models.ChatContinuityEntry.checkpoint_id,
      models.ChatContinuityEntry.run_id,
      models.ChatContinuityEntry.digest,
      models.ChatContinuityEntry.covered_message_count,
      models.ChatContinuityEntry.covered_prefix_hash,
      models.ChatContinuityEntry.created_at,
    ))
  has_more = False
  has_older = False
  if full:
    rows = query.order_by(models.ChatContinuityEntry.revision.asc()).all()
  elif after_revision is None:
    rows = query.order_by(
      models.ChatContinuityEntry.revision.desc()
    ).limit(limit + 1).all()
    has_older = len(rows) > limit
    rows = list(reversed(rows[:limit]))
  else:
    rows = query.filter(
      models.ChatContinuityEntry.revision > after_revision
    ).order_by(models.ChatContinuityEntry.revision.asc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
  entries = []
  for row in rows:
    item = {
      "revision": row.revision,
      "checkpoint_id": row.checkpoint_id,
      "run_id": row.run_id,
      "digest": row.digest,
      "created_at": row.created_at.isoformat() if row.created_at else None,
      "coverage": {
        "message_count": row.covered_message_count,
        "prefix_hash": row.covered_prefix_hash,
      },
      "legacy_baseline": row.checkpoint_id == "legacy-baseline-v1",
    }
    if full and row.legacy_markdown is not None:
      item["legacy_markdown"] = row.legacy_markdown
    if full:
      item["summary"] = row.current_summary
    entries.append(item)
  cursor = entries[-1]["revision"] if entries else (after_revision or 0)
  return {
    "continuity_version": CONTINUITY_VERSION,
    "chat_id": chat.id,
    "revision": state.revision,
    "title": chat.title,
    "title_locked": bool(chat.title_locked),
    "summary": state.current_summary,
    "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    "coverage": {
      "message_count": state.covered_message_count,
      "prefix_hash": state.covered_prefix_hash,
    },
    "entries": entries,
    "next_after_revision": cursor,
    "has_more": has_more,
    "has_older": has_older,
    "legacy_unmigrated": False,
  }


def _fence_for(text: str) -> str:
  longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
  return "`" * max(4, longest + 4)


def render_projection(db: Session, chat_id: str) -> str | None:
  chat = db.get(models.Chat, chat_id)
  state = db.get(models.ChatContinuity, chat_id)
  if chat is None or state is None:
    return None
  rows = db.query(models.ChatContinuityEntry).filter(
    models.ChatContinuityEntry.chat_id == chat_id,
  ).order_by(models.ChatContinuityEntry.revision.asc()).all()
  lines = [
    "---",
    f"description: {json.dumps(chat.title, ensure_ascii=False)}",
    f"continuity_version: {CONTINUITY_VERSION}",
    f"revision: {state.revision}",
    "---",
    "",
    "## Summary",
    "",
    (state.current_summary or "").strip(),
    "",
    "## Digest",
    "",
  ]
  for row in rows:
    stamp = row.created_at.isoformat() if row.created_at else "unknown time"
    lines.extend([
      f"### Revision {row.revision} · {stamp}",
      "",
      row.digest.strip(),
      "",
    ])
  legacy_rows = [row for row in rows if row.legacy_markdown is not None]
  if legacy_rows:
    lines.extend(["## Historical baseline (legacy raw)", ""])
    for row in legacy_rows:
      raw = row.legacy_markdown or ""
      fence = _fence_for(raw)
      lines.extend([f"{fence}markdown", raw, fence, ""])
  return "\n".join(lines).rstrip() + "\n"


def project_continuity(data_dir: str | Path, db: Session, chat_id: str) -> bool:
  """Atomically replace the recoverable Markdown projection from DB state."""
  rendered = render_projection(db, chat_id)
  if rendered is None:
    return False
  path = legacy_note_path(data_dir, chat_id)
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp_name = tempfile.mkstemp(prefix=".index.", suffix=".tmp", dir=path.parent)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(rendered)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp_name, path)
  except BaseException:
    try:
      os.unlink(tmp_name)
    except OSError:
      pass
    raise
  return True


def now_naive() -> datetime:
  return datetime.now(UTC).replace(tzinfo=None)
