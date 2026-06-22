"""Assembles the agent's injected memory block from the knowledge graph.

Möbius gives the agent its long-term memory by prepending a block to the
FIRST user message of a session (see `chat.py`). This module builds the block:
the router index (the root "Home" MOC) + the full summaries of the ~10
most-recently-modified per-chat notes. The deeper graph (mocs/, notes/) is NOT
injected — it is read on demand by the memory-search subagent (and by the agent
following `[[wikilinks]]`).

Layout under `<data_dir>/shared/memory/` (the "graph"):

  index.md              root MOC-of-MOCs. Always injected in full.
  chats/<id>/index.md   per-chat note (type: chat): the agent's GROWING summary
                        of one chat — durable facts + intent + a running summary
                        (+ the gist that IS the chat name). The most-recently-
                        modified ~10 are injected in full after the index. This
                        is the PRIMARY day-time memory carrier (there is no
                        shared inbox); the nightly pass consolidates them into
                        notes/.
  mocs/<topic>.md       topic hubs (curated [[links]]); read on demand.
  notes/<slug>.md       atomic notes (one fact each) with OKF frontmatter
                        (type, title, description=scent line); read on demand.
  read-trace/<id>.json  per-chat record of which nodes were injected/read,
                        written by chat.py + the SDK runner (memory_trace.py);
                        the reflection pass diffs it against the graph.
  .ready                sentinel: present iff a validated graph is published.

The `.ready` sentinel — not the mere existence of `index.md` — gates graph
mode. A consolidation builds into a staging tree, lints, publishes atomically,
and only then writes `.ready`; a partial or failed publish therefore leaves the
previously published graph in place rather than exposing a half-built one.

`build_memory_block` is a PURE function (no writes, no logging) so it is
trivially unit-testable; the caller in `chat.py` owns the activity emit and
the surrounding `<agent_experience>` envelope.

Prompt-cache stability: the block is the index + the recent chat summaries in a
fixed (newest-first) order and injects no mocs/notes, so a nightly consolidation
can't reorder the cached first-message prefix and bust prompt-cache reuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mobius.memory")

# Budget for the always-injected portion. ~25 KB / 400 lines mirrors Claude
# Code's MEMORY.md index budget (research B1). The cap is enforced here so a
# runaway graph can never blow the context window; overflow detail stays on
# disk for on-demand Read.
DEFAULT_BUDGET_BYTES = 25_000
DEFAULT_MAX_NOTES = 12
# How many recent per-chat notes to inject at session start. Each is the
# agent's growing summary of one chat (durable facts + intent + a running
# summary); the most-recently-modified ones open a fresh session with recent
# conversational context. Replaces the old recent-chats queue + inbox tail.
RECENT_CHAT_NOTES = 10


@dataclass
class MemoryBlock:
  """Result of assembling the injected memory context.

  `text` is the bare context (no `<agent_experience>` envelope — the caller
  adds that plus the dynamic provider/timezone/viewport tail). `loaded` is the
  list of graph-relative paths that made it into the block, so the caller can
  credit their access (the `memory_load` activity event). `mode` is
  "graph" | "empty" for observability.
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
# history. `build_memory_block` returns `loaded`; the injection site calls
# `record_usage(loaded)`. `load_usage` feeds the graph builder (the Memory
# viewer's "Used" column), so the effective access_count = frontmatter baseline
# + live usage. Keyed by node id (a note's slug), matching graph.json.
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


def record_usage(data_dir: str | Path, loaded: list[str]) -> None:
  """Increments the usage counter for every loaded note id. Best-effort and
  side-effecting — call it from the injection site, NOT from
  `build_memory_block` (which stays pure). Atomic temp-write + rename so a
  concurrent chat start can't read a half-written counter."""
  import json
  import os
  import tempfile
  ids = [i for i in (_loaded_path_to_id(p) for p in loaded) if i]
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
  """Assembles the injected memory context from the knowledge graph.

  `index.md` (the router, full, capped to the budget) + the full summaries of
  the ~10 most-recently-modified `chats/<id>/index.md` notes (newest first,
  budget-guarded). NO mocs/notes are injected — the deeper graph is read on
  demand by the memory-search subagent. Each included file is fenced with a
  `<<< path >>>` marker so the agent knows what a `[[link]]` resolves to.

  Returns an empty block when the graph is not yet published (`.ready` absent)
  or is empty — the agent then has no injected memory for this turn but can
  still `Read` the graph on demand. `.ready` is written atomically after the
  graph lints, so a partial/failed publish leaves the previous graph in place
  rather than handing over a half-built one.

  Pure: never writes, never raises on a missing/garbled file.
  """
  # Clamp at 0 so a stray negative budget can't reach the byte-slicing below,
  # where a negative limit returns a SUFFIX of the text instead of empty.
  budget_bytes = max(0, budget_bytes)
  root = memory_dir(data_dir)
  if is_graph_ready(data_dir):
    return _build_graph_block(root, budget_bytes, max_notes)
  return MemoryBlock(text="", loaded=[], mode="empty")


def _recent_chat_notes(root: Path, limit: int) -> list[Path]:
  """The per-chat note files (`chats/<id>/index.md`), most-recently-modified
  first, capped at `limit`. These are the growing chat summaries injected at
  session start. Tolerates a missing `chats/` dir (returns [])."""
  chats = root / "chats"
  if not chats.is_dir():
    return []
  notes = [p for p in chats.glob("*/index.md") if p.is_file()]
  notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
  return notes[:limit]


def _build_graph_block(
  root: Path, budget_bytes: int, max_notes: int
) -> MemoryBlock:
  parts: list[str] = []
  loaded: list[str] = []
  used = 0

  index = _read(root / "index.md").strip()
  if index:
    if len(index.encode("utf-8")) > budget_bytes:
      # Reserve room for the marker so index + marker stays within budget —
      # truncating to the full budget and THEN appending overran it by the
      # marker's length. If the budget is smaller than the marker itself
      # (pathological/tiny), drop the marker and hard-truncate to the budget
      # rather than passing a NEGATIVE limit to _truncate_bytes (which would
      # slice from the END and still overflow).
      marker = "\n\n[index truncated to fit the memory budget]"
      marker_len = len(marker.encode("utf-8"))
      if budget_bytes <= marker_len:
        index = _truncate_bytes(index, budget_bytes)
      else:
        index = _truncate_bytes(index, budget_bytes - marker_len) + marker
    parts.append(index)
    loaded.append("index.md")
    used += len(index.encode("utf-8"))

  # Recent chat NOTES — the growing per-chat summaries that ARE the day's
  # memory (there is no shared inbox). The most-recently-modified chats are
  # injected in full, newest first, so a new session opens with recent
  # conversational context: the durable facts + intent + the running summary
  # the agent maintains in each chat's `chats/<id>/index.md`. The nightly pass
  # consolidates these into the graph. The deeper graph (notes/, mocs/) is NOT
  # injected — it is read on demand by the memory-search subagent. A budget
  # guard stops injection before the block exceeds budget_bytes.
  for note in _recent_chat_notes(root, RECENT_CHAT_NOTES):
    body = _read(note).strip()
    if not body:
      continue
    rel = f"chats/{note.parent.name}/index.md"
    chunk = f"<<< {rel} (recent chat summary) >>>\n{body}"
    if used + len(chunk.encode("utf-8")) + 2 > budget_bytes:
      break
    parts.append(chunk)
    loaded.append(rel)
    used += len(chunk.encode("utf-8")) + 2

  if not parts:
    return MemoryBlock(text="", loaded=[], mode="empty")
  return MemoryBlock(text="\n\n".join(parts), loaded=loaded, mode="graph")
