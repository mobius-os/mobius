"""Budgets for agent-written text that Möbius assembles into provider context.

Agent input is not capped where it is written: the store keeps it whole and an
explicit read returns it whole. Text Möbius assembles into a turn (the
per-turn Goal brief, a resolved-dependency notice) is bounded where it is
assembled, so one long plan or outcome can never crowd out the owner's work
or make every turn fail.

TURN_SHARE_CHARS (24,000 characters, roughly 6k tokens) is a few percent of a
provider context window: the most one assembled block takes of a turn.
FIELD_SHARE_CHARS is the most any single text takes of that block. Text over
its share becomes a marked excerpt that names where the full text is read.
"""

import json

TURN_SHARE_CHARS = 24_000
FIELD_SHARE_CHARS = 4_000
# Entries per list and dependency ids per entry a Goal brief shows: a
# summary needs the first ones; read_goal(task=…) lists them all.
GOAL_BRIEF_LIST_ITEMS = 50


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


def emitted_len(text: str) -> int:
  """How many characters ``text`` takes once written into a prompt data block.

  JSON escaping and the block's inert <, > and & escapes can multiply a
  symbol-heavy text several times over; budgets count what is emitted. Same
  encoding as ``app.prompt_blocks.json_block_data`` (a test pins them equal);
  this module stays import-free because the control server loads it by path.
  """
  encoded = json.dumps(text, ensure_ascii=False)
  return len(encoded) - 2 + 5 * sum(encoded.count(char) for char in "<>&")


def excerpt(text: str, limit: int, read_full: str, *, width=len) -> str:
  """``text`` whole if it fits ``limit``, else its head and a marker that fit.

  ``width`` measures text: plain characters by default, ``emitted_len`` for
  text written into a prompt data block. Head plus marker fit ``limit``
  whenever the marker alone does; a marker longer than the text it would
  replace is never used.
  """
  full = width(text)
  if full <= limit:
    return text
  # A shorter head never needs more digits than ``limit``, so reserving the
  # marker as it reads for a ``limit``-long head is enough.
  room = limit - width(f"… [excerpt: {limit} of {len(text)} characters; {read_full}]")
  low, high = 0, min(len(text), max(room, 0))
  while low < high:  # the longest head that fits the room
    middle = (low + high + 1) // 2
    if width(text[:middle]) <= room:
      low = middle
    else:
      high = middle - 1
  head = text[:low].rstrip()
  shown = f"{head}… [excerpt: {len(head)} of {len(text)} characters; {read_full}]"
  return text if width(shown) >= full else shown


def fair_shares(demands: list[int], total: int) -> list[int]:
  """Split ``total`` so short texts stay whole and long ones share the rest.

  Shortest first, each gets at most an equal share of what is left, so the
  result does not depend on order and no text can starve another.
  """
  shares = [0] * len(demands)
  remaining = total
  order = sorted(range(len(demands)), key=demands.__getitem__)
  for position, index in enumerate(order):
    shares[index] = min(demands[index], remaining // (len(order) - position))
    remaining -= shares[index]
  return shares
