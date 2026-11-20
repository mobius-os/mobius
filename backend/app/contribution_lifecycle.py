"""The platform's binding to the contribution lifecycle engine.

The engine (`contribution_lifecycle_engine`) and its tables are pure data and logic shared with
the Contribute app. This module is where a stored contribution record meets them: the facts a
record states (`facts_of`), its vector brought in line with the stored `status` word
(`vector_of`), which mode the owning app chose, and the one function every route goes through
to write a record's `status` (`stamp`).

The mode is the app's own `settings.json` key `lifecycle_writer`:
  shadow (absent, anything else, or unreadable): the route's record is written exactly as it
    always was, and the engine only takes notes: where it would have written another status word,
    or refuses the move, one `lifecycle_disagreement` line is logged.
  engine: the status word and a `lifecycle` field come from the engine. A move that records
    what already happened on GitHub (`proven`) is never lost: if the engine refuses it the
    vector is seeded again from the word the route proved, and the disagreement is logged. A
    move that has not touched GitHub yet (a claim, a restage) is refused to the caller.
Nothing here may fail a route: any trouble in the engine path writes the route's own record.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app import contribution_lifecycle_engine as engine
from app import contribution_lifecycle_tables as lifecycle_tables
from app.contribution_records import MAX_RECORD_BYTES

log = logging.getLogger(__name__)

TABLES = lifecycle_tables.tables()

WRITER_SETTING = "lifecycle_writer"
SETTINGS_FILE = "settings.json"
SHADOW, ENGINE = "shadow", "engine"
SETTINGS_READ_LIMIT = 64 * 1024

TRACKED_TYPES = ("pr", "issue")
UNSETTLED_SUBMIT_STAGES = ("push_pending", "push_ambiguous")
UNCONFIRMED_SUBMIT_CODES = (
  "publication_unconfirmed", "update_unconfirmed", "create_unconfirmed", "push_ambiguous",
)
# Words the engine never writes: the land-stack route's own, kept for records it already made.
LEGACY_WORDS = ("landing",)
FINISHED_PUBLICATION = (
  "MERGED", "MERGED_ELSEWHERE", "ORPHANED", "CLOSED", "CLOSED_QUIET", "SUPERSEDED",
)
HISTORY_KEPT_WHEN_LARGE = 5

# The same reading of a record as the Contribute app's `lifecycle_record.py`: the shared cases
# in tests/fixtures/contribution_lifecycle_record_cases.json pin both.


def tracked(record: object) -> bool:
  """Comment records have no life on GitHub to follow."""
  return isinstance(record, dict) and record.get("type") in TRACKED_TYPES


def _sha(value: object) -> str:
  return str(value if value is not None else "").strip().lower()


def _pushed_current_version(record: dict) -> bool:
  """Restaging keeps `pushed` and the old push sha, so only a push of the version the plan sends
  now can leave this send unconfirmed. An absent sha, or a plan with no head, cannot say otherwise."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  pushed, head = _sha(record.get("last_submit_push_sha")), _sha(plan.get("head_sha"))
  return not pushed or not head or pushed == head


def _unconfirmed(record: dict) -> bool:
  """The last send's outcome is not known: the app's `lastSendUnconfirmed` rule."""
  stage = record.get("last_submit_stage")
  return (
    stage in UNSETTLED_SUBMIT_STAGES
    or record.get("last_submit_error_code") in UNCONFIRMED_SUBMIT_CODES
    or (stage == "pushed" and bool(record.get("last_submit_error")) and _pushed_current_version(record))
  )


def _following(record: dict) -> str:
  """The follow-up grant as the record mirrors it."""
  block = record.get("autopilot")
  if not isinstance(block, dict):
    return "off"
  if not block.get("enabled"):
    return "paused"
  return block.get("state") if block.get("state") in ("responding", "blocked") else "idle"


def facts_of(record: dict) -> dict:
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  return {
    "status": record.get("status"),
    "is_update": plan.get("action") == "pr_update",
    "has_pr": bool(record.get("url") or record.get("number")),
    "unconfirmed": _unconfirmed(record),
    "following": _following(record),
  }


def _adoption_facts(record: dict) -> dict:
  """The facts to adopt a record by. The legacy `landing` word is an open pull request."""
  facts = facts_of(record)
  return {**facts, "status": "open"} if facts["status"] in LEGACY_WORDS else facts


# The regions a contribution record's vector holds; the other regions live on their own host objects.
RECORD_REGIONS = frozenset(name for name, region in TABLES["regions"].items() if region["host"] == "record")


def _whole(value: object) -> bool:
  """Whole seconds. A float that is a whole number counts, as it does in the Contribute app's reader (JavaScript
  cannot tell 1763460000.0 from 1763460000 once the JSON is parsed); a fractional second, a boolean, NaN and
  infinity are not a time."""
  if isinstance(value, bool):
    return False
  return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def slot_readable(name: str, slot: object) -> bool:
  """A region slot the engine can read: a state the tables know, and times it can compare.

  A deadline belongs to a state that has a deadline rule. One on any other state comes from tables
  this copy does not have (the version may not have moved), so the whole vector is read as none.
  """
  states = TABLES["regions"][name]["states"]
  # A state that is not a string (a list or an object in a hand-edited file) is no state: looking it up in a dict would raise.
  if not isinstance(slot, dict) or not isinstance(slot.get("state"), str) or slot["state"] not in states:
    return False
  deadline = slot.get("deadline")
  timed = deadline is None or (_whole(deadline) and bool(states[slot["state"]].get("deadline")))
  return _whole(slot.get("since")) and timed


def _stored_vector(record: dict) -> dict | None:
  """The record's stored vector when the engine can read all of it, else None.

  The field is the record's own to edit: a hand edit or a partial write may leave anything in it. A vector
  the engine cannot read is not repaired; the record is adopted as if it had none, so no route fails on it.
  """
  stored = record.get("lifecycle")
  if not isinstance(stored, dict) or stored.get("v") != TABLES["version"]:
    return None
  regions, attention = stored.get("regions"), stored.get("attention")
  if not isinstance(regions, dict) or set(regions) != RECORD_REGIONS:
    return None
  if not all(slot_readable(name, slot) for name, slot in regions.items()):
    return None
  history = stored.get("history") or []
  history_ok = isinstance(history, list) and all(isinstance(entry, dict) for entry in history)
  attention_ok = attention is None or (isinstance(attention, dict) and isinstance(attention.get("flags"), list))
  return stored if history_ok and attention_ok else None


def adopted_vector(record: dict, now: int | None = None) -> dict | None:
  """The stored vector brought in line with the record's status word, no deadline applied: the
  state a move starts from. None for a record the machine does not track."""
  if not tracked(record):
    return None
  moment = int(time.time()) if now is None else now
  adopted = engine.adopt(TABLES, _stored_vector(record), _adoption_facts(record), moment)
  return adopted["vector"] if adopted else None


def vector_of(record: dict, now: int | None = None) -> dict | None:
  """The record's lifecycle as it stands: the adopted vector with every passed deadline applied.
  Nothing is written; the caller decides whether to store it."""
  moment = int(time.time()) if now is None else now
  vector = adopted_vector(record, moment)
  return engine.tick(TABLES, vector, moment)["vector"] if vector else None


def publication_state(record: dict, now: int | None = None) -> str | None:
  vector = vector_of(record, now)
  return vector["regions"]["publication"]["state"] if vector else None


def pr_finished(record: dict, now: int | None = None) -> bool:
  """Whether the record's pull request itself is finished, so its checkout may go and its
  follow-up may close out. A discarded update leaves its PR public, so it is not finished.
  A record the machine does not track has no PR life of its own: its status decides, as before.
  """
  state = publication_state(record, now)
  if state is None:
    return True
  if state == "ABANDONED":
    return not facts_of(record)["has_pr"]
  return state in FINISHED_PUBLICATION


def github_flags() -> list[str]:
  """The attention flags GitHub raises, in priority order: every flag but an escalation."""
  return [flag for flag in TABLES["attention"]["priority"] if flag != "human_required"]


def attention_cleared_by(event_type: str) -> list[str]:
  """The flags the tables say an event clears: one place names them."""
  rule = next((row for row in TABLES["attention"]["rules"] if row["event"] == event_type), None)
  return list(rule["flags"]) if rule else []


# The mode a route writes in, read from the owning app's own storage.


_WARNED_LARGE: set[Path] = set()


def _settings_of(path: Path) -> object:
  """The app's settings, or None when the file is larger than the read limit.

  The app reads the whole file, so one that is too large for the platform leaves the two in different modes:
  say so once per file, not on every record read.
  """
  with path.open("rb") as handle:
    raw = handle.read(SETTINGS_READ_LIMIT + 1)
  if len(raw) <= SETTINGS_READ_LIMIT:
    return json.loads(raw)
  if path not in _WARNED_LARGE:
    _WARNED_LARGE.add(path)
    log.warning("%s is larger than %d bytes: the platform reads %s as %r (shadow mode) whatever the app runs",
                path, SETTINGS_READ_LIMIT, WRITER_SETTING, SHADOW)
  return None


def writer_mode(record_path: Path | None) -> str:
  """`engine` only when the app's settings.json says so; any trouble reads `shadow`.

  A record lives at <app storage>/contributions/<id>.json, and the setting beside it.
  """
  try:
    if record_path is None or record_path.parent.name != "contributions":
      return SHADOW
    settings = _settings_of(record_path.parent.parent / SETTINGS_FILE)
  except (OSError, ValueError):
    return SHADOW
  return ENGINE if isinstance(settings, dict) and settings.get(WRITER_SETTING) == ENGINE else SHADOW


# The one writer.


@dataclass(frozen=True)
class Move:
  """What a route did to a record, as the engine understands it.

  `events` are the engine events in order. `proven` says the route already saw the move happen
  (on GitHub or in its own journal), so the write must land whatever the engine says. `level`
  is what a GitHub read showed, to be explained by the engine's own edges.
  """

  route: str
  events: tuple[dict, ...] = ()
  proven: bool = True
  level: dict | None = None


class LifecycleRefused(Exception):
  """The engine refused a move that had not touched GitHub yet. `message` is for the owner."""

  def __init__(self, message: str, refusal: dict):
    super().__init__(message)
    self.message = message
    self.refusal = refusal


@dataclass(frozen=True)
class _Outcome:
  vector: dict
  refusal: dict | None
  word: str | None


def _states(vector: dict) -> dict:
  return {name: slot["state"] for name, slot in vector["regions"].items()}


def _run(before: dict, after: dict, move: Move, now: int) -> _Outcome | None:
  """Bring the stored vector in line with the record as it was, then make the move."""
  basis = before if before else after
  adopted = engine.adopt(TABLES, _stored_vector(basis), _adoption_facts(basis), now)
  if adopted is None:
    return None
  vector, refusal = adopted["vector"], None
  for event in move.events:
    step = engine.apply(TABLES, vector, event, now)
    vector, refusal = step["vector"], step["refusal"]
    if refusal:
      break
  if move.level and not refusal:
    vector = engine.observe(TABLES, vector, move.level, now)["vector"]
  vector = engine.tick(TABLES, vector, now)["vector"]
  return _Outcome(vector, refusal, engine.status_of(TABLES, vector, after.get("status")))


def _differs(after: dict, outcome: _Outcome) -> bool:
  if after.get("status") in LEGACY_WORDS:
    return outcome.refusal is not None
  return outcome.refusal is not None or outcome.word != after.get("status")


def _note(mode: str, after: dict, move: Move, outcome: _Outcome) -> None:
  """One structured line per disagreement: the route and its word against the engine's."""
  refusal = outcome.refusal and {key: outcome.refusal.get(key) for key in ("reason", "type")}
  log.warning("lifecycle_disagreement %s", json.dumps({
    "mode": mode, "record": str(after.get("id") or ""), "route": move.route,
    "events": [event["type"] for event in move.events],
    "route_status": after.get("status"), "engine_status": outcome.word,
    "refusal": refusal, "states": _states(outcome.vector),
  }, sort_keys=True))


_CANNOT = {"claim": "be sent", "claim_stack": "be sent", "staged": "be prepared again"}


def refusal_message(route: str, vector: dict) -> str:
  """A plain reason for the owner: what the tables say each region is doing."""
  said = []
  for name in ("publication", "update"):
    state = vector["regions"].get(name, {}).get("state")
    text = TABLES["regions"][name]["states"].get(state, {}).get("text") if state else None
    if text and state != TABLES["regions"][name]["initial"]:
      said.append(text)
  detail = " ".join(said) or "Its lifecycle does not allow that now."
  return f"This contribution cannot {_CANNOT.get(route, 'change')} right now. {detail}"


def _engine_record(after: dict, move: Move, outcome: _Outcome, disagreed: bool, now: int) -> dict:
  if disagreed and not move.proven:
    raise LifecycleRefused(refusal_message(move.route, outcome.vector), outcome.refusal or {})
  # The vector follows the word the route proved when the engine did not reach it.
  adopted = engine.adopt(TABLES, outcome.vector, _adoption_facts(after), now)
  vector = adopted["vector"] if adopted else outcome.vector
  word = after.get("status")
  if word not in LEGACY_WORDS:
    word = engine.status_of(TABLES, vector, word) or word
  return _sized({**after, "status": word, "lifecycle": vector}, after)


def _sized(record: dict, fallback: dict) -> dict:
  """A record the store can still read: a long history is trimmed, then the field dropped."""
  def size(value: dict) -> int:
    return len((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
  if size(record) <= MAX_RECORD_BYTES:
    return record
  trimmed = {**record, "lifecycle": {**record["lifecycle"],
             "history": record["lifecycle"]["history"][-HISTORY_KEPT_WHEN_LARGE:]}}
  if size(trimmed) <= MAX_RECORD_BYTES:
    return trimmed
  log.warning("lifecycle field left out: record %s would be too large", record.get("id"))
  # The route's record was built from the stored one, so it still carries the old vector: leave that out too.
  return {key: value for key, value in fallback.items() if key != "lifecycle"}


def stamp(
  before: dict | None, after: dict, move: Move, record_path: Path | None, now: int | None = None,
) -> dict:
  """The record a route must write: `after`, with its status word and lifecycle as the mode says.

  `before` is the record as it was read (None for a new one), `after` the record the route's own
  logic chose, status word included. In shadow mode the answer is `after` itself. In engine mode
  a move that has not touched GitHub and that the engine refuses raises `LifecycleRefused`.
  """
  if not tracked(after):
    return after
  try:
    moment = int(time.time()) if now is None else now
    mode = writer_mode(record_path)
    outcome = _run(before or {}, after, move, moment)
    if outcome is None:
      return after
    disagreed = _differs(after, outcome)
    if disagreed:
      _note(mode, after, move, outcome)
    if mode == SHADOW:
      return after
    return _engine_record(after, move, outcome, disagreed, moment)
  except LifecycleRefused:
    raise
  except Exception:
    log.warning("lifecycle writer failed for %s; wrote the route's own record", after.get("id"), exc_info=True)
    return after
