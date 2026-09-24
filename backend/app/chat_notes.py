"""Canonical readers for platform-owned per-chat continuity notes.

Chat summaries are Markdown, so the cumulative ``## Summary`` may legitimately
contain its own level-two headings (notably when a provider-free fallback
preserves assistant prose).  The platform sections, not arbitrary Markdown
headings, define the note boundary.  Keep that rule in one place so context
inspection and provider handoff cannot disagree about what the summary is.
"""

from __future__ import annotations


_SUMMARY_TERMINATORS = frozenset({"facts & intent", "related"})


def extract_section(
  text: str,
  heading: str,
  *,
  terminators: frozenset[str] | None = None,
) -> str | None:
  """Return a platform note section without mistaking nested prose for it."""
  lines = text.splitlines()
  target = heading.strip().lower()
  start: int | None = None
  for index, line in enumerate(lines):
    if line.strip().lower() == f"## {target}":
      start = index + 1
      break
  if start is None:
    return None

  body: list[str] = []
  for line in lines[start:]:
    stripped = line.strip()
    if stripped.startswith("## "):
      found = stripped[3:].strip().lower()
      if terminators is None or found in terminators:
        break
    body.append(line)
  value = "\n".join(body).strip()
  return value or None


def extract_cumulative_summary(text: str) -> str | None:
  """Read Summary through the next platform-owned peer section."""
  return extract_section(text, "Summary", terminators=_SUMMARY_TERMINATORS)
