"""What a turn restores after its provider compacts context mid-turn.

A turn's start message carries the Goal brief and, for a top-level chat, the
open owner-request list. Provider-native compaction inside a running turn can
summarize both away, so each provider's post-compaction boundary takes them
again, read fresh at that moment:

- Claude: a ``SessionStart`` callback (source ``compact``) adds them to the
  very request that continues the compacted turn; ``PreCompact`` marks the
  need, and the next root ``PostToolUse`` result is the fallback boundary.
  A Claude helper-host turn (a subagent, for which the CLI runs no
  ``SessionStart`` hooks) uses ``PostToolUse`` only.
- Codex: a ``SessionStart`` command hook matching ``^compact$``
  (``scripts/codex_goal_brief_hook.py``), whose call is itself the signal.

Delegated helpers get their assignment brief only: the owner's request list
belongs to the top-level chat, and a helper asks its parent instead.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class CompactionContextRefresh:
  """Carry one fresh copy of a turn's restorable context across compaction.

  Turn starts already carry it. Within a turn, a provider that compacts its
  context marks this stale, and the next supported provider boundary takes
  the current text once. Nothing is stored in the transcript and no model
  call is added. The loader opens its own short session: provider turns run
  after the request session is released.

  Delivery is exactly once per need: concurrent boundaries (parallel tool
  results) serialize, a failed load keeps the need for the next boundary, and
  a compaction during an in-flight load is a new need, never absorbed by it.
  """

  def __init__(self, load):
    self._load = load
    self._marked = 0
    self._delivered = 0
    self._lock = asyncio.Lock()

  @property
  def stale(self):
    return self._marked > self._delivered

  def mark_compacted(self):
    self._marked += 1

  async def take(self):
    if not self.stale:
      return ""
    async with self._lock:
      need = self._marked
      if need <= self._delivered:
        return ""
      try:
        text = await asyncio.to_thread(self._load)  # unsettled: read-only (loads the turn's context)
      except Exception:
        log.warning("Compaction context refresh failed; retrying at the next boundary",
                    exc_info=True)
        return ""
      # Empty text is a successful load: nothing is left to restore.
      self._delivered = need
    return text or ""


def compaction_context(db, chat_id, run_id, *, delegated):
  """The Goal brief and, for a top-level turn, the open owner requests, now.

  The lead line names where each part is read whole, so a provider that
  clips hook context still points the agent at the full reads.
  """
  from app.goals import turn_goal_brief

  parts, reads = [], []
  brief = turn_goal_brief(db, chat_id, run_id, delegated=delegated)
  if brief:
    parts.append(brief)
    # A helper re-reads its brief with read_goal; a coordinator's overview is
    # update_goal with no arguments (read_goal there only expands a task).
    reads.append("current Goal brief follows (" + (
      "read_goal re-reads it" if delegated else "update_goal with no arguments shows the plan"
    ) + ")")
  if not delegated:
    from app.owner_input_requests import build_open_requests_context
    # No "the owner wrote" framing: no owner message follows this text, and
    # the turn-start or steered copy that carried it was already acted on.
    requests = build_open_requests_context(db, chat_id)
    if requests:
      parts.append(requests)
      reads.append("open owner requests follow (owner_input with op list shows them)")
  if not parts:
    return ""
  return f"Context was compacted; {'; '.join(reads)}.\n" + "\n\n".join(parts)


def compaction_context_refresh(chat_id, run_id, *, delegated):
  def load():
    from app.database import SessionLocal
    with SessionLocal() as db:
      return compaction_context(db, chat_id, run_id, delegated=delegated)
  return CompactionContextRefresh(load)
