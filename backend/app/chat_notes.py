"""Canonical readers for platform-owned per-chat continuity notes.

A note holds the short, replaceable ``## Summary`` and the append-only full
``## Digest``. The digest is Markdown and may legitimately contain its own
level-two headings (notably when a provider-free fallback preserved assistant
prose), so the platform sections, not arbitrary Markdown headings, define its
boundary. Keep that rule in one place so context inspection and provider
handoff cannot disagree about what the digest is.
"""

from __future__ import annotations


_DIGEST_TERMINATORS = frozenset({"facts & intent", "related"})


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


def extract_chat_summary(text: str) -> str | None:
  """Read the short, replaceable chat summary.

  The note keeps it above ``## Digest``, whose Markdown may contain its own
  ``## Summary`` headings, so only the part above the digest is read.
  """
  lines = text.splitlines()
  digest_at = next(
    (index for index, line in enumerate(lines) if line.strip().lower() == "## digest"),
    len(lines),
  )
  return extract_section("\n".join(lines[:digest_at]), "Summary")


def extract_full_digest(text: str) -> str | None:
  """Read the full digest through the next platform-owned peer section."""
  return extract_section(text, "Digest", terminators=_DIGEST_TERMINATORS)
