"""Small dependency-free text helpers for agent-facing surfaces."""

from __future__ import annotations


def elide(text: str, max_chars: int, *, note: str | None = None) -> tuple[str, bool]:
  """Bound text to ``max_chars`` while retaining useful head and tail context.

  A non-positive limit disables trimming. The returned boolean reports whether
  any source characters were omitted.
  """
  if max_chars <= 0 or len(text) <= max_chars:
    return text, False

  if note is not None:
    marker = f"\n…{note}…\n"
  else:
    # The marker length affects the number of source characters that fit.
    # Recalculate until the dropped-count width is stable.
    dropped = len(text) - max_chars
    while True:
      marker = f"\n…[{dropped} characters truncated]…\n"
      preserved = max(0, max_chars - len(marker))
      actual = len(text) - preserved
      if actual == dropped:
        break
      dropped = actual

  if len(marker) >= max_chars:
    return marker[:max_chars], True

  preserved = max_chars - len(marker)
  head = (preserved + 1) // 2
  tail = preserved // 2
  suffix = text[-tail:] if tail else ""
  return f"{text[:head]}{marker}{suffix}", True
