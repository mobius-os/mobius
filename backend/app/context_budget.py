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
# Summary entries per list in a Goal brief (children, siblings, blockers,
# dependencies, ancestors).
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


def excerpt(text: str, limit: int, read_full: str) -> str:
  """``text`` whole when it fits ``limit`` characters, else its head, marked."""
  if len(text) <= limit:
    return text
  head = text[:max(limit, 0)].rstrip()
  return f"{head}… [excerpt: {len(head)} of {len(text)} characters; {read_full}]"


def emitted_len(text: str) -> int:
  """How many characters ``text`` takes once written into a prompt data block.

  JSON escaping and the block's inert <, > and & escapes can multiply a
  symbol-heavy text several times over; budgets count what is emitted. Same
  encoding as ``app.prompt_blocks.json_block_data`` (a test pins them equal);
  this module stays import-free because the control server loads it by path.
  """
  encoded = json.dumps(text, ensure_ascii=False)
  return len(encoded) - 2 + 5 * sum(encoded.count(char) for char in "<>&")


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


def fit_emitted(text: str, width: int, read_full: str) -> str:
  """``text`` whole if its emitted width fits ``width``, else a marked excerpt.

  The marker names the head's length; a head shorter than ``width`` never
  needs more digits, so reserving the marker as it would read for a
  full-width head is enough. A marker longer than the text is never used.
  """
  full = emitted_len(text)
  if full <= width:
    return text
  marker = emitted_len(f"… [excerpt: {width} of {len(text)} characters; {read_full}]")
  low, high = 0, min(len(text), max(width - marker, 0))
  while low < high:  # the longest head whose emitted width fits
    middle = (low + high + 1) // 2
    if emitted_len(text[:middle]) <= width - marker:
      low = middle
    else:
      high = middle - 1
  shown = excerpt(text, low, read_full)
  return text if emitted_len(shown) >= full else shown


class Allowance:
  """A running emitted-width allowance for structure an assembler adds."""

  def __init__(self, total: int) -> None:
    self.remaining = total

  @property
  def spent(self) -> bool:
    return self.remaining <= 0

  def charge(self, text: str) -> str:
    self.remaining -= emitted_len(text)
    return text

  def take_ids(self, ids: list[str]) -> tuple[list[str], int]:
    """Keep ids while the allowance lasts; return them and how many were left out."""
    kept = []
    for value in ids:
      cost = emitted_len(value) + 3  # its quotes and separator in the block
      if cost > self.remaining:
        break
      self.remaining -= cost
      kept.append(value)
    return kept, len(ids) - len(kept)
