"""Pure interpreter for the contribution lifecycle tables.

Nothing here reads a clock, a file or the network, and nothing mutates its arguments: every
function takes the tables, a vector and the time (whole seconds since the epoch) and returns
what should be stored and done. The caller performs the effects and writes the vector with
compare-and-swap. The Contribute app (`lifecycle_engine.py`, and its JavaScript twin
`lifecycle-engine.js`) and the platform (`contribution_lifecycle_engine.py`) carry the same
interpreter, and one shared case file holds the answers all of them must give.

A vector is what one host object stores:
  {"v": 1, "regions": {name: {"state", "since", "deadline"}}, "attention": {"flags": [...]},
   "history": [...]}
`attention` exists only on a contribution record. An event is
  {"type", "source", "region" (optional: deliver to this region only), "data" (optional),
   "inferred" (optional: derived from a level, not seen as an edge)}.
Every function that changes state returns {"vector", "effects", "refusal", "log"}.
"""
from __future__ import annotations

import copy

ATTENTION = "attention"
DELIVERY_LIMIT = 24  # a raised event raises nothing further in the tables; this only bounds a bad table
TICK_LIMIT = 8  # the longest deadline chain in the tables is two


def _same(expected: object, actual: object) -> bool:
  return type(expected) is type(actual) and expected == actual


def _matches(when: dict, data: dict) -> bool:
  """Every named field must equal the expected value, or one of a list. Missing reads as None."""
  for field, expected in when.items():
    options = expected if isinstance(expected, list) else [expected]
    if not any(_same(option, data.get(field)) for option in options):
      return False
  return True


def _states(vector: dict) -> dict:
  return {name: slot["state"] for name, slot in vector["regions"].items()}


def _find_transition(region: dict, state: str, event: dict) -> dict | None:
  data = event.get("data") or {}
  for row in region["transitions"]:
    if row["from"] == state and row["event"] == event["type"] and _matches(row["when"], data):
      return row
  return None


def _target(tables: dict, row: dict, event: dict) -> str:
  rule = tables["targets"].get(row["to"])
  if rule is None:
    return row["to"]
  chosen = (event.get("data") or {}).get(rule["field"]) is True
  return rule["if_true"] if chosen else rule["otherwise"]


def _deadline(region: dict, state: str, now: int) -> int | None:
  rule = region["states"][state].get("deadline")
  return now + rule["seconds"] if rule else None


def _entry(event: dict, now: int, **fields: object) -> dict:
  entry = {"at": now, "type": event["type"], "source": event.get("source") or "unknown", **fields}
  if event.get("inferred"):
    entry["inferred"] = True
  return entry


def _take(tables: dict, work: dict, name: str, row: dict, event: dict, now: int, run: dict) -> list:
  """Move one region along one row. Returns the events the row raises for the other regions."""
  region, slot = tables["regions"][name], work["regions"][name]
  target = _target(tables, row, event)
  run["log"].append(_entry(event, now, region=name, to=target, **{"from": slot["state"]}))
  if target != slot["state"]:
    slot["since"] = now
  slot["state"] = target
  slot["deadline"] = _deadline(region, target, now)  # any move starts the state's clock again
  run["effects"].extend({"region": name, "effect": effect, "event": event["type"]} for effect in row["effects"])
  raised = {"source": "engine", "data": event.get("data") or {}, "inferred": bool(event.get("inferred"))}
  return [{"type": kind, **raised} for kind in row["raises"]]


def _attention_flags(rule: dict, flags: list, priority: list) -> list:
  if rule["action"] == "clear_all":
    return []
  if rule["action"] == "raise":
    wanted = set(flags) | set(rule["flags"])
  elif rule["action"] == "clear":
    wanted = set(flags) - set(rule["flags"])
  else:
    wanted = set(flags)
  return [flag for flag in priority if flag in wanted]


def _deliver_attention(tables: dict, work: dict, event: dict, now: int, run: dict) -> None:
  attention = work.get(ATTENTION)
  if attention is None or event.get("region") not in (None, ATTENTION):
    return
  data = event.get("data") or {}
  rule = next((row for row in tables["attention"]["rules"]
               if row["event"] == event["type"] and _matches(row["when"], data)), None)
  if rule is None:
    return
  run["matched"] = True
  flags = _attention_flags(rule, attention["flags"], tables["attention"]["priority"])
  if flags != attention["flags"] or rule["logged"]:
    run["log"].append(_entry(event, now, region=ATTENTION, action=rule["action"], flags=flags))
  attention["flags"] = flags


def _deliver(tables: dict, work: dict, event: dict, now: int, run: dict) -> list:
  """Offer one event to every region of the vector, then to attention. Returns raised events."""
  raised = []
  for name in sorted(work["regions"]):
    if event.get("region") not in (None, name):
      continue
    row = _find_transition(tables["regions"][name], work["regions"][name]["state"], event)
    if row is not None:
      run["matched"] = True
      raised.extend(_take(tables, work, name, row, event, now, run))
  _deliver_attention(tables, work, event, now, run)
  return raised


def _known_event(tables: dict, kind: str) -> bool:
  in_regions = any(row["event"] == kind for region in tables["regions"].values() for row in region["transitions"])
  return in_regions or any(row["event"] == kind for row in tables["attention"]["rules"])


def _refusal(tables: dict, vector: dict, event: dict) -> dict:
  reason = "no_transition" if _known_event(tables, event["type"]) else "unknown_event"
  return {"reason": reason, "type": event["type"], "states": _states(vector)}


def _store_log(tables: dict, work: dict, log: list) -> None:
  work["history"] = (work.get("history") or []) + log
  del work["history"][:-tables["history_limit"]]


def _refused(tables: dict, vector: dict, event: dict, now: int) -> dict:
  """An event no table accepts changes nothing. It is logged once, not once per repeat."""
  refusal = _refusal(tables, vector, event)
  entry = _entry(event, now, refused=refusal["reason"])
  last = (vector.get("history") or [{}])[-1]
  repeat = last.get("refused") == entry["refused"] and last.get("type") == entry["type"]
  work = copy.deepcopy(vector)
  if not repeat:
    _store_log(tables, work, [entry])
  return {"vector": work, "effects": [], "refusal": refusal, "log": [entry]}


def new_vector(tables: dict, states: dict, now: int, attention: bool = True) -> dict:
  """A fresh vector with the named regions in the given states (None means the initial one)."""
  regions = {}
  for name, state in states.items():
    region = tables["regions"][name]
    chosen = state or region["initial"]
    regions[name] = {"state": chosen, "since": now, "deadline": _deadline(region, chosen, now)}
  vector = {"v": tables["version"], "regions": regions, "history": []}
  if attention:
    vector[ATTENTION] = {"flags": []}
  return vector


def apply(tables: dict, vector: dict, event: dict, now: int) -> dict:
  """Deliver one event. A refused event leaves the states as they were."""
  work = copy.deepcopy(vector)
  run = {"effects": [], "log": [], "matched": False}
  queue, delivered = [event], 0
  while queue and delivered < DELIVERY_LIMIT:
    queue.extend(_deliver(tables, work, queue.pop(0), now, run))
    delivered += 1
  if not run["matched"]:
    return _refused(tables, vector, event, now)
  _store_log(tables, work, run["log"])
  return {"vector": work, "effects": run["effects"], "refusal": None, "log": run["log"]}


def _merge(total: dict, step: dict) -> dict:
  return {"vector": step["vector"], "effects": total["effects"] + step["effects"],
          "refusal": total["refusal"], "log": total["log"] + step["log"]}


def _unchanged(vector: dict) -> dict:
  return {"vector": vector, "effects": [], "refusal": None, "log": []}


def _due(tables: dict, vector: dict, now: int) -> dict | None:
  for name in sorted(vector["regions"]):
    slot = vector["regions"][name]
    rule = tables["regions"][name]["states"][slot["state"]].get("deadline")
    # A stored deadline on a state that has no deadline rule (a vector from other tables) is not due.
    if rule is not None and slot.get("deadline") is not None and now >= slot["deadline"]:
      return {"type": rule["event"], "region": name, "source": "clock"}
  return None


def tick(tables: dict, vector: dict, now: int) -> dict:
  """Raise the deadline event of every state whose time has passed."""
  total = _unchanged(vector)
  for _ in range(TICK_LIMIT):
    event = _due(tables, total["vector"], now)
    if event is None:
      break
    step = apply(tables, total["vector"], event, now)
    if step["refusal"]:
      break
    total = _merge(total, step)
  return total


def _apply_inferred(tables: dict, vector: dict, events: list, now: int) -> dict:
  """Try each derived event in turn. One that does not fit is a wrong guess and is dropped."""
  total = _unchanged(vector)
  for event in events:
    step = apply(tables, total["vector"], {**event, "inferred": True}, now)
    if not step["refusal"]:
      total = _merge(total, step)
  return total


def observe(tables: dict, vector: dict, level: dict, now: int) -> dict:
  """Turn what a GitHub read shows (a level) into the edges that explain it."""
  state = vector["regions"]["publication"]["state"]
  row = next((row for row in tables["level_edges"]
              if state in row["where"] and _matches(row["level"], level)), None)
  if row is None:
    return _unchanged(vector)
  events = [{"type": kind, "source": "github", "data": level} for kind in row["events"]]
  return _apply_inferred(tables, vector, events, now)


def _where(where: dict, vector: dict) -> bool:
  return _matches(where, _states(vector))


def status_of(tables: dict, vector: dict, stored: str | None = None) -> str | None:
  """The `status` word for these regions. The stored word is kept when the row accepts it."""
  row = next((row for row in tables["status"] if _where(row["where"], vector)), None)
  if row is None:
    return None
  return stored if stored in row["also"] else row["status"]


def _seed_row(tables: dict, facts: dict) -> dict | None:
  return next((row for row in tables["seed"] if _matches(row["facts"], facts)), None)


def _reseed(tables: dict, vector: dict | None, facts: dict, now: int) -> dict | None:
  row = _seed_row(tables, facts)
  if row is None:
    return None
  following = tables["seed_followup"].get(facts.get("following") or "off", "OFF")
  fresh = new_vector(tables, {"publication": None, "update": None, "followup": following, **row["states"]}, now)
  event = {"type": "SEEDED" if vector is None else "RESEEDED", "source": "status", "inferred": True}
  entry = _entry(event, now, to=_states(fresh), **{"from": _states(vector) if vector else None})
  if vector is not None:
    fresh[ATTENTION] = copy.deepcopy(vector.get(ATTENTION) or {"flags": []})
    fresh["history"] = copy.deepcopy(vector.get("history") or [])
  _store_log(tables, fresh, [entry])
  seeded = {"vector": fresh, "effects": [], "refusal": None, "log": [entry]}
  return _merge(seeded, _apply_inferred(tables, fresh, [{"type": kind, "source": "status"} for kind in row["events"]], now))


def _follow(tables: dict, result: dict, facts: dict, now: int) -> dict:
  """Keep the follow-up region in step with the grant the record mirrors.

  The grant is the platform's row and its writers do not know the vector, so the mirror is the
  truth. A finished PR stays closed out whatever the mirror says.
  """
  slot = result["vector"]["regions"].get("followup")
  wanted = tables["seed_followup"].get(facts.get("following"))
  if slot is None or wanted is None or slot["state"] in (wanted, "CLOSED_OUT"):
    return result
  work = copy.deepcopy(result["vector"])
  event = {"type": "RESEEDED", "source": "status", "inferred": True}
  entry = _entry(event, now, region="followup", to=wanted, **{"from": slot["state"]})
  region = tables["regions"]["followup"]
  work["regions"]["followup"] = {"state": wanted, "since": now, "deadline": _deadline(region, wanted, now)}
  _store_log(tables, work, [entry])
  return {**result, "vector": work, "log": result["log"] + [entry]}


def _adopt_status(tables: dict, vector: dict, facts: dict, now: int) -> dict | None:
  status = facts.get("status")
  row = next((row for row in tables["status_edges"]
              if _where(row["where"], vector) and _matches(row["facts"], facts)), None)
  if row is not None:
    events = [{"type": e["type"], "region": e["region"], "data": e["data"], "source": "status"} for e in row["events"]]
    result = _apply_inferred(tables, vector, events, now)
    explained = result["log"] and status_of(tables, result["vector"], status) == status
    # A correcting row overrules the stored word: with events it moves the regions, with none it
    # says the regions already stand right, so the same word is not explained again on every read.
    if explained or (row["corrects"] and (result["log"] or not events)):
      return result
  if status_of(tables, vector, status) == status:
    return _unchanged(vector)
  return _reseed(tables, vector, facts, now)


def adopt(tables: dict, vector: dict | None, facts: dict, now: int) -> dict | None:
  """Bring a record's vector in line with the status word another writer stored.

  Returns None when the facts describe nothing this machine tracks (the caller stores no vector).
  """
  result = _reseed(tables, None, facts, now) if vector is None else _adopt_status(tables, vector, facts, now)
  return _follow(tables, result, facts, now) if result else None


def waiting_on(tables: dict, vector: dict) -> list:
  """Every region that owes a move, with who it waits on and since when."""
  waits = []
  for name in sorted(vector["regions"]):
    slot = vector["regions"][name]
    who = tables["regions"][name]["states"][slot["state"]]["waits"]
    if who != "none":
      waits.append({"region": name, "state": slot["state"], "waits": who,
                    "since": slot["since"], "deadline": slot.get("deadline")})
  return waits


def shown_attention(vector: dict) -> str | None:
  """The one flag shown; the others wait behind it. Flags are stored in priority order."""
  flags = (vector.get(ATTENTION) or {}).get("flags") or []
  return flags[0] if flags else None


def allowed(tables: dict, vector: dict, kind: str, data: dict | None = None) -> bool:
  """Whether the tables accept this event now: the rule a button is offered by."""
  event = {"type": kind, "data": data or {}}
  return any(_find_transition(tables["regions"][name], slot["state"], event) is not None
             for name, slot in vector["regions"].items())


def _check_region(tables: dict, name: str, region: dict) -> list:
  problems, states = [], region["states"]
  targets = {region["initial"]}
  leaves = {state: 0 for state in states}
  for row in region["transitions"]:
    rule = tables["targets"].get(row["to"])
    reached = [rule["if_true"], rule["otherwise"]] if rule else [row["to"]]
    targets.update(reached)
    for state in [row["from"], *reached]:
      if state not in states:
        problems.append(f"{name}: transition {row['from']} {row['event']} names undeclared {state}")
    if row["from"] in leaves:
      leaves[row["from"]] += 1
  for state, meta in states.items():
    problems.extend(_check_state(name, region, state, meta, leaves[state], state in targets))
  return problems


def _check_state(name: str, region: dict, state: str, meta: dict, exits: int, reachable: bool) -> list:
  problems = []
  rule = meta.get("deadline")
  if meta["waits"] in ("github", "platform", "agent") and not rule:
    problems.append(f"{name}.{state}: waits on {meta['waits']} but has no deadline")
  if rule and not any(row["from"] == state and row["event"] == rule["event"] and row["to"] == rule["to"]
                      for row in region["transitions"]):
    problems.append(f"{name}.{state}: deadline {rule['event']} to {rule['to']} has no transition")
  if not meta["final"] and exits == 0:
    problems.append(f"{name}.{state}: not final and no way out")
  if not reachable:
    problems.append(f"{name}.{state}: unreachable")
  return problems


def _check_bridge(tables: dict) -> list:
  """The rows that tie the regions to the stored record must name real states, events and flags."""
  problems = []
  regions, priority = tables["regions"], tables["attention"]["priority"]
  for state in regions["publication"]["states"]:
    probe = {"regions": {"publication": {"state": state}, "update": {"state": "NONE"}}}
    if status_of(tables, probe) is None:
      problems.append(f"status: publication {state} has no status word")
  for row in tables["attention"]["rules"]:
    problems.extend(f"attention: unknown flag {flag}" for flag in row["flags"] if flag not in priority)
  for row in tables["status_edges"]:
    problems.extend(f"status_edges: unknown event {e['type']}" for e in row["events"] if not _known_event(tables, e["type"]))
  for row in tables["level_edges"]:
    problems.extend(f"level_edges: unknown event {kind}" for kind in row["events"] if not _known_event(tables, kind))
  for row in tables["seed"]:
    problems.extend(f"seed: unknown state {name}.{state}" for name, state in row["states"].items()
                    if state not in regions[name]["states"])
    problems.extend(f"seed: unknown event {kind}" for kind in row["events"] if not _known_event(tables, kind))
  return problems


def check_tables(tables: dict) -> list:
  """The rules the tables must obey. An empty list means they hold."""
  problems = []
  for name, region in tables["regions"].items():
    problems.extend(_check_region(tables, name, region))
  return problems + _check_bridge(tables)
