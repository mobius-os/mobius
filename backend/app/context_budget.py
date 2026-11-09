"""Display limits and read pages for agent-written Goal text.

Agent input to a Goal plan is not capped where it is written: the store keeps
it whole and the paged reads return it whole. The per-turn Goal brief shows
each task text and the next action within these display limits. They equal
the input caps those fields had before they became uncapped, so any plan
valid then is shown exactly as before; a longer text becomes a marked excerpt
naming the paged read. The Goal objective was never capped (an owner's /goal
is not) and is shown whole, as before.
"""

TITLE_CHARS = 160
# A task's note, result and completion condition.
TEXT_CHARS = 1_000
# A Goal's next action and a deferral (hold) reason.
NEXT_ACTION_CHARS = 2_000
# Entries per brief list and dependency ids per task, as a plan held them.
LIST_ITEMS = 64
DEPENDENCY_IDS = 16


# One page of a long control-tool result, in UTF-8 bytes. Providers cap a
# tool result (Claude Code MCP at about 25k tokens; Codex truncates). Bytes,
# not characters, so CJK and other multi-byte text stays within it too.
READ_PAGE_BYTES = 40_000


def text_page(text: str, offset: int = 0) -> dict:
  """One page of ``text`` from character ``offset``, within READ_PAGE_BYTES.

  Pages end on character boundaries; offsets count characters.
  """
  offset = max(int(offset), 0)
  if offset and offset >= len(text):
    raise ValueError(
      f"offset {offset} is past the end of this {len(text)}-character result; "
      "start again from offset 0"
    )
  low, high = 0, min(len(text) - offset, READ_PAGE_BYTES)
  while low < high:  # the most characters whose UTF-8 form fits one page
    middle = (low + high + 1) // 2
    if len(text[offset:offset + middle].encode("utf-8", "surrogatepass")) <= READ_PAGE_BYTES:
      low = middle
    else:
      high = middle - 1
  end = offset + low
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
