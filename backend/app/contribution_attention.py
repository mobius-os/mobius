"""Attention flags on a contribution record, and which of them a move may clear.

The ledger holds one shown attention (`attention`, with `needs_attention`), the ones waiting
behind it (`waiting_attention`) and one parked outside note (`outside_activity`). Each carries
a `type` that is a flag name of the lifecycle tables. A flag a move does not name is left alone:
an escalation (`human_required`) is the owner's to clear, a GitHub flag is GitHub's or the
update's to clear, and neither may wipe the other.
"""

from __future__ import annotations

from typing import Collection


def _goes(item: object, flags: frozenset[str], untyped: bool) -> bool:
  if not isinstance(item, dict):
    return False
  kind = item.get("type")
  return kind in flags or (untyped and not kind)


def clear_flags(
  record: dict, flags: Collection[str], *, untyped: bool = False, shown_only: bool = False,
) -> bool:
  """Clear the named flags from the record in place; report whether anything changed.

  `untyped` also clears an attention that names no type: the records written before
  attention was typed, whose only writer was an escalation. `shown_only` looks at the
  attention on show alone, leaving the waiting ones and the parked outside note.
  """
  names = frozenset(flags)
  changed = False
  if _goes(record.get("attention"), names, untyped):
    record["attention"] = None
    record["needs_attention"] = False
    changed = True
  queued = record.get("waiting_attention")
  if not shown_only and isinstance(queued, list):
    kept = [item for item in queued if not _goes(item, names, untyped)]
    if len(kept) != len(queued):
      record["waiting_attention"] = kept
      changed = True
  if not shown_only and _goes(record.get("outside_activity"), names, untyped):
    del record["outside_activity"]
    changed = True
  return changed
