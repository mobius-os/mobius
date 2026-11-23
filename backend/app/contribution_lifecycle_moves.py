"""The engine events behind each route's move on a contribution record.

A route decides a status word by its own logic; here is what that move is called in the
lifecycle tables. Each builder returns a `Move` for `contribution_lifecycle.stamp`. Builders read
the record as it was, never the engine's answer: the move says what the route did, and the
engine says whether it fits.
"""

from __future__ import annotations

from app import contribution_lifecycle as lifecycle
from app import contribution_lifecycle_engine as engine
from app.contribution_lifecycle import Move, facts_of

UPDATE_IN_FLIGHT = ("SENDING_UPDATE", "UPDATE_UNCERTAIN", "UPDATE_NOT_CONFIRMED")
UPDATE_UNSURE = ("UPDATE_UNCERTAIN", "UPDATE_NOT_CONFIRMED")
PUBLICATION_UNSURE = ("UNCERTAIN", "NOT_CONFIRMED")
PR_LEVEL = {"merged": "MERGED", "closed": "CLOSED"}


def _event(kind: str, source: str = "platform", **data: object) -> dict:
  return {"type": kind, "source": source, **({"data": data} if data else {})}


def _states(record: dict) -> dict:
  vector = lifecycle.adopted_vector(record)
  if vector is None:
    return {}
  return {name: slot["state"] for name, slot in vector["regions"].items()}


def _update_sent(record: dict) -> bool:
  return _states(record).get("update") in UPDATE_IN_FLIGHT


def claim(before: dict, route: str = "claim") -> Move:
  """Send was pressed: the record is claimed. Nothing is public yet, so a refusal is the owner's."""
  kind = "SEND_UPDATE_CONFIRMED" if facts_of(before)["is_update"] else "SEND_CONFIRMED"
  return Move(route, (_event(kind, "owner"),), proven=False)


def resume_restored(before: dict) -> Move:
  """A signed receipt proved an earlier claim: the word goes back to submitting. When the engine
  already knows the send is uncertain there is no event to raise; the vector follows the word."""
  states = _states(before)
  kind = "SEND_UPDATE_CONFIRMED" if states.get("update") == "PREPARED_UPDATE" else "SEND_CONFIRMED"
  vector = lifecycle.adopted_vector(before)
  fits = vector is not None and engine.allowed(lifecycle.TABLES, vector, kind)
  return Move("resume_restored", (_event(kind, "owner"),) if fits else ())


def _refusal(before: dict) -> dict:
  return _event("UPDATE_REFUSED" if _update_sent(before) else "SUBMIT_REFUSED")


def resume_released(before: dict) -> Move:
  """A claim with no receipt behind it was returned to prepared: nothing had been sent."""
  return Move("resume_released", (_refusal(before),))


def send_failed(before: dict, after: dict) -> Move:
  """A send ended in an error: refused when nothing was pushed, lost when the answer is unknown."""
  lost = facts_of(after)["unconfirmed"]
  kind = ("UPDATE_" if _update_sent(before) else "SUBMIT_") + ("LOST" if lost else "REFUSED")
  return Move("send_failed", (_event(kind),))


def update_unconfirmed(before: dict) -> Move:
  """The answer to a successor update was lost; the claim stays submitting."""
  return Move("update_unconfirmed", (_event("UPDATE_LOST" if _update_sent(before) else "SUBMIT_LOST"),))


def pr_created(after: dict) -> Move:
  return Move("pr_created", (_event("SUBMIT_OK", is_draft=after.get("status") == "draft"),))


def pr_updated() -> Move:
  return Move("pr_updated", (_event("UPDATE_OK"),))


def observed(before: dict, after: dict, base_is_default: bool | None) -> Move:
  """GitHub was read and the exact outcome found: the lost answer, the head, then the level."""
  states = _states(before)
  events = []
  if states.get("publication") == "SENDING":
    events.append(_event("SUBMIT_LOST"))
  if states.get("update") == "SENDING_UPDATE":
    events.append(_event("UPDATE_LOST"))
  if states.get("update") in UPDATE_IN_FLIGHT:
    events.append(_event("OBSERVED_HEAD", "github"))
  status = after.get("status")
  level = {
    "pr": PR_LEVEL.get(status, "OPEN"), "is_draft": status == "draft", "queued": None,
    "base_is_default": base_is_default,
  }
  return Move("observed", tuple(events), level=level)


def staged(before: dict | None, after: dict, in_place: bool) -> Move:
  """The agent prepared the record again. An update of a public PR is a STAGE_UPDATE, and a
  restage that dropped a lost send's journal first settles that send as not sent."""
  events = []
  states = _states(before) if before else {}
  unsure = states.get("update") in UPDATE_UNSURE or states.get("publication") in PUBLICATION_UNSURE
  if unsure and not facts_of(after)["unconfirmed"]:
    events.append(_refusal(before))
  if facts_of(after)["is_update"] and not in_place:
    events.append(_event("STAGE_UPDATE", "agent"))
  return Move("staged", tuple(events), proven=False)


def ready_settled(before: dict, after: dict) -> Move:
  came, went = before.get("status"), after.get("status")
  return Move("ready_settled", (_event("MARKED_READY", "owner"),) if (came, went) == ("draft", "open") else ())


def ready_released(before: dict, after: dict) -> Move:
  """GitHub still shows a draft: a record that said open goes back to draft."""
  back = (before.get("status"), after.get("status")) == ("open", "draft")
  return Move("ready_released", (_event("MARKED_DRAFT", "github"),) if back else ())


def land_started() -> Move:
  """The legacy `landing` word is an open PR to the engine: no event, the word is the route's."""
  return Move("land_started")


def land_released() -> Move:
  return Move("land_released")


def land_settled() -> Move:
  """A stack lands by fast-forward onto the branch its first PR targets.

  Nothing that builds this move has read the repository's default branch (the landing route checked it before
  it pushed; the journal it settles from names only a branch), so the base is not claimed to be it. The tables
  read an unknown base as the default, and the next GitHub read says if it was not."""
  return Move("land_settled", (_event("MERGED", "github", base_is_default=None),))
