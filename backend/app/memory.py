"""Assembles the agent's injected memory block from the knowledge graph.

Möbius gives the agent its long-term memory by prepending a block to the
FIRST user message of a session (see `chat.py`). This module builds a
*progressive-disclosure* block: a small always-loaded index (the root "Home"
MOC) plus the highest-value notes, with everything else left on disk for the
agent to `Read` on demand by following `[[wikilinks]]`.

Layout under `<data_dir>/shared/memory/` (the "graph"):

  index.md              root MOC-of-MOCs. Always injected in full.
  recent-chats.md       fixed-size queue of the last ~10 chats (one line per
                        chat: id + date + summary), maintained by the nightly
                        "dreaming" pass; injected right after the index.
  inbox.md              persistent append-only buffer for the day's raw
                        observations; injected as a tail so same-day learnings
                        are visible next session. Consolidated into notes by
                        the nightly "dreaming" pass, then truncated.
  mocs/<topic>.md       topic hubs (curated [[links]]); read on demand.
  notes/<slug>.md       atomic notes (one fact each) with YAML frontmatter
                        carrying `importance` (1-5) and `access_count`.
  read-trace/<id>.json  per-chat record of which nodes were injected/read,
                        written by chat.py + the SDK runner (memory_trace.py);
                        the dreaming pass diffs it against the graph.
  .ready                sentinel: present iff a validated graph is published.

The `.ready` sentinel — not the mere existence of `index.md` — gates graph
mode. A consolidation builds into a staging tree, lints, publishes atomically,
and only then writes `.ready`; a partial or failed publish therefore leaves the
previously published graph in place rather than exposing a half-built one.

`build_memory_block` is a PURE function (no writes, no logging) so it is
trivially unit-testable; the caller in `chat.py` owns the activity emit and
the surrounding `<agent_experience>` envelope.

Selection vs. rendering order: hot notes are *selected* by score (importance,
then access_count) but *rendered* in stable path order, so a nightly
access_count change can't reorder the cached first-message prefix and bust
prompt-cache reuse.
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
# The recent-chats queue is dreaming-maintained at ~10 one-line entries, so
# this cap should never bind in practice; it bounds the damage if the file
# is ever grown by hand. The queue is chronological (oldest first), so any
# truncation keeps the newest TAIL.
RECENT_CHATS_TAIL_BYTES = 4_000


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
  node id. inbox.md, recent-chats.md, and anything unrecognised return None
  (rolling buffers aren't graph nodes — counting them would invent phantom
  ids in usage.json and the read-trace)."""
  import os
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


_FLOOR_CATEGORIES = ("user_model", "active_project")


def _frontmatter_mentions(fm: dict[str, object], key: str, needle: str) -> bool:
  value = fm.get(key)
  if isinstance(value, list):
    return any(str(item) == needle for item in value)
  if isinstance(value, str):
    return needle in value
  return False


def _note_floor_category(fm: dict[str, object]) -> str | None:
  """Returns the protected memory lane a note belongs to, if any.

  This is intentionally broad enough to cover both current `mocs: [...]`
  frontmatter and older live notes that used `maps: [[...]]`.
  """
  if (
    _frontmatter_mentions(fm, "mocs", "about-the-user")
    or _frontmatter_mentions(fm, "maps", "about-the-user")
    or _frontmatter_mentions(fm, "tags", "user")
    or _frontmatter_mentions(fm, "tags", "user-pref")
  ):
    return "user_model"
  if (
    _frontmatter_mentions(fm, "mocs", "building-mobius-apps")
    or _frontmatter_mentions(fm, "maps", "building-mobius-apps")
    or _frontmatter_mentions(fm, "tags", "project")
    or _frontmatter_mentions(fm, "tags", "app")
  ):
    return "active_project"
  return None


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
  changes."""
  candidates: list[tuple[tuple[int, int], Path, str, dict[str, object]]] = []
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
    fm = parse_frontmatter(text)
    # Redirect stubs are forwarding pointers left behind by a move/rename
    # (see memory_graph.py) — pure noise in the injected block.
    if fm.get("type") == "redirect":
      continue
    imp, base_acc = _note_score(fm)
    score = (imp, base_acc + usage.get(fp.stem, 0))
    candidates.append((score, fp, text, fm))
  # Select the top-N by score (descending). Protect the best user-model and
  # active-project notes from being crowded out by platform/meta hotness: this
  # is still value-ranked (best note per lane wins), but it prevents a mature
  # instance from silently opening a session with zero user/project context.
  candidates.sort(key=lambda c: c[0], reverse=True)
  if max_notes <= 0:
    return []
  winners_by_path = {cand[1]: cand for cand in candidates[:max_notes]}
  floor_paths: list[Path] = []
  for category in _FLOOR_CATEGORIES:
    if len(floor_paths) >= max_notes:
      break
    floor = next(
      (cand for cand in candidates if _note_floor_category(cand[3]) == category),
      None,
    )
    if floor is None:
      continue
    fp = floor[1]
    if fp not in floor_paths:
      floor_paths.append(fp)
    winners_by_path[fp] = floor

  protected = set(floor_paths)
  while len(winners_by_path) > max_notes:
    removable = [cand for fp, cand in winners_by_path.items() if fp not in protected]
    if not removable:
      break
    victim = min(removable, key=lambda c: (c[0][0], c[0][1], c[1].name))
    winners_by_path.pop(victim[1], None)

  # Render protected floors first so a tight byte budget cannot be consumed by
  # lower-value notes that sort earlier by filename. Render the rest by path to
  # preserve the stable prompt-cache-friendly order for the common case.
  floor_winners = [winners_by_path[fp] for fp in floor_paths if fp in winners_by_path]
  rest = [cand for fp, cand in winners_by_path.items() if fp not in protected]
  rest.sort(key=lambda c: c[1].name)
  return [(fp, text) for _, fp, text, _ in [*floor_winners, *rest]]


def build_memory_block(
  data_dir: str | Path,
  *,
  budget_bytes: int = DEFAULT_BUDGET_BYTES,
  max_notes: int = DEFAULT_MAX_NOTES,
) -> MemoryBlock:
  """Assembles the injected memory context from the knowledge graph.

  `index.md` (full, capped to the budget) + the `recent-chats.md` queue
  (truncated oldest-first when tight) + hot notes (selected by score,
  rendered in path order, until the byte budget is consumed) + the `inbox.md`
  tail. Each included file is fenced with a `<<< path >>>` marker so the agent
  knows what a `[[link]]` resolves to.

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

  # The recent-chats queue rides right after the index, before hot notes:
  # "what just happened" is orientation, like the index, and must not lose
  # its slot to a fat note. The queue is chronological (oldest first), so
  # when the remaining budget is tight it is truncated OLDEST-FIRST — the
  # newest tail survives.
  recent = _read(root / "recent-chats.md").strip()
  if recent:
    marker = "<<< recent-chats.md (last chats — oldest first) >>>\n"
    omitted = "[older recent-chats entries omitted]\n"
    overhead = len(marker.encode("utf-8")) + 2
    room = min(RECENT_CHATS_TAIL_BYTES, budget_bytes - used - overhead)
    body = recent
    if len(body.encode("utf-8")) > room:
      tail_room = room - len(omitted.encode("utf-8"))
      if tail_room > 0:
        body = omitted + body.encode("utf-8")[-tail_room:].decode(
          "utf-8", errors="ignore"
        )
      else:
        body = ""
    if body:
      chunk = marker + body
      parts.append(chunk)
      loaded.append("recent-chats.md")
      used += len(chunk.encode("utf-8")) + 2

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
