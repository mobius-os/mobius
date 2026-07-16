"""Assemble the always-on recent-chat continuity block.

The platform owns only per-chat summaries under
``<data_dir>/shared/memory/chats/<id>/index.md``. A new session receives the
bounded Digest from the most recently touched notes, never their cumulative
Summary/facts and never knowledge-graph files. Optional installed apps may use
the sibling directory for richer data, but they activate and retrieve that data
through their own system-prompt contribution and reader.

``build_memory_block`` is pure; ``chat.py`` owns the surrounding private-context
envelope and observability event.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

# Budget for the always-injected recent-chat digest portion.
DEFAULT_BUDGET_BYTES = 25_000
DEFAULT_MAX_NOTES = 12
# How many recent per-chat notes to inject at session start. Each is the
# agent's one-paragraph Digest; the most-recently-modified ones open a fresh
# session with recent conversational context.
RECENT_CHAT_NOTES = 10
# Per-note byte cap on the injected chat digest. The daytime agent is
# instructed to keep each chat's summary bounded and high-level (the full
# detail lives in the transcript, read on demand), but this is a defensive
# cap so a note that grew past its intended size still injects a bounded
# head rather than crowding out the other recent chats or blowing the budget.
DIGEST_MAX_BYTES = 800

# Injected once per new session, outside the individual recent-chat entries.
# Keeping retrieval guidance here makes the structured entry contract and its
# single shared instruction one source of truth.
RECENT_CHAT_RETRIEVAL_INSTRUCTION = (
  "Each recent-chat entry gives a Name, Location, and bounded Digest. "
  "When more detail would materially help, read "
  "/data/shared/memory/<Location> for that chat's complete cumulative "
  "summary. The platform alone publishes those files; do not edit them."
)


@dataclass
class MemoryBlock:
  """Result of assembling the injected memory context.

  `text` is the bare context (no `<agent_experience>` envelope — the caller
  adds that plus the dynamic provider/timezone/viewport tail). `loaded` is the
  list of chat-note paths that made it into the block; `entries` is their
  owner-visible name/location/digest representation. `mode` is
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
  ``digest`` is the bounded cross-chat continuity paragraph. The unbounded
  ``## Summary`` remains owned by :func:`compaction.load_cumulative_summary`
  because it is also continuation-critical provider handoff state.

  Missing and legacy notes are normal: older notes predate ``## Digest`` and
  return ``None`` for that layer rather than duplicating their full Summary.
  """
  path = memory_dir(data_dir) / "chats" / chat_id / "index.md"
  text = _read(path)
  if not text.strip():
    return {"description": None, "digest": None}
  description = str(parse_frontmatter(text).get("description", "")).strip()
  digest = _extract_section(text, "Digest")
  return {
    "description": description or None,
    "digest": digest.strip() if digest and digest.strip() else None,
  }


def _read(path: Path) -> str:
  try:
    return path.read_text(encoding="utf-8")
  except OSError:
    return ""


def _truncate_bytes(text: str, limit: int) -> str:
  """Truncates on a UTF-8 byte budget without splitting a codepoint."""
  raw = text.encode("utf-8")
  if len(raw) <= limit:
    return text
  return raw[:limit].decode("utf-8", errors="ignore")


def build_memory_block(
  data_dir: str | Path,
  *,
  budget_bytes: int = DEFAULT_BUDGET_BYTES,
  max_notes: int = DEFAULT_MAX_NOTES,
  eligible_chat_ids: Collection[str] | None = None,
) -> MemoryBlock:
  """Assembles the injected memory context.

  Only recent-chat digests are injected, always and without a graph/app gate.
  Each entry is the note's one-line ``description``, relative path, and bounded
  ``## Digest`` paragraph. The cumulative ``## Summary``,
  facts, graph router, MOCs, and atomic notes are never pulled into a new chat.
  An installed system app may teach the agent to request graph recall through
  a separate prompt-scoped reader.

  Returns an empty block only when there are no usable chat notes. ``max_notes``
  can narrow the platform default but cannot expand it past
  ``RECENT_CHAT_NOTES``. Pure: never writes and never raises on missing/garbled
  files.
  """
  # Clamp at 0 so a stray negative budget can't reach the byte-slicing below,
  # where a negative limit returns a SUFFIX of the text instead of empty.
  budget_bytes = max(0, budget_bytes)
  root = memory_dir(data_dir)
  parts: list[str] = []
  loaded: list[str] = []
  entries: list[dict[str, str]] = []
  used = 0
  # Each note is independently capped. Continue past one that does not fit so
  # an unusually long newest note cannot hide every older short digest.
  note_limit = min(RECENT_CHAT_NOTES, max(0, max_notes))
  for note in _recent_chat_notes(
    root, note_limit, eligible_chat_ids=eligible_chat_ids,
  ):
    name, digest = _chat_digest_parts(note)
    if not name and not digest:
      continue
    rel = f"chats/{note.parent.name}/index.md"
    chunk = (
      "<recent_chat>\n"
      f"Name: {name or note.parent.name}\n"
      f"Location: {rel}\n"
      f"Digest: {digest}\n"
      "</recent_chat>"
    )
    if used + len(chunk.encode("utf-8")) + 2 > budget_bytes:
      continue
    parts.append(chunk)
    loaded.append(rel)
    entries.append({
      "name": name or note.parent.name,
      "location": rel,
      "digest": digest,
    })
    used += len(chunk.encode("utf-8")) + 2

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
) -> list[Path]:
  """The per-chat note files (`chats/<id>/index.md`), most-recently-modified
  first, capped at `limit`. Their bounded digests are injected at
  session start. Tolerates a missing `chats/` dir (returns [])."""
  chats = root / "chats"
  if not chats.is_dir():
    return []
  eligible = set(eligible_chat_ids) if eligible_chat_ids is not None else None
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


def _extract_section(text: str, heading: str) -> str | None:
  """Body under a level-2 `## <heading>` section, up to the next level-2
  heading or EOF. Case-insensitive on the heading; None when it is absent."""
  lines = _strip_frontmatter(text).splitlines()
  target = heading.strip().lower()
  start = None
  for i, line in enumerate(lines):
    s = line.strip()
    if s.startswith("## ") and s[3:].strip().lower() == target:
      start = i + 1
      break
  if start is None:
    return None
  collected: list[str] = []
  for line in lines[start:]:
    if line.strip().startswith("## "):
      break
    collected.append(line)
  return "\n".join(collected).strip()


def _chat_digest_parts(note: Path) -> tuple[str, str]:
  """Return a chat's one-line name and bounded cross-chat paragraph.

  New notes carry an explicit ``## Digest``. Legacy notes fall back to their
  ``## Summary`` (not the whole body, so facts never enter automatic startup
  context); a heading-less legacy note falls back to its loose body.
  """
  text = _read(note)
  if not text.strip():
    return "", ""
  desc = str(parse_frontmatter(text).get("description", "")).strip()
  digest = _extract_section(text, "Digest")
  if digest is None:
    digest = _extract_section(text, "Summary")
  if digest is None:
    digest = _strip_frontmatter(text).strip()
  return desc, _truncate_bytes(digest.strip(), DIGEST_MAX_BYTES).strip()
