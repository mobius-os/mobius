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

# Combined peer-note bodies delivered in one turn's coordination block.
PEER_NOTES_CHARS = 24_000
# Combined prose fields (titles, notes, results, conditions, next action) in
# one Goal brief, and the most any single field takes of it.
GOAL_BRIEF_CHARS = 24_000
GOAL_BRIEF_FIELD_CHARS = 4_000
# Summary entries per list in a Goal brief (children, siblings, blockers,
# dependencies). Each entry's own prose is still charged to the budget above.
GOAL_BRIEF_LIST_ITEMS = 50


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
    head = text[:max(allowance, 0)].rstrip()
    self.remaining -= len(head)
    return f"{head}… [excerpt: {len(head)} of {len(text)} characters; {read_full}]"
