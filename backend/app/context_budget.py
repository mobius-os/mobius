"""Budgets for agent-written text that Möbius repeats into provider context.

Agent input is not capped where it is written: the store keeps it whole and an
explicit read returns it whole. What Möbius re-sends on every turn (and again
after compaction) is bounded where that context is assembled, so one long plan
or outcome can never crowd out the owner's work or make every turn fail.

24,000 characters is roughly 6k tokens, a few percent of a provider context
window. Text over budget becomes a marked excerpt that names where the full
text is read.
"""

import json

# Combined prose fields (titles, notes, results, conditions, next action) in
# one Goal brief, and the most any single field takes of it.
GOAL_BRIEF_CHARS = 24_000
GOAL_BRIEF_FIELD_CHARS = 4_000
# Summary entries per list in a Goal brief (children, siblings, blockers,
# dependencies). Each entry's own prose is still charged to the budget above.
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
  """``text`` whole when it fits ``limit``, else its head marked as an excerpt."""
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


def _head_within(text: str, width: int) -> int:
  """The longest prefix length of ``text`` whose emitted width fits ``width``."""
  low, high = 0, min(len(text), max(width, 0))
  while low < high:
    middle = (low + high + 1) // 2
    if emitted_len(text[:middle]) <= width:
      low = middle
    else:
      high = middle - 1
  return low


class TextBudget:
  """Hand out whole texts while a combined character budget lasts."""

  def __init__(self, total: int, *, field: int | None = None) -> None:
    self.remaining = total
    self.field = field

  def take(self, text: str, read_full: str) -> str:
    """Return ``text`` whole, or a marked excerpt naming ``read_full``.

    Widths are emitted widths (see ``emitted_len``).
    """
    allowance = self.remaining if self.field is None else min(self.remaining, self.field)
    width = emitted_len(text)
    if width <= allowance:
      self.remaining -= width
      return text
    # The marker is charged too, so head plus marker must fit the allowance.
    # The marker names the head's length, and a head shorter than the
    # allowance never needs more digits, so reserving the marker as it would
    # read for a full-allowance head is enough. A marker longer than the text
    # it replaces is never used.
    marker = emitted_len(f"… [excerpt: {allowance} of {len(text)} characters; {read_full}]")
    shown = excerpt(text, _head_within(text, allowance - marker), read_full)
    return self.charge(text if emitted_len(shown) >= width else shown)

  @property
  def spent(self) -> bool:
    return self.remaining <= 0

  def charge(self, text: str) -> str:
    """Count text the assembler adds itself, such as an omission marker."""
    self.remaining -= emitted_len(text)
    return text

  def take_ids(self, ids: list[str]) -> tuple[list[str], int]:
    """Keep ids while the budget lasts; return them and how many were left out."""
    kept = []
    for value in ids:
      cost = emitted_len(value) + 3  # its quotes and separator in the block
      if cost > self.remaining:
        break
      self.remaining -= cost
      kept.append(value)
    return kept, len(ids) - len(kept)
