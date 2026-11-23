"""The platform's binding to the lifecycle engine: what a record states, the mode, the one writer.

The record reader is pinned by the same cases the Contribute app's reader is (a copy of its
test/lifecycle-record.cases.json), so a record means the same facts on both sides; the same goes for
what counts as a stored vector it can read (test/lifecycle-unreadable.cases.json).
"""
import json
import logging
from pathlib import Path

import pytest

from app import contribution_lifecycle as lifecycle
from app import contribution_lifecycle_engine as engine
from app import contribution_lifecycle_moves as moves
from app import contribution_review_lifecycle as review_lifecycle
from app.contribution_lifecycle import LifecycleRefused, Move

CASES = json.loads(
  (Path(__file__).parent / "fixtures" / "contribution_lifecycle_record_cases.json").read_text("utf-8"),
)["cases"]
UNREADABLE = json.loads(
  (Path(__file__).parent / "fixtures" / "contribution_lifecycle_unreadable_cases.json").read_text("utf-8"),
)["cases"]
NOW = 1_763_460_000
URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def storage(tmp_path):
  """An app's storage folder: its records live in contributions/, its settings beside them."""
  root = tmp_path / "apps" / "7"
  (root / "contributions").mkdir(parents=True)

  class Storage:
    path = root / "contributions" / "r1.json"

    @staticmethod
    def setting(value: object) -> None:
      (root / "settings.json").write_text(json.dumps(value))

  return Storage


def _states(record: dict) -> dict:
  return {name: slot["state"] for name, slot in record["lifecycle"]["regions"].items()}


# What a record states


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_record_states_the_facts_the_apps_reader_states(case):
  assert lifecycle.facts_of(case["record"]) == case["facts"]


def test_only_pull_requests_and_issues_are_tracked():
  assert lifecycle.tracked({"type": "pr"}) and lifecycle.tracked({"type": "issue"})
  assert not lifecycle.tracked({"type": "comment"})
  assert not lifecycle.tracked({"id": "no-type"}) and not lifecycle.tracked(None)
  assert lifecycle.vector_of({"type": "comment", "status": "commented"}, NOW) is None


def test_a_record_without_a_vector_is_seeded_from_its_status_word():
  record = {"type": "pr", "status": "draft", "number": 7}
  vector = lifecycle.vector_of(record, NOW)
  assert vector["regions"]["publication"]["state"] == "DRAFT"
  assert "lifecycle" not in record  # reading never writes


def test_a_stored_vector_is_brought_in_line_with_a_status_word_another_writer_stored():
  stored = engine.new_vector(lifecycle.TABLES, {"publication": "OPEN", "update": None, "followup": None}, NOW - 10)
  record = {"type": "pr", "status": "merged", "number": 7, "lifecycle": stored}
  vector = lifecycle.vector_of(record, NOW)
  assert vector["regions"]["publication"]["state"] == "MERGED"
  assert record["lifecycle"] == stored


def test_the_legacy_landing_word_reads_as_an_open_pull_request_without_churning_history():
  record = {"type": "pr", "status": "landing", "number": 7}
  assert lifecycle.vector_of(record, NOW)["regions"]["publication"]["state"] == "OPEN"
  stored = lifecycle.vector_of(record, NOW)
  again = lifecycle.vector_of({**record, "lifecycle": stored}, NOW + 5)
  assert [entry["type"] for entry in again["history"]] == ["SEEDED"]


def test_deadlines_are_applied_when_a_record_is_read():
  record = {"type": "pr", "status": "submitting", "submit_started_at": "x"}
  later = lifecycle.vector_of(record, NOW)
  assert lifecycle.vector_of({**record, "lifecycle": later}, NOW + 301)["regions"]["publication"]["state"] == "UNCERTAIN"


@pytest.mark.parametrize("record,finished", [
  ({"type": "pr", "status": "merged", "number": 7}, True),
  ({"type": "pr", "status": "closed", "number": 7}, True),
  ({"type": "pr", "status": "superseded"}, True),
  ({"type": "pr", "status": "abandoned"}, True),
  ({"type": "pr", "status": "abandoned", "number": 7, "url": URL, "plan": {"action": "pr_update"}}, False),
  ({"type": "pr", "status": "abandoned", "number": 7, "url": URL, "plan": {"action": "pr"}}, False),  # ABANDONED, PR still public
  ({"type": "pr", "status": "open", "number": 7}, False),
  ({"type": "pr", "status": "prepared"}, False),
  ({"type": "pr", "status": "submitting"}, False),
  ({"type": "issue_comment", "status": "abandoned"}, True),
  ({"status": "abandoned", "plan": {}}, True),
])
def test_a_pull_request_is_finished_only_when_nothing_of_it_is_public_any_more(record, finished):
  assert lifecycle.pr_finished(record, NOW) is finished


def test_the_flags_a_restage_clears_are_read_from_the_tables():
  assert lifecycle.attention_cleared_by("STAGE_UPDATE") == [
    "merge_conflict", "checks_failed", "changes_requested", "github_activity", "outside_activity",
  ]
  assert "human_required" not in lifecycle.attention_cleared_by("STAGE_UPDATE")
  assert lifecycle.attention_cleared_by("NO_SUCH_EVENT") == []


# The mode


@pytest.mark.parametrize("settings,mode", [
  (None, "shadow"), ({}, "shadow"), ({"lifecycle_writer": "shadow"}, "shadow"),
  ({"lifecycle_writer": "engine"}, "engine"), ({"lifecycle_writer": "Engine"}, "shadow"),
  ({"lifecycle_writer": True}, "shadow"), (["engine"], "shadow"), ("engine", "shadow"),
  ({"autopilot_default": True, "lifecycle_writer": "engine"}, "engine"),
])
def test_the_mode_is_the_apps_lifecycle_writer_setting(storage, settings, mode):
  if settings is not None:
    storage.setting(settings)
  assert lifecycle.writer_mode(storage.path) == mode


def test_an_unreadable_settings_file_reads_as_shadow(storage):
  (storage.path.parent.parent / "settings.json").write_bytes(b"{not json")
  assert lifecycle.writer_mode(storage.path) == "shadow"
  (storage.path.parent.parent / "settings.json").write_bytes(b"\xff\xfe\x00")
  assert lifecycle.writer_mode(storage.path) == "shadow"
  (storage.path.parent.parent / "settings.json").unlink()
  (storage.path.parent.parent / "settings.json").mkdir()
  assert lifecycle.writer_mode(storage.path) == "shadow"


def test_a_settings_file_over_the_read_limit_reads_as_shadow_and_says_so_once(storage, caplog, monkeypatch):
  """The platform reads at most 64 KiB; the app reads the whole file, so a larger one is a mode mismatch to log."""
  monkeypatch.setattr(lifecycle, "_WARNED_LARGE", set())
  padded = {"lifecycle_writer": "engine", "pad": "x" * lifecycle.SETTINGS_READ_LIMIT}
  storage.setting(padded)
  with caplog.at_level(logging.WARNING):
    assert lifecycle.writer_mode(storage.path) == "shadow"
    assert lifecycle.writer_mode(storage.path) == "shadow"
  warnings = [record for record in caplog.records if "larger than" in record.getMessage()]
  assert len(warnings) == 1 and "shadow" in warnings[0].getMessage()


def test_a_settings_file_at_the_read_limit_is_still_read_whole(storage, caplog, monkeypatch):
  monkeypatch.setattr(lifecycle, "_WARNED_LARGE", set())
  body = json.dumps({"lifecycle_writer": "engine", "pad": ""})
  room = lifecycle.SETTINGS_READ_LIMIT - len(body)
  storage.setting({"lifecycle_writer": "engine", "pad": "x" * room})
  assert (storage.path.parent.parent / "settings.json").stat().st_size == lifecycle.SETTINGS_READ_LIMIT
  with caplog.at_level(logging.WARNING):
    assert lifecycle.writer_mode(storage.path) == "engine"
  assert not caplog.records


def test_a_path_outside_an_apps_contributions_folder_reads_as_shadow(tmp_path):
  (tmp_path / "settings.json").write_text(json.dumps({"lifecycle_writer": "engine"}))
  assert lifecycle.writer_mode(tmp_path / "loose.json") == "shadow"
  assert lifecycle.writer_mode(None) == "shadow"


# The one writer, in shadow mode


def _claim_pair(**fields):
  before = {"id": "r1", "type": "pr", "status": "prepared", "plan": {"action": "pr"}, **fields}
  return before, {**before, "status": "submitting"}


def test_shadow_mode_returns_the_routes_own_record_untouched(storage, caplog):
  before, after = _claim_pair()
  with caplog.at_level(logging.INFO):
    assert lifecycle.stamp(before, after, moves.claim(before), storage.path, NOW) is after
  assert "lifecycle" not in after and after["status"] == "submitting"
  assert not caplog.records  # the engine agreed: nothing to say


def test_shadow_mode_logs_one_structured_line_where_the_engine_disagrees(storage, caplog):
  before, after = _claim_pair(last_submit_stage="push_pending")
  with caplog.at_level(logging.WARNING):
    result = lifecycle.stamp(before, after, moves.claim(before), storage.path, NOW)
  assert result is after and "lifecycle" not in result
  [line] = [record.getMessage() for record in caplog.records]
  prefix, _, body = line.partition(" ")
  assert prefix == "lifecycle_disagreement"
  assert json.loads(body) == {
    "mode": "shadow", "record": "r1", "route": "claim", "events": ["SEND_CONFIRMED"],
    "route_status": "submitting", "engine_status": "submitting",
    "refusal": {"reason": "no_transition", "type": "SEND_CONFIRMED"},
    "states": {"followup": "OFF", "publication": "UNCERTAIN", "update": "NONE"},
  }


def test_shadow_mode_logs_where_the_engine_would_write_another_word(storage, caplog):
  before = {"id": "r1", "type": "pr", "status": "submitting", "plan": {"action": "pr"}}
  after = {**before, "status": "open", "url": URL, "number": 7}
  with caplog.at_level(logging.WARNING):
    lifecycle.stamp(before, after, Move("pr_created", (moves._event("SUBMIT_OK", is_draft=True),)), storage.path, NOW)
  body = json.loads(caplog.records[0].getMessage().partition(" ")[2])
  assert (body["route_status"], body["engine_status"], body["refusal"]) == ("open", "draft", None)


def test_a_record_the_machine_does_not_track_is_returned_as_it_is(storage):
  before = {"id": "c1", "type": "comment", "status": "commented"}
  assert lifecycle.stamp(before, {**before, "status": "open"}, Move("x"), storage.path, NOW)["status"] == "open"


def test_a_failing_engine_never_fails_the_route(storage, monkeypatch, caplog):
  storage.setting({"lifecycle_writer": "engine"})
  def explode(*_args):
    raise RuntimeError("engine bug")
  monkeypatch.setattr(engine, "adopt", explode)
  before, after = _claim_pair()
  with caplog.at_level(logging.WARNING):
    assert lifecycle.stamp(before, after, moves.claim(before), storage.path, NOW) is after
  assert "wrote the route's own record" in caplog.text


# The one writer, in engine mode


@pytest.fixture
def engine_mode(storage):
  storage.setting({"lifecycle_writer": "engine"})
  return storage


def test_engine_mode_writes_the_status_word_and_the_vector(engine_mode):
  before, after = _claim_pair()
  record = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert record["status"] == "submitting"
  assert _states(record)["publication"] == "SENDING"
  assert record["lifecycle"]["history"][-1]["type"] == "SEND_CONFIRMED"
  assert {key: value for key, value in record.items() if key != "lifecycle"} == after


def test_engine_mode_keeps_every_field_the_route_wrote_and_the_old_vector_history(engine_mode):
  before, after = _claim_pair(unknown_field={"kept": [1, 2]}, autopilot={"enabled": True, "state": "idle"})
  first = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert first["unknown_field"] == {"kept": [1, 2]}
  assert _states(first)["followup"] == "IDLE"
  failed = {**first, "status": "prepared", "last_submit_error": "no"}
  second = lifecycle.stamp(first, failed, moves.send_failed(first, failed), engine_mode.path, NOW + 5)
  assert second["status"] == "prepared" and _states(second)["publication"] == "PREPARED"
  assert [entry["type"] for entry in second["lifecycle"]["history"]][-2:] == ["SEND_CONFIRMED", "SUBMIT_REFUSED"]


def test_engine_mode_refuses_a_claim_the_engine_does_not_accept_and_says_why(engine_mode):
  before, after = _claim_pair(last_submit_stage="push_pending")
  with pytest.raises(LifecycleRefused) as raised:
    lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert raised.value.message == (
    "This contribution cannot be sent right now. The answer was lost; reading GitHub to find out."
  )
  assert raised.value.refusal["reason"] == "no_transition"


def test_engine_mode_never_loses_a_write_that_records_what_already_happened(engine_mode, caplog):
  """The route proved the PR is open; the engine thought nothing was in flight."""
  before = {"id": "r1", "type": "pr", "status": "prepared", "plan": {"action": "pr"}}
  after = {**before, "status": "open", "url": URL, "number": 7}
  with caplog.at_level(logging.WARNING):
    record = lifecycle.stamp(before, after, moves.pr_created(after), engine_mode.path, NOW)
  assert record["status"] == "open"
  assert _states(record)["publication"] == "OPEN"  # reseeded from the word the route proved
  assert "lifecycle_disagreement" in caplog.text and '"mode": "engine"' in caplog.text


def test_engine_mode_keeps_the_legacy_landing_word(engine_mode):
  before = {"id": "r1", "type": "pr", "status": "open", "number": 7}
  after = {**before, "status": "landing"}
  record = lifecycle.stamp(before, after, moves.land_started(), engine_mode.path, NOW)
  assert record["status"] == "landing" and _states(record)["publication"] == "OPEN"


def _stored_size(record: dict) -> int:
  return len((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def test_engine_mode_leaves_the_vector_out_rather_than_make_a_record_too_big_to_read(engine_mode):
  before, after = _claim_pair()
  room = 64 * 1024 - _stored_size(after) - 200
  before, after = _claim_pair(filler="x" * room)
  record = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert "lifecycle" not in record and record == after


def test_engine_mode_trims_a_long_history_before_it_gives_the_vector_up(engine_mode):
  history = [{"at": NOW - i, "type": "NOTE", "source": "test", "pad": "p" * 100} for i in range(30)]
  stored = lifecycle.vector_of({"type": "pr", "status": "prepared"}, NOW)
  stored = {**stored, "history": history}
  before, after = _claim_pair(lifecycle=stored)
  full = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  kept = len(full["lifecycle"]["history"])
  assert kept == 30 or kept > lifecycle.HISTORY_KEPT_WHEN_LARGE  # nothing was too big yet
  room = 64 * 1024 - _stored_size(full) + 500
  before, after = _claim_pair(lifecycle=stored, filler="x" * room)
  record = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert len(record["lifecycle"]["history"]) == lifecycle.HISTORY_KEPT_WHEN_LARGE
  assert _stored_size(record) <= 64 * 1024


def test_engine_mode_leaves_a_stale_vector_out_too_when_the_record_would_be_too_big_to_read(engine_mode):
  """The route builds its record from the stored one, so it carries the old, long vector. Giving the
  vector up means giving that up, or the written record is as unreadable as the one the engine would write."""
  history = [{"at": NOW - i, "type": "NOTE", "source": "test", "pad": "p" * 100} for i in range(30)]
  stale = {**lifecycle.vector_of({"type": "pr", "status": "prepared"}, NOW), "history": history}
  room = 64 * 1024 - _stored_size(_claim_pair()[1]) - 200
  before, after = _claim_pair(lifecycle=stale, filler="x" * room)
  without = {key: value for key, value in after.items() if key != "lifecycle"}
  assert _stored_size(without) <= 64 * 1024 < _stored_size(after)
  record = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert record == without and _stored_size(record) <= 64 * 1024


def test_a_stored_vector_that_is_not_ours_is_adopted_not_trusted(engine_mode):
  before, after = _claim_pair(lifecycle={"v": 99, "regions": "garbage"})
  record = lifecycle.stamp(before, after, moves.claim(before), engine_mode.path, NOW)
  assert _states(record)["publication"] == "SENDING"


# A corrupted stored `lifecycle` field never fails a route


def _slots(**changes) -> dict:
  """A vector's regions as the engine makes them, with named changes."""
  regions = {"publication": {"state": "OPEN", "since": 1, "deadline": None},
             "update": {"state": "NONE", "since": 1, "deadline": None},
             "followup": {"state": "OFF", "since": 1, "deadline": None}}
  return {**regions, **changes}


CORRUPT = {
  "a string": "junk", "a list": ["OPEN"], "a number": 5, "no regions": {"v": 1}, "empty regions": {"v": 1, "regions": {}},
  "regions as a list": {"v": 1, "regions": []},
  "one region only": {"v": 1, "regions": {"publication": {"state": "OPEN", "since": 1, "deadline": None}}, "history": []},
  "a slot that is a string": {"v": 1, "regions": _slots(publication="OPEN"), "history": []},
  "an unknown state": {"v": 1, "regions": _slots(publication={"state": "NOPE", "since": 1, "deadline": None}), "history": []},
  "an unknown region": {"v": 1, "regions": _slots(review={"state": "CLEAR", "since": 1, "deadline": None}), "history": []},
  "no times": {"v": 1, "regions": _slots(update={"state": "NONE"}), "history": []},
  "a deadline that is not a time": {
    "v": 1, "regions": _slots(publication={"state": "SENDING", "since": 1, "deadline": "soon"}), "history": []},
  # A deadline on a state the tables give no deadline (a vector from newer tables with the version unmoved).
  "a deadline on a state with no deadline rule": {
    "v": 1, "regions": _slots(publication={"state": "OPEN", "since": 1, "deadline": 5}), "history": []},
  "a deadline on a followup state with no deadline rule": {
    "v": 1, "regions": _slots(followup={"state": "IDLE", "since": 1, "deadline": 5}), "history": []},
  "history that is a string": {"v": 1, "regions": _slots(), "history": "x"},
  "history of numbers": {"v": 1, "regions": _slots(), "history": [1, 2]},
  "attention that is a list": {"v": 1, "regions": _slots(), "attention": [], "history": []},
  "flags that are a string": {"v": 1, "regions": _slots(), "attention": {"flags": "x"}, "history": []},
  "another version": {"v": 99, "regions": _slots(), "history": []},
}
STATUSES = ("prepared", "submitting", "draft", "open", "merged", "closed", "abandoned", "superseded")


def _every_reader_and_move(before: dict, after: dict) -> list:
  return [
    lambda: moves.claim(before), lambda: moves.claim(before, "claim_stack"), lambda: moves.resume_restored(before),
    lambda: moves.resume_released(before), lambda: moves.send_failed(before, after),
    lambda: moves.update_unconfirmed(before), lambda: moves.pr_created(after), lambda: moves.observed(before, after, True),
    lambda: moves.staged(before, after, False), lambda: moves.ready_settled(before, after),
    lambda: moves.ready_released(before, after), lambda: lifecycle.pr_finished(before, NOW),
    lambda: lifecycle.vector_of(before, NOW), lambda: lifecycle.adopted_vector(before, NOW),
  ]


@pytest.mark.parametrize("name", list(CORRUPT))
def test_a_corrupted_lifecycle_field_never_fails_a_move_or_a_reader(name, storage):
  """Fuzz shape by shape and status by status: no builder, reader or writer raises, in either mode."""
  for status in STATUSES:
    before = {"id": "r1", "type": "pr", "status": status, "number": 7, "url": URL,
              "plan": {"action": "pr_update"}, "lifecycle": CORRUPT[name]}
    after = {**before, "status": "open"}
    for call in _every_reader_and_move(before, after):
      call()
    for mode in ("shadow", "engine"):
      storage.setting({"lifecycle_writer": mode})
      move = moves.pr_updated()
      stamped = lifecycle.stamp(before, after, move, storage.path, NOW)
      assert (stamped is after) if mode == "shadow" else lifecycle._stored_vector(stamped) is not None


@pytest.mark.parametrize("name", list(CORRUPT))
def test_a_corrupted_lifecycle_field_is_adopted_as_if_the_record_had_none(name):
  record = {"id": "r1", "type": "pr", "status": "open", "number": 7, "url": URL}
  assert lifecycle._stored_vector({**record, "lifecycle": CORRUPT[name]}) is None
  assert lifecycle.vector_of({**record, "lifecycle": CORRUPT[name]}, NOW) == lifecycle.vector_of(record, NOW)


def test_a_deadline_on_a_state_that_has_a_deadline_rule_is_still_trusted():
  """The other half: SENDING has a deadline rule, so its stored deadline is read as written."""
  record = {"id": "r1", "type": "pr", "status": "submitting", "number": 7, "url": URL}
  vector = lifecycle.adopted_vector(record, NOW)
  assert vector["regions"]["publication"]["deadline"] is not None
  assert lifecycle._stored_vector({**record, "lifecycle": vector}) == vector


@pytest.mark.parametrize("case", UNREADABLE, ids=[case["name"] for case in UNREADABLE])
def test_a_stored_vector_is_kept_only_when_the_apps_reader_keeps_it(case):
  record = case["record"]
  assert (lifecycle._stored_vector(record) is not None) == case["stored"]
  vector = lifecycle.vector_of(record, 100)
  read = {"publication": vector["regions"]["publication"]["state"], "update": vector["regions"]["update"]["state"],
          "flags": vector["attention"]["flags"]}
  assert read == case["reads"]


@pytest.mark.parametrize("state", [[], ["OPEN"], {}, {"OPEN": 1}, [["OPEN"]], 1, True, None])
def test_a_region_state_that_is_not_a_string_is_no_state_and_never_raises(state):
  vector = lifecycle.adopted_vector({"id": "r1", "type": "pr", "status": "open", "number": 7, "url": URL}, NOW)
  vector["regions"]["publication"]["state"] = state
  assert lifecycle._stored_vector({"type": "pr", "lifecycle": vector}) is None
  assert not lifecycle.slot_readable("publication", vector["regions"]["publication"])


@pytest.mark.parametrize("since, readable", [(NOW, True), (float(NOW), True), (NOW + 0.5, False),
                                             (float("inf"), False), (float("nan"), False), (True, False), (False, False)])
def test_a_time_is_a_whole_number_of_seconds_and_a_boolean_is_not_a_time(since, readable):
  vector = lifecycle.adopted_vector({"id": "r1", "type": "pr", "status": "open", "number": 7, "url": URL}, NOW)
  vector["regions"]["publication"]["since"] = since
  assert (lifecycle._stored_vector({"type": "pr", "lifecycle": vector}) is not None) == readable


@pytest.mark.parametrize("state", [[], ["REVIEWING"], {}, None])
def test_a_review_vector_whose_state_is_not_a_string_is_unreadable(state):
  vector = review_lifecycle._start("review", "REVIEWING", 1000)
  vector["regions"]["review"]["state"] = state
  assert not review_lifecycle.readable(vector)


@pytest.mark.parametrize("since, readable", [(1000, True), (1000.0, True), (1000.5, False), (float("nan"), False), (True, False)])
def test_a_review_vector_reads_a_time_as_the_record_reader_does(since, readable):
  vector = review_lifecycle._start("review", "REVIEWING", 1000)
  vector["regions"]["review"]["since"] = since
  assert review_lifecycle.readable(vector) is readable


@pytest.mark.parametrize("status", STATUSES)
def test_a_vector_the_engine_made_is_still_trusted(status):
  record = {"id": "r1", "type": "pr", "status": status, "number": 7, "url": URL}
  vector = lifecycle.adopted_vector(record, NOW)
  assert lifecycle._stored_vector({**record, "lifecycle": vector}) == vector
