"""Persists per-chat read traces of the memory graph for the nightly pass.

The Reflection consolidation diff — "what did today's agents actually see
vs. what WOULD have helped them" — needs the read side of that diff
recorded somewhere durable. Two signals feed it:

  - the injected block: `chat.py` records which nodes `build_memory_block`
    loaded into the session's first user message;
  - explicit reads: `claude_sdk_runner`'s `can_use_tool` records each
    mid-turn `Read` of a `notes/` or `mocs/` file.

Both merge into `<data_dir>/shared/memory/read-trace/<chat_id>.json`:

  {"chat_id": "...", "dates": ["YYYY-MM-DD", ...],
   "nodes_injected": ["<node id>", ...], "nodes_read": ["<node id>", ...],
   "updated": "<ISO8601>"}

Node ids match `graph.json` ids (a file's slug), so Reflection can diff a
trace directly against the graph without re-deriving the mapping. The
two lists stay separate on purpose: `nodes_injected` is what the platform
pushed at the agent for free; `nodes_read` is what the agent went and dug
for — the second is the stronger relevance signal.

Every write here is FIRE-AND-FORGET: a trace failure must never block,
slow, or fail the turn that produced it, so all public functions swallow
their own errors. The directory is bounded by `prune_traces` (14 days),
called from the boot init and by the Reflection skill.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from app.memory import _loaded_path_to_id, memory_dir

log = logging.getLogger("mobius.memory")

# Traces older than this are deleted — by the boot-time sweep and by the
# nightly pass. Two weeks comfortably covers "the day's chats" plus a few
# skipped nights, without the dir growing one file per chat forever.
TRACE_RETENTION_DAYS = 14


def trace_dir(data_dir: str | Path) -> Path:
  return memory_dir(data_dir) / "read-trace"


def _safe_chat_id(chat_id: str) -> str:
  """Sanitizes a chat id for use as a filename (same rule the Codex
  prompt-file path applies before using a chat id as a path component)."""
  return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)


def _trace_path(data_dir: str | Path, chat_id: str) -> Path:
  return trace_dir(data_dir) / f"{_safe_chat_id(chat_id)}.json"


def _load(path: Path) -> dict:
  """Reads an existing trace, tolerating absence/corruption (→ {})."""
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def _str_list(value: object) -> list[str]:
  if not isinstance(value, list):
    return []
  return [v for v in value if isinstance(v, str)]


def _merge_and_write(
  data_dir: str | Path, chat_id: str, field: str, ids: list[str]
) -> None:
  """Read-merge-write one trace file, deduping `field` in arrival order.

  Atomic via a UNIQUE temp file + os.replace (the record_usage pattern):
  a concurrent reader never sees a half-written trace. Raises on failure;
  the public wrappers own the swallow-and-log."""
  path = _trace_path(data_dir, chat_id)
  trace = _load(path)
  today = datetime.now(UTC).date().isoformat()
  dates = _str_list(trace.get("dates"))
  if today not in dates:
    dates.append(today)
  merged = _str_list(trace.get(field))
  for nid in ids:
    if nid not in merged:
      merged.append(nid)
  trace.update({
    "chat_id": chat_id,
    "dates": dates,
    field: merged,
    "updated": datetime.now(UTC).isoformat(),
  })
  trace.setdefault("nodes_injected", [])
  trace.setdefault("nodes_read", [])
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(
    dir=str(path.parent), prefix=".trace-", suffix=".tmp"
  )
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
      json.dump(trace, fh, ensure_ascii=False)
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def record_injected(
  data_dir: str | Path, chat_id: str, loaded: list[str]
) -> None:
  """Merges an injection's loaded paths into the chat's trace.

  `loaded` is `MemoryBlock.loaded` (graph-relative paths); each maps to
  its graph node id. inbox.md and recent-chats.md map to None — they are
  rolling buffers, not graph nodes, so they'd be noise in the diff."""
  try:
    if not chat_id:
      return
    ids = [i for i in (_loaded_path_to_id(p) for p in loaded) if i]
    if not ids:
      return
    _merge_and_write(data_dir, chat_id, "nodes_injected", ids)
  except Exception:
    log.debug("memory_trace.record_injected failed", exc_info=True)


def record_note_read(
  data_dir: str | Path, chat_id: str, node_id: str
) -> None:
  """Merges one explicitly-Read node id into the chat's trace."""
  try:
    if not chat_id or not node_id:
      return
    _merge_and_write(data_dir, chat_id, "nodes_read", [node_id])
  except Exception:
    log.debug("memory_trace.record_note_read failed", exc_info=True)


def prune_traces(
  data_dir: str | Path, max_age_days: int = TRACE_RETENTION_DAYS
) -> int:
  """Deletes trace files older than `max_age_days` (by mtime).

  mtime, not the JSON `updated` field, so a corrupt trace still ages
  out instead of surviving forever. Returns the number removed;
  best-effort throughout (a locked file is skipped, not fatal)."""
  removed = 0
  try:
    cutoff = time.time() - max_age_days * 86400
    for fp in trace_dir(data_dir).glob("*.json"):
      try:
        if fp.stat().st_mtime < cutoff:
          fp.unlink()
          removed += 1
      except OSError:
        continue
  except OSError:
    pass
  return removed
