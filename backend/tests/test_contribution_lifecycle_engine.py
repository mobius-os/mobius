"""The lifecycle interpreter against the shared cases, and the lock that pins every copy.

The Contribute app carries the same tables, engine and case file. Two repositories cannot share
one CI run, so each pins the lock by hash, and the two locks are compared wherever a checkout of
the app is named (LIFECYCLE_APP_LOCK). Never by default: an installed app of another version
must not turn this suite red.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app import contribution_lifecycle_engine as engine
from app import contribution_lifecycle_tables as lifecycle_tables

APP = Path(engine.__file__).parent
FIXTURES = Path(__file__).parent / "fixtures"
CASES_FILE = FIXTURES / "contribution_lifecycle_cases.json"
LOCK = json.loads((FIXTURES / "contribution_lifecycle_lock.json").read_text("utf-8"))
FILE = json.loads(CASES_FILE.read_text("utf-8"))
TABLES = lifecycle_tables.tables()


def _sha256(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def _start_of(start):
  if start is None:
    return None
  vector = engine.new_vector(TABLES, start["states"], start["now"], start["attention"])
  if start["flags"]:
    vector["attention"]["flags"] = list(start["flags"])
  for region, deadline in (start.get("deadlines") or {}).items():
    vector["regions"][region]["deadline"] = deadline
  return vector


def _run(step, vector):
  if step["op"] == "apply":
    return engine.apply(TABLES, vector, step["payload"], step["now"])
  if step["op"] == "tick":
    return engine.tick(TABLES, vector, step["now"])
  if step["op"] == "observe":
    return engine.observe(TABLES, vector, step["payload"], step["now"])
  return engine.adopt(TABLES, vector, step["payload"], step["now"])


def test_the_lock_pins_the_tables_the_cases_and_the_python_files():
  assert _sha256(lifecycle_tables.canonical_json().encode()) == LOCK["tables_sha256"]
  assert _sha256(CASES_FILE.read_bytes()) == LOCK["cases_sha256"]
  assert _sha256((APP / "contribution_lifecycle_tables.py").read_bytes()) == LOCK["tables_py_sha256"]
  assert _sha256((APP / "contribution_lifecycle_engine.py").read_bytes()) == LOCK["engine_py_sha256"]


def test_the_contribute_app_copy_pins_the_same_lock_when_one_is_named():
  sibling = os.environ.get("LIFECYCLE_APP_LOCK")
  if not sibling:
    pytest.skip("LIFECYCLE_APP_LOCK is not set")
  assert json.loads(Path(sibling).read_text("utf-8")) == LOCK


def test_the_tables_obey_their_own_rules():
  assert engine.check_tables(TABLES) == []


@pytest.mark.parametrize("case", FILE["cases"], ids=[case["name"] for case in FILE["cases"]])
def test_every_shared_case_gives_the_stored_answer_step_by_step(case):
  vector = _start_of(case["start"])
  for step in case["steps"]:
    result = _run(step, vector)
    assert result == step["result"]
    if result is None:
      continue
    vector = result["vector"]
    assert engine.waiting_on(TABLES, vector) == step["waiting"]
    if "publication" in vector["regions"]:
      assert engine.status_of(TABLES, vector) == step["status"]


def test_an_event_with_no_row_for_the_state_is_refused_and_changes_nothing():
  for region, state, kind in FILE["refusals"]:
    vector = engine.new_vector(TABLES, {region: state}, 1, False)
    result = engine.apply(TABLES, vector, {"type": kind, "source": "test"}, 2)
    assert (result["refusal"] or {}).get("reason") == "no_transition", (region, state, kind)
    assert result["vector"]["regions"] == vector["regions"]
    assert not engine.allowed(TABLES, vector, kind)


def test_every_publication_and_update_pair_has_the_stored_status_word():
  for publication, update, status in FILE["statuses"]:
    vector = engine.new_vector(TABLES, {"publication": publication, "update": update}, 1)
    assert engine.status_of(TABLES, vector) == status


def test_the_engine_never_changes_what_it_was_given():
  vector = engine.new_vector(TABLES, {"publication": "OPEN", "update": None, "followup": "IDLE"}, 1)
  before = json.dumps(vector, sort_keys=True)
  engine.apply(TABLES, vector, {"type": "MERGED", "source": "github", "data": {"base_is_default": True}}, 2)
  engine.observe(TABLES, vector, {"pr": "CLOSED"}, 2)
  engine.adopt(TABLES, vector, {"status": "merged"}, 2)
  assert json.dumps(vector, sort_keys=True) == before
