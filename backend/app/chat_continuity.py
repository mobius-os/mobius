"""The agent-authored chat note at ``shared/memory/chats/<id>/index.md``.

The working agent saves its chat's name, short ``## Summary``, and entries for
the append-only full ``## Digest`` through ``checkpoint_chat``. This module applies
one save to the note in the format every reader already parses (new-session
context, provider handoff, Memory, Reflection), so there is no second store to
keep in sync and no format version to branch on.
"""

from __future__ import annotations

import os
import json
import hashlib
import tempfile
import re
from datetime import UTC, datetime
from pathlib import Path

from app.chat_notes import extract_chat_summary, extract_full_digest, extract_section

# Frontmatter written by retired writers (the turn-end publisher and the
# short-lived journal projection).
_RETIRED_KEYS = frozenset({
  "source_message_count", "source_messages_sha256", "continuity_version", "revision",
})
# Readers split the note on level-two headings, so agent text may not add one.
_SECTION_HEADING = re.compile(r"^[ \t]*## ", re.MULTILINE)


def _as_note_text(value: str) -> str:
  return _SECTION_HEADING.sub("### ", value.strip())


def note_path(data_dir: str | Path, chat_id: str) -> Path:
  return Path(data_dir) / "shared" / "memory" / "chats" / chat_id / "index.md"


def _split_frontmatter(text: str) -> tuple[list[str], str]:
  if text.startswith("---\n"):
    end = text.find("\n---", 3)
    if end != -1:
      return text[4:end].splitlines(), text[end + 4:].lstrip("\n")
  return [], text


def apply_checkpoint(
  note: str | None,
  *,
  name: str,
  summary: str | None = None,
  digest: str | None = None,
  now: datetime | None = None,
  coverage: dict | None = None,
) -> str:
  """Return ``note`` with one save applied; other sections are preserved.

  ``summary`` replaces the short chat summary. ``digest`` appends one
  timestamped entry to the full digest. The name always mirrors the chat's
  committed title, so owner renames are picked up on the next save.
  """
  meta, body = _split_frontmatter(note or "")
  meta = [
    line for line in meta
    if line.partition(":")[0].strip() not in _RETIRED_KEYS | {"description", "type"}
  ]
  old_summary = extract_chat_summary(body)
  full = extract_full_digest(body)
  if full is None and old_summary is None and "\n## " not in f"\n{body}":
    full = body.strip() or None  # A section-less legacy note is history.
  if digest:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"### {stamp}\n\n{_as_note_text(digest)}"
    full = f"{full}\n\n{entry}" if full else entry
    # Only an authored digest entry advances coverage; a rename or the short
    # summary says nothing about what history was covered.
    meta = [line for line in meta if not line.startswith("recovery_coverage:")]
    if coverage is not None:
      bound = {**coverage, "digest_sha256": digest_fingerprint(full or "")}
      meta.append("recovery_coverage: " + json.dumps(bound, sort_keys=True))
  current = _as_note_text(summary) if summary is not None else old_summary

  one_line_name = " ".join(name.split())
  parts = ["---", "type: chat", f"description: {one_line_name}", *meta, "---", ""]
  parts += ["## Summary", "", current or "", ""]
  parts += ["## Digest", "", full or "", ""]
  for heading in ("Facts & intent", "Related"):
    kept = extract_section(body, heading)
    if kept:
      parts += [f"## {heading}", "", kept, ""]
  return "\n".join(parts).rstrip() + "\n"


def write_note(path: Path, text: str) -> None:
  """Atomically replace one note."""
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(prefix=".index.", suffix=".tmp", dir=path.parent)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(text)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def digest_fingerprint(digest: str) -> str:
  return hashlib.sha256(digest.encode("utf-8")).hexdigest()


def checkpoint_coverage(messages: list[dict], run_token: str) -> dict:
  """Bind sealed earlier turns, deliberately leaving this turn in the tail.

  Steering can seal several assistant segments during one physical run. Stop
  at its FIRST segment, then keep the preceding owner input in the tail too.
  No current-turn tool result or concurrent steer is claimed as covered.
  """
  from app.chat_message_identity import assistant_message_run_id
  from app.chat_writer import messages_fingerprint

  frontier = next((i for i, row in enumerate(messages)
    if row.get("role") == "assistant"
    and assistant_message_run_id(row.get("id")) == run_token), len(messages))
  count = next((i + 1 for i in range(frontier - 1, -1, -1)
    if messages[i].get("role") == "assistant"), 0)
  return {"message_count": count, "messages_sha256": messages_fingerprint(messages[:count])}


def recovery_source(note: str, messages: list[dict]) -> tuple[str, list[dict]]:
  """Use the detailed note only with its unchanged, explicitly saved prefix.

  The author owns semantic completeness. This binding proves source identity,
  not that a model remembered every fact. Legacy/unbound notes remain readable
  but cannot silently replace transcript intervals during automatic recovery.
  """
  from app.chat_writer import messages_fingerprint

  digest = extract_full_digest(note) or ""
  meta, _ = _split_frontmatter(note)
  try:
    raw = next(line.partition(":")[2] for line in meta
               if line.startswith("recovery_coverage:"))
    coverage = json.loads(raw)
    count = coverage["message_count"]
    if (type(count) is not int or not 0 <= count <= len(messages)
        or coverage["digest_sha256"] != digest_fingerprint(digest)
        or coverage["messages_sha256"] != messages_fingerprint(messages[:count])
        or not digest.strip()):
      raise ValueError
  except (StopIteration, KeyError, TypeError, ValueError):
    raise ValueError("The saved handoff does not have verified conversation coverage.") from None
  return digest, messages[count:]
