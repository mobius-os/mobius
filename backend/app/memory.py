"""Assemble the always-on recent-chat continuity block.

The platform owns only per-chat summaries under
``<data_dir>/shared/memory/chats/<id>/index.md``. A new session receives the
short Summary of the most recently active chats, never their cumulative Digest,
facts, or knowledge-graph files. Optional installed apps may use the sibling
directory for richer data, but they activate and retrieve that data
through their own system-prompt contribution and reader.

``build_memory_block`` is pure; ``chat.py`` owns the surrounding private-context
envelope and observability event.
"""

from __future__ import annotations

import html
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from app.chat_notes import extract_chat_summary

# How many recent per-chat notes to inject at session start. Each contributes
# the agent's whole short Summary, with no size cap or budget; the most
# recently active ones open a fresh session with recent conversational context.
RECENT_CHAT_NOTES = 10

# Injected once per new session, outside the individual recent-chat entries.
# Keeping retrieval guidance here makes the structured entry contract and its
# single shared instruction one source of truth.
RECENT_CHAT_RETRIEVAL_INSTRUCTION = (
  "Each recent-chat entry gives a Name, Location, and Summary. "
  "When more detail would materially help, read "
  "/data/shared/memory/<Location> for that chat's complete cumulative "
  "digest. The platform alone publishes those files; do not edit them."
)


@dataclass
class MemoryBlock:
  """Result of assembling the injected memory context.

  `text` is the bare context (no `<agent_experience>` envelope — the caller
  adds that plus the dynamic provider/timezone/viewport tail). `loaded` is the
  list of chat-note paths that made it into the block; `entries` is their
  owner-visible name/location/summary representation. `mode` is
  "recent_chats" | "empty" for observability. Knowledge-graph material is
  deliberately never assembled here; installed apps recall it explicitly.
  """

  text: str
  loaded: list[str] = field(default_factory=list)
  entries: list[dict[str, str]] = field(default_factory=list)
  mode: str = "empty"


def memory_dir(data_dir: str | Path) -> Path:
  return Path(data_dir) / "shared" / "memory"


def parse_frontmatter(text: str) -> dict[str, object]:
  """Minimal YAML-frontmatter reader for the handful of scalar/list fields a
  note carries (`importance`, `access_count`, `title`, `tags`, `mocs`).

  Deliberately dependency-free and forgiving: a malformed header yields an
  empty dict rather than raising, because a single bad note must never break
  the whole memory-injection path. Supports `key: scalar` and
  `key: [a, b, c]` one-line lists; nested structures are ignored.
  """
  if not text.startswith("---"):
    return {}
  end = text.find("\n---", 3)
  if end == -1:
    return {}
  body = text[3:end].strip("\n")
  out: dict[str, object] = {}
  for line in body.splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
      continue
    if ":" not in line:
      continue
    key, _, raw = line.partition(":")
    key = key.strip()
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
      items = [x.strip().strip("'\"") for x in raw[1:-1].split(",")]
      out[key] = [x for x in items if x]
    elif raw.lstrip("-").isdigit():
      out[key] = int(raw)
    else:
      out[key] = raw.strip("'\"")
  return out


def load_chat_summary_metadata(
  data_dir: str | Path, chat_id: str,
) -> dict[str, str | None]:
  """Read the short, owner-visible layers of a published chat note.

  ``description`` is the one-line gist that normally becomes the chat name;
  ``summary`` is the short cross-chat continuity paragraph. The cumulative
  ``## Digest`` is owned by :func:`compaction.load_full_digest` because it is
  also continuation-critical provider handoff state.

  Missing and legacy notes are normal: older notes predate the short layer and
  return ``None`` for it rather than duplicating their full Digest.
  """
  path = memory_dir(data_dir) / "chats" / chat_id / "index.md"
  text = _read(path)
  if not text.strip():
    return {"description": None, "summary": None}
  description = str(parse_frontmatter(text).get("description", "")).strip()
  summary = _chat_summary(text)
  return {"description": description or None, "summary": summary or None}


def _read(path: Path) -> str:
  try:
    return path.read_text(encoding="utf-8")
  except OSError:
    return ""


def build_memory_block(
  data_dir: str | Path,
  *,
  eligible_chat_ids: Collection[str] | None = None,
  ordered_chat_ids: Collection[str] | None = None,
) -> MemoryBlock:
  """Assembles the injected memory context.

  Only recent-chat summaries are injected, always and without a graph/app
  gate. Each entry is the note's one-line ``description``, relative path, and
  whole ``## Summary``. The full ``## Digest``, facts, graph router, MOCs, and
  atomic notes are never pulled into a new chat. An installed app may teach
  the agent to request graph recall through a separate prompt-scoped reader.

  Returns an empty block only when there are no usable chat notes. Pure: never
  writes and never raises on missing/garbled files.
  """
  root = memory_dir(data_dir)
  parts: list[str] = []
  loaded: list[str] = []
  entries: list[dict[str, str]] = []
  for note in _recent_chat_notes(
    root, RECENT_CHAT_NOTES,
    eligible_chat_ids=eligible_chat_ids,
    ordered_chat_ids=ordered_chat_ids,
  ):
    text = _read(note)
    name = str(parse_frontmatter(text).get("description", "")).strip()
    summary = _chat_summary(text)
    if not name and not summary:
      continue
    rel = f"chats/{note.parent.name}/index.md"
    safe_name = html.escape(name or note.parent.name, quote=False)
    safe_summary = html.escape(summary, quote=False)
    parts.append(
      "<recent_chat>\n"
      f"Name: {safe_name}\n"
      f"Location: {rel}\n"
      f"Summary: {safe_summary}\n"
      "</recent_chat>"
    )
    loaded.append(rel)
    entries.append({
      "name": name or note.parent.name,
      "location": rel,
      "summary": summary,
    })

  if not parts:
    return MemoryBlock(text="", loaded=[], entries=[], mode="empty")
  return MemoryBlock(
    text="\n\n".join(parts), loaded=loaded, entries=entries,
    mode="recent_chats",
  )


def _recent_chat_notes(
  root: Path,
  limit: int,
  *,
  eligible_chat_ids: Collection[str] | None = None,
  ordered_chat_ids: Collection[str] | None = None,
) -> list[Path]:
  """Return bounded chat notes in explicit activity order when supplied.

  The mtime fallback serves legacy callers that only provide eligibility.
  Tolerates a missing ``chats/`` directory.
  """
  chats = root / "chats"
  if not chats.is_dir():
    return []
  eligible = set(eligible_chat_ids) if eligible_chat_ids is not None else None
  if ordered_chat_ids is not None:
    ordered: list[Path] = []
    for chat_id in ordered_chat_ids:
      if eligible is not None and chat_id not in eligible:
        continue
      path = chats / chat_id / "index.md"
      try:
        if path.is_file():
          ordered.append(path)
      except OSError:
        continue
      if len(ordered) >= limit:
        break
    return ordered
  candidates: list[tuple[float, Path]] = []
  try:
    paths = chats.glob("*/index.md")
    for path in paths:
      if eligible is not None and path.parent.name not in eligible:
        continue
      try:
        if path.is_file():
          candidates.append((path.stat().st_mtime, path))
      except OSError:
        # A hard-purge may remove a note between glob and stat.  It was already
        # ineligible for durable continuity, so treat the race as a miss.
        continue
  except OSError:
    return []
  candidates.sort(key=lambda item: item[0], reverse=True)
  return [path for _mtime, path in candidates[:limit]]


def _strip_frontmatter(text: str) -> str:
  """The note body after any leading `---` frontmatter block (the whole text
  when there is no closing fence)."""
  if not text.startswith("---"):
    return text
  end = text.find("\n---", 3)
  if end == -1:
    return text
  rest = text[end + 1:]
  nl = rest.find("\n")
  return rest[nl + 1:] if nl != -1 else ""


def _chat_summary(text: str) -> str:
  """Return a note's whole short chat summary, or ``""`` when it has none."""
  return (extract_chat_summary(_strip_frontmatter(text)) or "").strip()
