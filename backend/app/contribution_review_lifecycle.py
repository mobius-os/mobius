"""The Agent review and Verdict regions of the lifecycle machine, read off one review item.

Pure: no database, no clock. The tables are `contribution_lifecycle_tables` (the REVIEW and VERDICT
regions); this file only says how a review item's saved record becomes a vector and which facts move it.

A review item is one entry of a run's `outcomes_json`. Its saved `state` word is what every other writer
and the Contribute app already read, and it stays exactly as it was. The vector lives beside it, in the
optional key `lifecycle`, and is never trusted over the word: a vector the word contradicts is brought in
line first (the agent reported, so the review is no longer waiting). What the vector adds is what the word
cannot say: the review stalled, or is waiting on the owner's answer.

The chat's own state moves it too. A stalled review whose chat works again (a live run, or a finished turn
with a next move) is running again, with a new hour (the tables' CHAT_WORKING row); and the hour itself
only counts silence, so a review whose chat is still working is never stalled by it.

A verdict keeps its vector inside `verdict_proposal.lifecycle` the same way, beside its own `state` word.
"""
from __future__ import annotations

import calendar
from datetime import datetime

from app import contribution_lifecycle
from app import contribution_lifecycle_engine as engine
from app import contribution_lifecycle_tables as lifecycle_tables
from app import contribution_review_runs as reviews

TABLES = lifecycle_tables.tables()
KEY = "lifecycle"
REVIEW, VERDICT = "review", "verdict"
# The states a chat fact or the clock can still move; every other state is a result or a final word.
LIVE = ("REVIEWING", "ASKS", "STALLED")
# The states of a review that has reported. The word `reviewing` beside one of them means another round began.
REPORTED = ("CLEAR", "PROBLEMS", "NOT_FINISHED")
# What an agent's saved result means for the machine. A word that is not here says nothing about the review.
RESULT_EVENTS = {"all_clear": "OUTCOME_CLEAR", "merging": "OUTCOME_CLEAR", "merge_unknown": "OUTCOME_CLEAR",
                 "queued": "OUTCOME_CLEAR", "merged": "OUTCOME_CLEAR", "needs_you": "OUTCOME_PROBLEMS",
                 "repairing": "OUTCOME_PROBLEMS", "pushing": "OUTCOME_PROBLEMS", "push_unknown": "OUTCOME_PROBLEMS"}
VERDICT_STATES = {"proposed": "PROPOSED", "posting": "POSTING", "unknown": "UNKNOWN",
                  "not_dispatched": "NOT_DISPATCHED", "posted": "POSTED", "discarded": "DISCARDED"}


def epoch(moment: datetime | str | None) -> int | None:
  """Whole seconds since the epoch for a naive UTC datetime or an ISO string ending in Z."""
  if isinstance(moment, str):
    try:
      moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
      return None
  if not isinstance(moment, datetime):
    return None
  return int(moment.timestamp()) if moment.tzinfo else calendar.timegm(moment.timetuple())


def readable(vector: object, region: str = REVIEW) -> bool:
  """A stored vector this version of the tables can read, all of it: the one region it holds in a state the
  tables know with times the engine can compare, and a history of entries. A damaged one is not repaired; the
  caller seeds a new one from the item's word, so no read of a run fails on it (the record reader's rule)."""
  if not isinstance(vector, dict) or vector.get("v") != TABLES["version"]:
    return False
  regions, history = vector.get("regions"), vector.get("history") or []
  if not isinstance(regions, dict) or set(regions) != {region}:
    return False
  return contribution_lifecycle.slot_readable(region, regions[region]) and isinstance(history, list) and all(
    isinstance(entry, dict) for entry in history)


def _start(region: str, state: str, since: int) -> dict:
  return engine.new_vector(TABLES, {region: state}, since, attention=False)


def state_of(vector: dict, region: str = REVIEW) -> str:
  return vector["regions"][region]["state"]


def _explained(vector: dict, item: dict, at: int) -> dict:
  """The vector after the agent's saved result, when the word says the review has reported."""
  event = RESULT_EVENTS.get(item.get("state", "reviewing"))
  if event is None or state_of(vector) not in LIVE:
    return vector
  if event == "OUTCOME_PROBLEMS" and item.get("blocked"):
    event = "OUTCOME_BLOCKED"
  return engine.apply(TABLES, vector, {"type": event, "source": "agent", "inferred": True}, at)["vector"]


def _next_round(saved: dict, now: int) -> dict:
  """A fresh REVIEWING vector for an item the word sends back to `reviewing` (a repair, a moved base).

  A deliberate reseed, not a move the tables could make: they have no way back from a result (CLEAR,
  PROBLEMS, NOT_FINISHED), and the new round owes a new hour. The old vector is replaced and says so in its
  history, the way the engine's own reseed does.
  """
  fresh = _start(REVIEW, "REVIEWING", now)
  entry = {"at": now, "type": "RESEEDED", "source": "status", "region": REVIEW,
           "from": state_of(saved), "to": "REVIEWING", "inferred": True}
  fresh["history"] = [*(saved.get("history") or []), entry][-TABLES["history_limit"]:]
  return fresh


def review_vector(item: dict, started: int, now: int) -> dict:
  """This item's vector: the saved one, or one seeded at `started`; the saved `state` word wins."""
  saved = item.get(KEY)
  if not readable(saved, REVIEW):
    return _explained(_start(REVIEW, "REVIEWING", started), item, started)
  if item.get("state", "reviewing") == "reviewing" and state_of(saved) in REPORTED:
    return _next_round(saved, now)
  return _explained(saved, item, now)


def _fact_events(state: str, facts: dict) -> list[tuple[str, str]]:
  """The events the run's chat and the owner's Stop raise for a review in `state`, as (type, source)."""
  if state not in LIVE:
    return []
  if facts.get("stopped"):
    return [("STOP", "owner")]
  if facts.get("asking"):
    return [("AGENT_ASKS", "agent")] if state == "REVIEWING" else []
  if state == "STALLED":
    # Already stalled, so a dead chat adds nothing (the engine would refuse CHAT_ENDED, and a refusal
    # adds a history line, so the next read would write). A working chat is the way back.
    return [("CHAT_WORKING", "platform")] if facts.get("working") else []
  events = [("OWNER_ANSWERS", "owner")] if state == "ASKS" else []
  return events + ([("CHAT_ENDED", "platform")] if facts.get("ended") else [])


def advance(vector: dict, facts: dict, now: int) -> dict:
  """Apply what the chat says (a question, an end, Stop, work again), then the clock. Unchanged when nothing moved.

  A refused step is dropped, not kept: the engine logs a refusal in the history, and a vector that grew a
  line would be written back on every read.
  """
  for kind, source in _fact_events(state_of(vector), facts):
    step = engine.apply(TABLES, vector, {"type": kind, "source": source, "inferred": True}, now)
    vector = vector if step["refusal"] else step["vector"]
  # The hour measures silence, not a long review: a chat that is still working has not been quiet for it.
  return vector if facts.get("working") else engine.tick(TABLES, vector, now)["vector"]


def stalled(vector: dict | None) -> bool:
  return bool(vector) and state_of(vector) == "STALLED"


def compact(vector: dict) -> dict:
  """The vector without its history: what a list row needs."""
  return {key: value for key, value in vector.items() if key != "history"}


def verdict_vector(proposal: dict) -> dict | None:
  """A proposal's vector: the saved one while it agrees with the `state` word, else one seeded from it.

  The clock starts at `submitted_at`, else at `proposed_at`; only a proposal that has neither is taken to
  have been armed when this process loaded (the assumption `contribution_review_runs.dispatch_window_closed`
  makes). So a posting verdict an older build armed reads overdue on its first read after an upgrade and
  becomes `unknown`: the safe direction, since an unconfirmed verdict is read back and never resent.
  """
  state = VERDICT_STATES.get(proposal.get("state"))
  if state is None:
    return None
  saved = proposal.get(KEY)
  if readable(saved, VERDICT) and state_of(saved, VERDICT) == state:
    return saved
  since = epoch(proposal.get("submitted_at") or proposal.get("proposed_at"))
  return _start(VERDICT, state, int(reviews.process_loaded_at()) if since is None else since)


def verdict_timeout(proposal: dict, now: int) -> dict | None:
  """The vector once a posting verdict's 5 minutes passed without an answer, or None while they have not."""
  vector = verdict_vector(proposal) if proposal else None
  slot = vector["regions"][VERDICT] if vector else {}
  if slot.get("state") != "POSTING" or slot.get("deadline") is None or now < slot["deadline"]:
    return None
  step = engine.apply(TABLES, vector, {"type": "TIMEOUT", "source": "clock", "region": VERDICT}, now)
  return None if step["refusal"] else step["vector"]
