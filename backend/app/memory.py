"""Assembles the agent's injected memory block from the knowledge graph.

Möbius gives the agent its long-term memory by prepending a block to the
FIRST user message of a session (see `chat.py`). Historically that block was
the entire flat `/data/shared/agent-experience.md`. This module builds a
*progressive-disclosure* block instead: a small always-loaded index (the root
"Home" MOC) plus the highest-value notes, with everything else left on disk
for the agent to `Read` on demand by following `[[wikilinks]]`.

Layout under `<data_dir>/shared/memory/` (the "graph"):

  index.md              root MOC-of-MOCs. Always injected in full.
  inbox.md              persistent append-only buffer for the day's raw
                        observations; injected as a tail so same-day learnings
                        are visible next session. Consolidated into notes by
                        the nightly "dreaming" pass, then truncated.
  mocs/<topic>.md       topic hubs (curated [[links]]); read on demand.
  notes/<slug>.md       atomic notes (one fact each) with YAML frontmatter
                        carrying `importance` (1-5) and `access_count`.
  .ready                sentinel: present iff a validated graph is published.

The `.ready` sentinel — not the mere existence of `index.md` — gates graph
mode. A migration/consolidation builds into a staging tree, lints, publishes
atomically, and only then writes `.ready`; a partial or failed migration
therefore never disables the legacy fallback (Codex review R2).

`build_memory_block` is a PURE function (no writes, no logging) so it is
trivially unit-testable; the caller in `chat.py` owns the activity emit and
the surrounding `<agent_experience>` envelope.

Selection vs. rendering order (Codex review R3): hot notes are *selected* by
score (importance, then access_count) but *rendered* in stable path order, so
a nightly access_count change can't reorder the cached first-message prefix
and bust prompt-cache reuse.
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
# The inbox can grow unbounded between nightly consolidations; only its tail
# is injected so a busy day can't crowd out the index + hot notes.
INBOX_TAIL_BYTES = 6_000


@dataclass
class MemoryBlock:
  """Result of assembling the injected memory context.

  `text` is the bare context (no `<agent_experience>` envelope — the caller
  adds that plus the dynamic provider/timezone/viewport tail). `loaded` is the
  list of graph-relative paths that made it into the block, so the caller can
  credit their access (the `memory_load` activity event). `mode` is
  "graph" | "legacy" | "empty" for observability.
  """

  text: str
  loaded: list[str] = field(default_factory=list)
  mode: str = "empty"


def memory_dir(data_dir: str | Path) -> Path:
  return Path(data_dir) / "shared" / "memory"


def is_graph_ready(data_dir: str | Path) -> bool:
  """Graph mode is active iff the atomic `.ready` sentinel is present."""
  return (memory_dir(data_dir) / ".ready").is_file()


# ─── Usage tracking (the "access_count" / Mind "Used" signal) ───────────
#
# access_count is meant to be "how often a note was loaded" (the hotness
# signal _note_score reads), but nothing ever incremented it — so every
# note read 0 and the Mind app's "Used" column was uniformly zero. We track
# it in a sidecar counter (`usage.json`) rather than rewriting note
# frontmatter on the hot path: a counter bump is cheap and churns no git
# history. `build_memory_block` returns `loaded`; the injection site calls
# `record_usage(loaded)`. `load_usage` feeds both hot-note selection and the
# graph builder, so the effective access_count = frontmatter baseline + live
# usage. Keyed by node id (a note's slug), matching graph.json.
def _usage_path(data_dir: str | Path) -> Path:
  return memory_dir(data_dir) / "usage.json"


def _loaded_path_to_id(rel: str) -> str | None:
  """Maps a `loaded` entry (e.g. 'notes/foo.md', 'index.md') to its graph
  node id. inbox.md and anything unrecognised return None (not counted)."""
  import os
  name = os.path.basename(rel)
  if name == "inbox.md":
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
  ids = [i for i in (_loaded_path_to_id(p) for p in loaded) if i]
  if not ids:
    return
  counts = load_usage(data_dir)
  for nid in ids:
    counts[nid] = counts.get(nid, 0) + 1
  path = _usage_path(data_dir)
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(counts), encoding="utf-8")
    os.replace(tmp, path)
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


def _note_score(fm: dict[str, object]) -> tuple[int, int]:
  """Selection key: importance (1-5, author/dreaming-set) dominates, then
  access_count (how often the note was loaded — the MDL hotness signal).
  Missing fields score 0 so an unannotated note sorts last but is still
  eligible."""
  imp = fm.get("importance")
  acc = fm.get("access_count")
  return (
    imp if isinstance(imp, int) else 0,
    acc if isinstance(acc, int) else 0,
  )


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


def _select_hot_notes(
  notes_dir: Path, max_notes: int
) -> list[tuple[Path, str]]:
  """Returns up to `max_notes` (path, full_text) pairs SELECTED by score but
  SORTED by path, so the rendered order is stable across nightly score
  changes (Codex review R3)."""
  candidates: list[tuple[tuple[int, int], Path, str]] = []
  try:
    files = sorted(notes_dir.glob("*.md"))
  except OSError:
    return []
  # Live usage (notes_dir is <memory>/notes, so the sidecar is one level up).
  # Effective hotness = frontmatter access_count baseline + live load count,
  # so a note the agent keeps loading rises even if its seed baseline is 0.
  usage = _read_usage_file(notes_dir.parent / "usage.json")
  for fp in files:
    text = _read(fp)
    if not text.strip():
      continue
    imp, base_acc = _note_score(parse_frontmatter(text))
    score = (imp, base_acc + usage.get(fp.stem, 0))
    candidates.append((score, fp, text))
  # Select the top-N by score (descending), then re-sort the winners by path.
  candidates.sort(key=lambda c: c[0], reverse=True)
  winners = candidates[:max_notes]
  winners.sort(key=lambda c: c[1].name)
  return [(fp, text) for _, fp, text in winners]


def build_memory_block(
  data_dir: str | Path,
  *,
  budget_bytes: int = DEFAULT_BUDGET_BYTES,
  max_notes: int = DEFAULT_MAX_NOTES,
) -> MemoryBlock:
  """Assembles the injected memory context.

  Graph mode (`.ready` present): `index.md` (full, capped to the budget) +
  hot notes (selected by score, rendered in path order, until the byte budget
  is consumed) + the `inbox.md` tail. Each included file is fenced with a
  `<<< path >>>` marker so the agent knows what a `[[link]]` resolves to.

  Legacy mode (no `.ready`): the flat `agent-experience.md`, unchanged — the
  exact pre-graph behaviour, so an instance never loses memory on upgrade or a
  failed migration.

  Pure: never writes, never raises on a missing/garbled file.
  """
  root = memory_dir(data_dir)
  if is_graph_ready(data_dir):
    block = _build_graph_block(root, budget_bytes, max_notes)
    if block.mode != "empty":
      return block
    # .ready is present but the graph yielded nothing (index/notes/inbox all
    # empty or deleted) — don't hand the agent an empty memory block; fall
    # through to the legacy file so an emptied-but-published graph never
    # silently wipes the agent's memory.

  legacy = Path(data_dir) / "shared" / "agent-experience.md"
  ctx = _read(legacy).strip()
  if not ctx:
    return MemoryBlock(text="", loaded=[], mode="empty")
  return MemoryBlock(text=ctx, loaded=["agent-experience.md"], mode="legacy")


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
      # marker's length.
      marker = "\n\n[index truncated to fit the memory budget]"
      index = _truncate_bytes(index, budget_bytes - len(marker.encode("utf-8")))
      index += marker
    parts.append(index)
    loaded.append("index.md")
    used += len(index.encode("utf-8"))

  # Hot notes fill the remaining budget. Skip entirely if the index already
  # consumed it (Codex review P3 — oversized index).
  for fp, text in _select_hot_notes(root / "notes", max_notes):
    body = text.strip()
    rel = f"notes/{fp.name}"
    chunk = f"<<< {rel} >>>\n{body}"
    cost = len(chunk.encode("utf-8")) + 2
    if used + cost > budget_bytes:
      continue
    parts.append(chunk)
    loaded.append(rel)
    used += cost

  inbox = _read(root / "inbox.md").strip()
  if inbox:
    tail = _truncate_bytes(inbox, INBOX_TAIL_BYTES)
    if tail != inbox:
      # Keep the most recent observations, not the oldest.
      tail = inbox.encode("utf-8")[-INBOX_TAIL_BYTES:].decode(
        "utf-8", errors="ignore"
      )
      tail = "[older inbox entries omitted]\n" + tail
    inbox_chunk = f"<<< inbox.md (recent, unconsolidated) >>>\n{tail}"
    # Budget the inbox like the hot notes above (+2 is the "\n\n"
    # separator). INBOX_TAIL_BYTES caps only the tail body, not the
    # header+marker+separator, so the chunk could still push the block
    # past budget_bytes when a large index leaves little room — this
    # check keeps build_memory_block within its ~budget_bytes contract.
    if used + len(inbox_chunk.encode("utf-8")) + 2 <= budget_bytes:
      parts.append(inbox_chunk)
      loaded.append("inbox.md")
      used += len(inbox_chunk.encode("utf-8")) + 2

  if not parts:
    return MemoryBlock(text="", loaded=[], mode="empty")
  return MemoryBlock(text="\n\n".join(parts), loaded=loaded, mode="graph")
