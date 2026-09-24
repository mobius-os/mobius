"""The agent-authored chat note at ``shared/memory/chats/<id>/index.md``.

The working agent saves its chat's name, short ``## Digest``, and entries for
the cumulative ``## Summary`` through ``checkpoint_chat``. This module applies
one save to the note in the format every reader already parses (new-session
context, provider handoff, Memory, Reflection), so there is no second store to
keep in sync and no format version to branch on.
"""

from __future__ import annotations

import os
import tempfile
import re
from datetime import UTC, datetime
from pathlib import Path

from app.chat_notes import extract_cumulative_summary, extract_section

# Frontmatter written by retired writers (the turn-end publisher and the
# short-lived journal projection).
_RETIRED_KEYS = frozenset({
  "source_message_count", "source_messages_sha256", "continuity_version", "revision",
})
# Readers split the note on level-two headings, so agent text may not add one.
_SECTION_HEADING = re.compile(r"^## ", re.MULTILINE)


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
  digest: str | None = None,
  summary: str | None = None,
  now: datetime | None = None,
) -> str:
  """Return ``note`` with one save applied; other sections are preserved.

  ``digest`` replaces the short current paragraph. ``summary`` appends one
  timestamped entry to the cumulative Summary. The name always mirrors the
  chat's committed title, so owner renames are picked up on the next save.
  """
  meta, body = _split_frontmatter(note or "")
  meta = [
    line for line in meta
    if line.partition(":")[0].strip() not in _RETIRED_KEYS | {"description", "type"}
  ]
  old_digest = extract_section(body, "Digest")
  cumulative = extract_cumulative_summary(body)
  if cumulative is None and old_digest is None and "\n## " not in f"\n{body}":
    cumulative = body.strip() or None  # A section-less legacy note is history.
  if summary:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"### {stamp}\n\n{_as_note_text(summary)}"
    cumulative = f"{cumulative}\n\n{entry}" if cumulative else entry
  current = _as_note_text(digest) if digest is not None else old_digest

  one_line_name = " ".join(name.split())
  parts = ["---", "type: chat", f"description: {one_line_name}", *meta, "---", ""]
  parts += ["## Digest", "", current or "", ""]
  parts += ["## Summary", "", cumulative or "", ""]
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
