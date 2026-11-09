"""Display limits for agent-written text that Möbius repeats into a turn.

Agent input is not capped where it is written: the store keeps it whole and
the paged reads return it whole. Where Möbius repeats a text into a turn (the
per-turn Goal brief, a resolved-dependency notice, a claim notice or result),
it shows at most these display limits. They equal the input caps those fields
had before inputs became uncapped, so anything valid then is shown exactly as
before; a longer text becomes a marked excerpt naming the paged read.
"""

TITLE_CHARS = 160
# A task's note, result and completion condition; a Goal objective; a claim's
# outcome and takeover reason.
TEXT_CHARS = 1_000
# A Goal's next action and a deferral (hold) reason.
NEXT_ACTION_CHARS = 2_000
# A work claim's summary.
SUMMARY_CHARS = 500
# Entries per brief list and dependency ids per task, as a plan held them.
LIST_ITEMS = 64
DEPENDENCY_IDS = 16


# One page of a long control-tool result. Providers cap a tool result (Claude
# Code MCP at about 25k tokens; Codex truncates), so a 200k-character text
# could not arrive in one read; 40,000 characters (~10k tokens) fits.
READ_PAGE_CHARS = 40_000


def text_page(text: str, offset: int = 0) -> dict:
  """One character range of ``text``, with what the next read needs."""
  offset = max(int(offset), 0)
  if offset and offset >= len(text):
    raise ValueError(
      f"offset {offset} is past the end of this {len(text)}-character result; "
      "start again from offset 0"
    )
  end = min(offset + READ_PAGE_CHARS, len(text))
  return {
    "text": text[offset:end], "offset": offset, "total_chars": len(text),
    "next_offset": end if end < len(text) else None,
  }


def excerpt(text: str, limit: int, read_full: str) -> str:
  """``text`` whole if it fits ``limit`` characters, else a marked excerpt.

  The excerpt is its head plus a marker naming ``read_full``, together within
  ``limit`` whenever the marker alone fits.
  """
  if len(text) <= limit:
    return text
  # A shorter head never needs more digits than ``limit``, so reserving the
  # marker as it reads for a ``limit``-long head is enough.
  room = limit - len(f"… [excerpt: {limit} of {len(text)} characters; {read_full}]")
  head = text[:max(room, 0)].rstrip()
  return f"{head}… [excerpt: {len(head)} of {len(text)} characters; {read_full}]"
