"""Budgets for agent-written text that Möbius repeats into provider context.

Agent input is not capped where it is written: the store keeps it whole and an
explicit read returns it whole. What Möbius re-sends on every turn (and again
after compaction) is bounded where that context is assembled, so one long note
or plan can never crowd out the owner's work or make every turn fail.

24,000 characters is roughly 6k tokens, a few percent of a provider context
window, so peer notes and the Goal brief together stay a small share of every
turn. Text over budget becomes a marked excerpt that names where the full text
is read.
"""

# Combined peer-note bodies delivered in one turn's coordination block, split
# by fair_shares so every note keeps a share.
PEER_NOTES_CHARS = 24_000
# Combined prose fields (titles, notes, results, conditions, next action) in
# one Goal brief, and the most any single field takes of it.
GOAL_BRIEF_CHARS = 24_000
GOAL_BRIEF_FIELD_CHARS = 4_000
# Summary entries per list in a Goal brief (children, siblings, blockers,
# dependencies). Each entry's own prose is still charged to the budget above.
GOAL_BRIEF_LIST_ITEMS = 50


def excerpt(text: str, limit: int, read_full: str) -> str:
  """``text`` whole when it fits ``limit``, else its head marked as an excerpt."""
  if len(text) <= limit:
    return text
  head = text[:max(limit, 0)].rstrip()
  return f"{head}… [excerpt: {len(head)} of {len(text)} characters; {read_full}]"


def fair_shares(lengths: list[int], total: int) -> list[int]:
  """Split ``total`` so short texts stay whole and long ones share the rest.

  Shortest first, each text gets at most an equal share of what is left, so
  the result does not depend on order: an urgent short note is never blanked
  by earlier long ones, and every long note keeps a share.
  """
  shares = [0] * len(lengths)
  remaining = total
  order = sorted(range(len(lengths)), key=lengths.__getitem__)
  for position, index in enumerate(order):
    shares[index] = min(lengths[index], remaining // (len(order) - position))
    remaining -= shares[index]
  return shares


class TextBudget:
  """Hand out whole texts while a combined character budget lasts."""

  def __init__(self, total: int, *, field: int | None = None) -> None:
    self.remaining = total
    self.field = field

  def take(self, text: str, read_full: str) -> str:
    """Return ``text`` whole, or a marked excerpt naming ``read_full``."""
    allowance = self.remaining if self.field is None else min(self.remaining, self.field)
    if len(text) <= allowance:
      self.remaining -= len(text)
      return text
    # The marker is part of the brief too: leave room for it in the head, and
    # never swap a text for a marker longer than the text itself.
    room = allowance - len(excerpt("x" * (len(text) + 1), 0, read_full)) - len(str(allowance))
    shown = excerpt(text, max(room, 0), read_full)
    return self.charge(text if len(shown) >= len(text) else shown)

  @property
  def spent(self) -> bool:
    return self.remaining <= 0

  def charge(self, text: str) -> str:
    """Count text the assembler adds itself, such as an omission marker."""
    self.remaining -= len(text)
    return text

  def take_ids(self, ids: list[str]) -> tuple[list[str], int]:
    """Keep ids while the budget lasts; return them and how many were left out."""
    kept = []
    for value in ids:
      cost = len(value) + 3  # its quotes and separator in the JSON block
      if cost > self.remaining:
        break
      self.remaining -= cost
      kept.append(value)
    return kept, len(ids) - len(kept)
