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

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mobius.memory")

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


@dataclass
class MemoryBlock:
  """Result of assembling the injected memory context.

  `text` is the bare context (no `<agent_experience>` envelope — the caller
  adds that plus the dynamic provider/timezone/viewport tail). `loaded` is the
  list of chat-note paths that made it into the block. `mode` is
  "recent_chats" | "empty" for observability. Knowledge-graph material is
  deliberately never assembled here; installed apps recall it explicitly.
  """

  text: str
  loaded: list[str] = field(default_factory=list)
  mode: str = "empty"


def memory_dir(data_dir: str | Path) -> Path:
  return Path(data_dir) / "shared" / "memory"


def is_graph_ready(data_dir: str | Path) -> bool:
  """Graph mode is active iff the atomic `.ready` sentinel is present."""
  return (memory_dir(data_dir) / ".ready").is_file()


# ─── Usage tracking (the "access_count" / Memory "Used" signal) ───────────
#
# access_count is "how often a note was loaded" — a usage signal for the Memory
# app's "Used" column. v2 retrieval no longer RANKS by it (injection is
# router->traverse); it survives only as viewer/analytics signal. We track
# it in a sidecar counter (`usage.json`) rather than rewriting note
# frontmatter on the hot path: a counter bump is cheap and churns no git
# history. usage.json is the SELECTION signal: a node accrues a count when
# retrieval RETURNS it as relevant — the memory-search subagent's cited
# SOURCES (`record_usage_ids`) — NOT when it is merely injected into the prompt
# for free or traversed during a search. `load_usage` feeds the graph builder,
# so the effective access_count = frontmatter baseline + live usage; the Memory
# viewer's "Used" column reads it. Keyed by node id (a note's slug), matching
# graph.json.
def _usage_path(data_dir: str | Path) -> Path:
  return memory_dir(data_dir) / "usage.json"


def _loaded_path_to_id(rel: str) -> str | None:
  """Maps a `loaded` entry (e.g. 'notes/foo.md', 'index.md') to its graph
  node id. inbox.md, recent-chats.md, and anything unrecognised return None
  (rolling buffers aren't graph nodes — counting them would invent phantom
  ids in usage.json and the read-trace)."""
  import os
  # Per-chat note: chats/<id>/index.md is keyed by the chat, not the basename
  # (which is "index.md" and would collide with the root router index).
  if rel.startswith("chats/") and rel.endswith("/index.md"):
    return "chat:" + rel.split("/")[1]
  name = os.path.basename(rel)
  if name in ("inbox.md", "recent-chats.md"):
    return None
  if name == "index.md":
    return "index"
  if name.endswith(".md"):
    return name[:-3]
  return None


def _read_usage_file(path: Path) -> dict[str, int]:
  """Reads a usage.json by absolute path, tolerating absence/corruption."""
  import json
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return {}
  if not isinstance(data, dict):
    return {}
  return {k: v for k, v in data.items() if isinstance(v, int)}


def load_usage(data_dir: str | Path) -> dict[str, int]:
  """Reads the usage counter for an instance, tolerating absence (→ {})."""
  return _read_usage_file(_usage_path(data_dir))


def _bump_usage(data_dir: str | Path, ids: list[str]) -> None:
  """Increments the usage counter for each graph node id. Best-effort and
  side-effecting. Atomic temp-write + rename so a concurrent chat start can't
  read a half-written counter."""
  import json
  import os
  import tempfile
  ids = [i for i in ids if i]
  if not ids:
    return
  counts = load_usage(data_dir)
  for nid in ids:
    counts[nid] = counts.get(nid, 0) + 1
  path = _usage_path(data_dir)
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a UNIQUE temp file (not a shared "usage.json.tmp"): two
    # concurrent chat starts would otherwise both write the same temp path and
    # one could os.replace a file the other is still writing — corrupting it.
    # mkstemp gives each writer its own temp; os.replace is atomic. (Increments
    # can still race across processes, but Mobius runs a single worker where
    # this sync write is atomic, and the counter is best-effort regardless.)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-", suffix=".tmp")
    try:
      with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(counts, fh)
      os.replace(tmp, path)
    except BaseException:
      try:
        os.unlink(tmp)
      except OSError:
        pass
      raise
  except OSError as exc:
    log.warning("memory.record_usage: could not persist usage.json: %r", exc)


def record_usage(data_dir: str | Path, loaded: list[str]) -> None:
  """Increments the usage counter for every loaded note PATH's id. Best-effort
  and side-effecting; never called from `build_memory_block` (which stays
  pure). Path variant of `record_usage_ids` for callers that hold graph-
  relative paths rather than node ids."""
  _bump_usage(data_dir, [_loaded_path_to_id(p) for p in loaded])


def record_usage_ids(data_dir: str | Path, ids: list[str]) -> None:
  """Increments the usage counter for graph node ids directly — the SELECTION
  signal. A node accrues access_count when retrieval RETURNS it as relevant
  (the memory-search subagent's cited SOURCES), not when it is merely injected
  into the prompt for free or traversed during a search."""
  _bump_usage(data_dir, ids)


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
) -> MemoryBlock:
  """Assembles the injected memory context.

  Only recent-chat digests are injected, always and without a graph/app gate.
  Each chunk is the note's one-line ``description`` plus bounded ``## Digest``
  paragraph, fenced with the full-note path. The cumulative ``## Summary``,
  facts, graph router, MOCs, and atomic notes are never pulled into a new chat.
  An installed system app may teach the agent to request graph recall through
  a separate prompt-scoped reader.

  Returns an empty block only when there are no usable chat notes. `max_notes`
  is retained for signature stability; the recent-chat cap is
  `RECENT_CHAT_NOTES`. Pure: never writes and never raises on missing/garbled
  files.
  """
  # Clamp at 0 so a stray negative budget can't reach the byte-slicing below,
  # where a negative limit returns a SUFFIX of the text instead of empty.
  budget_bytes = max(0, budget_bytes)
  root = memory_dir(data_dir)
  parts: list[str] = []
  loaded: list[str] = []
  used = 0
  # Each note is independently capped. Continue past one that does not fit so
  # an unusually long newest note cannot hide every older short digest.
  for note in _recent_chat_notes(root, RECENT_CHAT_NOTES):
    digest = _chat_digest(note)
    if not digest:
      continue
    rel = f"chats/{note.parent.name}/index.md"
    chunk = f"<<< {rel} (recent chat — Read this file for the full note) >>>\n{digest}"
    if used + len(chunk.encode("utf-8")) + 2 > budget_bytes:
      continue
    parts.append(chunk)
    loaded.append(rel)
    used += len(chunk.encode("utf-8")) + 2

  if not parts:
    return MemoryBlock(text="", loaded=[], mode="empty")
  return MemoryBlock(
    text="\n\n".join(parts), loaded=loaded, mode="recent_chats"
  )


def _recent_chat_notes(root: Path, limit: int) -> list[Path]:
  """The per-chat note files (`chats/<id>/index.md`), most-recently-modified
  first, capped at `limit`. Their bounded digests are injected at
  session start. Tolerates a missing `chats/` dir (returns [])."""
  chats = root / "chats"
  if not chats.is_dir():
    return []
  notes = [p for p in chats.glob("*/index.md") if p.is_file()]
  notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
  return notes[:limit]


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


def _chat_digest(note: Path) -> str:
  """Return one bounded cross-chat paragraph plus its one-line gist.

  New notes carry an explicit ``## Digest``. Legacy notes fall back to their
  ``## Summary`` (not the whole body, so facts never enter automatic startup
  context); a heading-less legacy note falls back to its loose body.
  """
  text = _read(note)
  if not text.strip():
    return ""
  desc = str(parse_frontmatter(text).get("description", "")).strip()
  digest = _extract_section(text, "Digest")
  if digest is None:
    digest = _extract_section(text, "Summary")
  if digest is None:
    digest = _strip_frontmatter(text).strip()
  combined = "\n".join(p for p in (desc, digest.strip()) if p).strip()
  return _truncate_bytes(combined, DIGEST_MAX_BYTES).strip()
