"""Every place the platform writes a contribution record's `status`, with and without the engine.

Shadow mode (the default) must write exactly what the routes always wrote, with no `lifecycle`
field: each writer is pinned here by the full record it writes. Engine mode writes the same
fields plus the vector, and the status word the engine derives. A move the engine refuses before
anything is public is refused to the caller; one that records what already happened is never lost.
"""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import contribution_lifecycle as lifecycle
from app import contribution_lifecycle_engine as engine
from app import contribution_lifecycle_moves as moves
from app import github_contributions as gc
from app.contribution_records import read_record, write_record

NOW = "2026-11-18T10:00:00Z"
URL = "https://github.com/o/r/pull/7"
HEAD, OLD = "a" * 40, "b" * 40
SHADOW_MODES = [None, "shadow"]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
  root = tmp_path / "apps" / "7"
  (root / "contributions").mkdir(parents=True)
  monkeypatch.setattr(gc, "_now_iso", lambda: NOW)

  class Ledger:
    @staticmethod
    def put(record: dict) -> Path:
      path = root / "contributions" / f"{record['id']}.json"
      write_record(path, record)
      return path

    @staticmethod
    def mode(value: object) -> None:
      if value is not None:
        (root / "settings.json").write_text(json.dumps({"lifecycle_writer": value}))

    @staticmethod
    def paths(app_id: int, record_id: str):
      return root / "contributions" / f"{record_id}.json", root / "contributions" / f"{record_id}.diff"

  return Ledger


def _states(record: dict) -> dict:
  return {name: slot["state"] for name, slot in record["lifecycle"]["regions"].items()}


def _plain(record: dict) -> dict:
  return {key: value for key, value in record.items() if key != "lifecycle"}


def _new_pr(status: str = "prepared", **fields) -> dict:
  return {
    "id": "r1", "type": "pr", "status": status, "title": "T", "repo": "o/r",
    "plan": {"action": "pr", "head_sha": HEAD}, "unknown_field": {"kept": True}, **fields,
  }


def _update(status: str = "prepared", **fields) -> dict:
  return {**_new_pr(status), "url": URL, "number": 7, "publication_stage": "ready",
          "plan": {"action": "pr_update", "head_sha": HEAD}, **fields}


# Claim


@pytest.fixture
def claiming(ledger, monkeypatch):
  monkeypatch.setattr(gc, "_record_paths", ledger.paths)
  for name in ("_recheck_submit_app", "_assert_publication_approval", "_require_all_clear_review"):
    monkeypatch.setattr(gc, name, lambda *args, **kwargs: None)

  def claim(record: dict, expected_action: str = "pr"):
    path = ledger.put(record)
    hooked = []
    claimed, _, _ = gc._claim_record(
      app_id=7, record_id=record["id"], db=None, expected_nonce=None, expected_action=expected_action,
      before_claim_write=lambda *args: hooked.append(args),
    )
    return claimed, path, hooked

  return claim


def _claimed(record: dict) -> dict:
  expected = {**record, "status": "submitting", "submitter": "contribute-button",
              "submit_started_at": NOW, "updated_at": NOW}
  expected["personal_submit_input_sha256"] = gc._personal_publication_input_sha256(expected)
  return expected


@pytest.mark.parametrize("mode", SHADOW_MODES)
@pytest.mark.parametrize("record,action", [(_new_pr(), "pr"), (_update(), "pr_update")])
def test_a_claim_writes_submitting_exactly_as_it_always_did(ledger, claiming, mode, record, action):
  ledger.mode(mode)
  claimed, path, hooked = claiming(record, action)
  assert claimed == _claimed(record) == read_record(path)
  assert hooked and hooked[0][0] == claimed and "lifecycle" not in claimed


def test_an_engine_claim_moves_the_publication_or_the_update_to_sending(ledger, claiming):
  ledger.mode("engine")
  claimed, path, _ = claiming(_new_pr())
  assert _plain(claimed) == _claimed(_new_pr())
  assert (_states(claimed)["publication"], claimed["status"]) == ("SENDING", "submitting")
  assert read_record(path) == claimed
  update, _, _ = claiming(_update(id="r2"), "pr_update")
  assert _states(update) == {"publication": "OPEN", "update": "SENDING_UPDATE", "followup": "OFF"}
  assert update["status"] == "submitting"


def test_an_engine_claim_the_engine_refuses_is_a_plain_409_before_anything_is_written(ledger, claiming):
  ledger.mode("engine")
  record = _new_pr(last_submit_stage="push_pending")  # the last send's answer was lost
  path = ledger.put(record)
  before = path.read_bytes()
  with pytest.raises(HTTPException) as raised:
    claiming(record)
  assert raised.value.status_code == 409
  assert "cannot be sent right now" in raised.value.detail
  assert path.read_bytes() == before


def test_a_shadow_claim_of_the_same_record_still_goes_through(ledger, claiming, caplog):
  claimed, _, _ = claiming(_new_pr(last_submit_stage="push_pending"))
  assert claimed["status"] == "submitting" and "lifecycle" not in claimed
  assert "lifecycle_disagreement" in caplog.text


# A failed or unconfirmed send


def _failed(record: dict, patch: dict | None, message: str = "No.", **extra) -> dict:
  expected = {**record, **(patch or {}), "status": "prepared", "last_submit_error": message, "updated_at": NOW}
  expected.pop("last_submit_error_detail", None)
  expected.pop("last_submit_error_code", None)
  expected.update(extra)
  return expected


LOST = {"last_submit_stage": "push_ambiguous", "last_submit_push_sha": HEAD}


@pytest.mark.parametrize("mode", SHADOW_MODES)
@pytest.mark.parametrize("record,patch,extra", [
  (_new_pr("submitting"), None, {}),
  (_new_pr("submitting"), LOST, {"last_submit_error_code": "push_ambiguous"}),
  (_update("submitting"), {"last_submit_error_code": "review_refresh_needed"},
   {"last_submit_error_code": "review_refresh_needed"}),
  (_update("submitting"), LOST, {"last_submit_error_code": "push_ambiguous"}),
])
def test_a_failed_send_writes_prepared_with_its_error_exactly_as_it_always_did(ledger, mode, record, patch, extra):
  ledger.mode(mode)
  path = ledger.put(record)
  code = (patch or {}).get("last_submit_error_code", "")
  failed = gc._mark_submit_failure(
    app_id=0, record_path=path, message="No.", record_patch=patch, code=extra.get("last_submit_error_code", code),
  )
  assert failed == _failed(record, patch, **extra) == read_record(path)
  assert "lifecycle" not in failed


@pytest.mark.parametrize("record,patch,code,publication,update", [
  (_new_pr("submitting"), None, "", "PREPARED", "NONE"),
  (_new_pr("submitting"), LOST, "push_ambiguous", "UNCERTAIN", "NONE"),
  (_update("submitting"), None, "review_refresh_needed", "OPEN", "PREPARED_UPDATE"),
  (_update("submitting"), LOST, "push_ambiguous", "OPEN", "UPDATE_UNCERTAIN"),
])
def test_an_engine_failed_send_is_refused_when_nothing_was_pushed_and_lost_when_the_answer_is_unknown(
  ledger, record, patch, code, publication, update,
):
  ledger.mode("engine")
  path = ledger.put(record)
  failed = gc._mark_submit_failure(app_id=0, record_path=path, message="No.", record_patch=patch, code=code)
  assert failed["status"] == "prepared"
  assert (_states(failed)["publication"], _states(failed)["update"]) == (publication, update)
  assert read_record(path) == failed


def test_a_failed_send_on_a_record_that_is_not_submitting_changes_nothing(ledger):
  ledger.mode("engine")
  path = ledger.put(_new_pr("open", number=7))
  before = path.read_bytes()
  assert gc._mark_submit_failure(app_id=0, record_path=path, message="No.")["status"] == "open"
  assert path.read_bytes() == before


@pytest.mark.parametrize("mode", [None, "engine"])
def test_an_unconfirmed_successor_update_stays_submitting(ledger, mode):
  ledger.mode(mode)
  record = _update("submitting")
  path = ledger.put(record)
  patch = {"last_submit_push_sha": HEAD}
  noted = gc._note_submit_unconfirmed(
    record_path=path, message="Not sure.", record_patch=patch, code="", detail="read it again",
  )
  assert _plain(noted) == {**record, **patch, "status": "submitting", "last_submit_error": "Not sure.",
                           "last_submit_error_code": "update_unconfirmed",
                           "last_submit_error_detail": "read it again", "updated_at": NOW}
  assert read_record(path) == noted
  assert ("lifecycle" in noted) is (mode == "engine")
  if mode == "engine":
    assert _states(noted)["update"] == "UPDATE_UNCERTAIN"


# The PR was created, the PR was updated


@pytest.mark.parametrize("mode", SHADOW_MODES)
@pytest.mark.parametrize("stage,word", [("draft", "draft"), ("ready", "open")])
def test_a_created_pr_writes_draft_or_open_exactly_as_it_always_did(ledger, mode, stage, word):
  ledger.mode(mode)
  record = _new_pr("submitting", last_submit_error="old", last_submit_error_code="x")
  path = ledger.put(record)
  patch = {"publication_stage": stage, "last_submit_push_sha": HEAD}
  done = gc._mark_submit_success(record_path=path, record=record, pr_url=URL, number=7, record_patch=patch)
  expected = {**record, **patch, "status": word, "url": URL, "number": 7, "updated_at": NOW, "submitted_at": NOW}
  expected.pop("last_submit_error"), expected.pop("last_submit_error_code")
  assert done == expected == read_record(path)


@pytest.mark.parametrize("stage,word", [("draft", "draft"), ("ready", "open")])
def test_an_engine_created_pr_is_a_draft_or_open_publication(ledger, stage, word):
  ledger.mode("engine")
  record = _new_pr("submitting")
  path = ledger.put(record)
  done = gc._mark_submit_success(
    record_path=path, record=record, pr_url=URL, number=7, record_patch={"publication_stage": stage},
  )
  assert done["status"] == word and _states(done)["publication"] == word.upper()
  assert done["lifecycle"]["history"][-1]["type"] == "SUBMIT_OK"


@pytest.mark.parametrize("mode", SHADOW_MODES)
def test_an_updated_pr_writes_its_public_word_exactly_as_it_always_did(ledger, mode):
  ledger.mode(mode)
  record = _update("submitting", last_submit_error="old", last_submit_upstream_conflict={"x": 1})
  path = ledger.put(record)
  patch = {"last_submit_push_sha": HEAD}
  done = gc._mark_existing_pr_update_success(record_path=path, record=record, pr_url=URL, number=7, record_patch=patch)
  expected = {**record, **patch, "status": "open", "url": URL, "number": 7, "updated_at": NOW,
              "last_updated_pr_at": NOW}
  expected.pop("last_submit_error"), expected.pop("last_submit_upstream_conflict")
  assert done == expected == read_record(path)


def test_an_engine_updated_pr_settles_its_update_and_stays_public(ledger):
  """A draft PR is restaged, claimed and updated: the engine remembers it is a draft throughout."""
  ledger.mode("engine")
  public = _update("draft", publication_stage="draft")
  path = ledger.put(public)
  restaged = gc.stamped(
    public, {**public, "status": "prepared"}, gc.lifecycle_moves.staged(public, {**public, "status": "prepared"}, False), path,
  )
  claimed = gc.stamped(
    restaged, {**restaged, "status": "submitting"}, gc.lifecycle_moves.claim(restaged), path,
  )
  assert _states(claimed) == {"publication": "DRAFT", "update": "SENDING_UPDATE", "followup": "OFF"}
  done = gc._mark_existing_pr_update_success(
    record_path=path, record=claimed, pr_url=URL, number=7, record_patch={"publication_stage": "draft"},
  )
  assert (done["status"], _states(done)["publication"], _states(done)["update"]) == ("draft", "DRAFT", "NONE")
  assert done["lifecycle"]["history"][-1]["type"] == "UPDATE_OK"


# Ready


class _Target:
  """The exact PR a Ready action names; the record check is stubbed, the writers are real."""


@pytest.fixture
def readying(ledger, monkeypatch):
  target = gc.PersonalReadyTarget(
    repo_path=Path("/nowhere"), repo="o/r", number=7, url=URL, head_repository="me/r",
    head_branch="topic", base_branch="main", head_sha=HEAD,
  )
  monkeypatch.setattr(gc, "_personal_ready_target", lambda record: target)

  def put(status: str, **fields):
    record = _new_pr(status, url=URL, number=7, publication_stage="draft",
                     readying={**target.journal(), "started_at": NOW}, **fields)
    return record, ledger.put(record), target

  return put


@pytest.mark.parametrize("mode", SHADOW_MODES)
@pytest.mark.parametrize("status,word", [("draft", "open"), ("open", "open"), ("merged", "merged"), ("closed", "closed")])
def test_a_settled_ready_writes_open_and_never_regresses_a_finished_pr(ledger, readying, mode, status, word):
  ledger.mode(mode)
  record, path, target = readying(status, last_ready_error="old")
  done = gc._settle_personal_pr_ready(path, target)
  expected = {**record, "status": word, "publication_stage": "ready", "ready_at": NOW,
              "last_ready_head_sha": HEAD, "updated_at": NOW}
  expected.pop("readying"), expected.pop("last_ready_error")
  assert done == expected == read_record(path)


@pytest.mark.parametrize("status,publication", [("draft", "OPEN"), ("open", "OPEN"), ("merged", "MERGED"), ("closed", "CLOSED")])
def test_an_engine_settled_ready_marks_a_draft_ready_and_leaves_the_rest(ledger, readying, status, publication):
  ledger.mode("engine")
  _, path, target = readying(status)
  done = gc._settle_personal_pr_ready(path, target)
  assert _states(done)["publication"] == publication
  assert done["status"] == ("open" if status in ("draft", "open") else status)


@pytest.mark.parametrize("mode", SHADOW_MODES)
@pytest.mark.parametrize("confirmed", [True, False])
def test_a_released_ready_writes_draft_only_when_github_confirmed_it(ledger, readying, mode, confirmed):
  ledger.mode(mode)
  record, path, target = readying("open")
  error = gc.ContributionSubmitError("Still a draft.", code="ready_not_applied")
  done = gc._release_personal_pr_ready(path, target, error, confirmed_draft=confirmed)
  expected = {**record, "last_ready_error": "Still a draft.", "last_ready_error_code": "ready_not_applied",
              "updated_at": NOW}
  if confirmed:
    expected.update(status="draft", publication_stage="draft")
  expected.pop("readying")
  assert done == expected == read_record(path)


def test_an_engine_released_ready_that_github_confirmed_as_draft_goes_back_to_draft(ledger, readying):
  ledger.mode("engine")
  _, path, target = readying("open")
  done = gc._release_personal_pr_ready(
    path, target, gc.ContributionSubmitError("Still a draft.", code="ready_not_applied"), confirmed_draft=True,
  )
  assert done["status"] == "draft" and _states(done)["publication"] == "DRAFT"


# Land-stack (legacy, kept working)


def _stack_rows(ledger, *statuses):
  rows = []
  for index, status in enumerate(statuses):
    record = _new_pr(status, id=f"s{index}", url=URL, number=7 + index)
    rows.append({"record": record, "record_path": ledger.put(record)})
  return rows


@pytest.mark.parametrize("mode", SHADOW_MODES)
def test_a_failed_landing_writes_open_again_and_a_settled_one_writes_merged(ledger, mode):
  ledger.mode(mode)
  rows = _stack_rows(ledger, "landing", "landing")
  failed = gc._mark_stack_land_failure(rows, "Moved.")
  assert [row["status"] for row in failed] == ["open", "open"]
  assert failed[0] == {**rows[0]["record"], "status": "open", "last_land_error": "Moved.", "updated_at": NOW}
  rows = _stack_rows(ledger, "landing", "landing")
  merged = gc._mark_stack_land_success(rows, target_branch="main", landed_sha=HEAD)
  assert merged[1] == {**rows[1]["record"], "status": "merged", "merged_at": NOW, "landed_at": NOW,
                       "last_land_mode": "atomic-fast-forward", "last_land_target_branch": "main",
                       "last_land_head_sha": HEAD, "updated_at": NOW}
  assert all("lifecycle" not in row for row in merged + failed)


def test_an_engine_landing_keeps_its_own_word_and_ends_merged(ledger):
  ledger.mode("engine")
  rows = _stack_rows(ledger, "landing", "landing")
  failed = gc._mark_stack_land_failure(rows, "Moved.")
  assert [row["status"] for row in failed] == ["open", "open"]
  assert _states(failed[0])["publication"] == "OPEN"
  rows = _stack_rows(ledger, "landing", "landing")
  merged = gc._mark_stack_land_success(rows, target_branch="main", landed_sha=HEAD)
  assert [row["status"] for row in merged] == ["merged", "merged"]
  assert _states(merged[0])["publication"] == "MERGED"


def test_a_settled_landing_does_not_claim_its_base_is_the_default_branch(ledger):
  """The move is built from a journal that names a branch, not from a read of the default one. An unknown
  base ends MERGED all the same (the tables read it as the default), and the next GitHub read corrects it."""
  assert moves.land_settled().events[0]["data"] == {"base_is_default": None}
  ledger.mode("engine")
  rows = _stack_rows(ledger, "landing", "landing")
  merged = gc._mark_stack_land_success(rows, target_branch="stack-parent", landed_sha=HEAD)
  assert [row["status"] for row in merged] == ["merged", "merged"]
  assert _states(merged[0])["publication"] == "MERGED"
  read = {"pr": "MERGED", "is_draft": False, "queued": None, "base_is_default": False}
  seen = engine.observe(lifecycle.TABLES, merged[0]["lifecycle"], read, 1_763_460_000)
  assert seen["vector"]["regions"]["publication"]["state"] == "MERGED_ELSEWHERE"  # the read that knows says so


@pytest.fixture
def landing(ledger, monkeypatch):
  monkeypatch.setattr(gc, "_record_paths", ledger.paths)
  monkeypatch.setattr(gc, "_recheck_submit_app", lambda *args, **kwargs: None)

  def claim(*records: dict):
    for record in records:
      ledger.put(record)
    monkeypatch.setattr(gc, "_validate_stack_records", lambda rows: [
      {"record": row, "stack": {"base_branch": "main"}} for row in rows])
    return gc._claim_stack_landing(app_id=7, record_ids=[r["id"] for r in records], db=None, expected_nonce=None)

  return claim


def _open(index: int) -> dict:
  plan = {"action": "pr", "head_sha": HEAD, "base_sha": OLD}
  return _new_pr("open", id=f"s{index}", url=URL, number=7 + index, plan=plan)


@pytest.mark.parametrize("mode", SHADOW_MODES)
def test_a_landing_claim_writes_landing_with_its_journal_exactly_as_it_always_did(ledger, landing, mode):
  ledger.mode(mode)
  rows, how = landing(_open(0), _open(1))
  assert how == "new"
  for index, row in enumerate(rows):
    expected = {**_open(index), "status": "landing", "land_started_at": NOW, "land_target_branch": "main",
                "land_expected_base_sha": OLD, "land_head_sha": HEAD, "updated_at": NOW}
    assert row["record"] == expected == read_record(row["record_path"])


def test_an_engine_landing_claim_keeps_the_landing_word_over_an_open_publication(ledger, landing):
  ledger.mode("engine")
  rows, _ = landing(_open(0), _open(1))
  assert [row["record"]["status"] for row in rows] == ["landing", "landing"]
  assert _states(rows[0]["record"])["publication"] == "OPEN"


# The stack claim


@pytest.fixture
def stack_claiming(ledger, monkeypatch):
  monkeypatch.setattr(gc, "_record_paths", ledger.paths)
  for name in ("_recheck_submit_app", "_assert_publication_approval", "_require_all_clear_review"):
    monkeypatch.setattr(gc, name, lambda *args, **kwargs: None)

  def claim(*records: dict):
    for record in records:
      ledger.put(record)
    monkeypatch.setattr(gc, "_validate_stack_records", lambda rows, **kwargs: [
      {"record": row, "stack": {"base_branch": "main"}} for row in rows])
    return gc._claim_stack_records(app_id=7, record_ids=[r["id"] for r in records], db=None, expected_nonce=None)

  return claim


@pytest.mark.parametrize("mode", SHADOW_MODES)
def test_a_stack_claim_writes_submitting_for_every_layer_exactly_as_it_always_did(ledger, stack_claiming, mode):
  ledger.mode(mode)
  records = [_new_pr(id="s0"), _new_pr(id="s1")]
  rows = stack_claiming(*records)
  for row, record in zip(rows, records):
    expected = {**record, "status": "submitting", "submitter": "contribute-stack-button",
                "submit_started_at": NOW, "updated_at": NOW}
    expected["personal_submit_input_sha256"] = gc._personal_publication_input_sha256(expected)
    assert row["record"] == expected == read_record(row["record_path"])


def test_an_engine_stack_claim_that_refuses_one_layer_writes_none(ledger, stack_claiming):
  ledger.mode("engine")
  records = [_new_pr(id="s0"), _new_pr(id="s1", last_submit_stage="push_pending")]
  paths = [ledger.put(record) for record in records]
  before = [path.read_bytes() for path in paths]
  with pytest.raises(HTTPException) as raised:
    stack_claiming(*records)
  assert raised.value.status_code == 409 and "cannot be sent right now" in raised.value.detail
  assert [path.read_bytes() for path in paths] == before


# The moves of resume, observe and stage, on their own


def _stamp(ledger, before: dict, after: dict, move, mode: str | None) -> dict:
  ledger.mode(mode)
  return gc.stamped(before, after, move, ledger.put(before))


def _moves():
  return gc.lifecycle_moves


@pytest.mark.parametrize("mode", SHADOW_MODES)
def test_a_resume_that_a_receipt_proves_writes_submitting_and_the_engine_agrees(ledger, mode, caplog):
  before = _new_pr("prepared")
  after = {**before, "status": "submitting"}
  assert _stamp(ledger, before, after, _moves().resume_restored(before), mode) == after
  assert "lifecycle_disagreement" not in caplog.text


def test_an_engine_resume_with_a_receipt_marks_the_send_in_flight(ledger):
  before = _new_pr("prepared")
  record = _stamp(ledger, before, {**before, "status": "submitting"}, _moves().resume_restored(before), "engine")
  assert record["status"] == "submitting" and _states(record)["publication"] == "SENDING"
  update = _update("prepared")
  record = _stamp(ledger, update, {**update, "status": "submitting"}, _moves().resume_restored(update), "engine")
  assert _states(record) == {"publication": "OPEN", "update": "SENDING_UPDATE", "followup": "OFF"}


def test_an_engine_resume_of_a_send_whose_answer_was_lost_keeps_it_uncertain(ledger):
  before = _new_pr("prepared", last_submit_stage="push_ambiguous")
  record = _stamp(ledger, before, {**before, "status": "submitting"}, _moves().resume_restored(before), "engine")
  assert record["status"] == "submitting" and _states(record)["publication"] == "UNCERTAIN"


@pytest.mark.parametrize("make,publication,update", [
  (_new_pr, "PREPARED", "NONE"), (_update, "OPEN", "PREPARED_UPDATE"),
])
def test_an_engine_resume_with_no_receipt_returns_the_claim_to_prepared(ledger, make, publication, update):
  before = make("submitting")
  after = {**before, "status": "prepared"}
  record = _stamp(ledger, before, after, _moves().resume_released(before), "engine")
  assert record["status"] == "prepared"
  assert (_states(record)["publication"], _states(record)["update"]) == (publication, update)
  assert _plain(_stamp(ledger, before, after, _moves().resume_released(before), None)) == after


@pytest.mark.parametrize("pr,level,publication", [
  ({"status": "draft"}, None, "DRAFT"),
  ({"status": "open"}, None, "OPEN"),
  ({"status": "merged"}, True, "MERGED"),
  ({"status": "merged"}, False, "MERGED_ELSEWHERE"),  # merged into a branch that is not the default one
  ({"status": "merged"}, None, "MERGED"),
  ({"status": "closed"}, None, "CLOSED"),
])
def test_an_engine_observation_of_a_lost_send_settles_it_by_what_github_shows(ledger, pr, level, publication):
  before = _new_pr("submitting")
  after = {**before, **pr, "url": URL, "number": 7}
  record = _stamp(ledger, before, after, _moves().observed(before, after, level), "engine")
  assert record["status"] == pr["status"] and _states(record)["publication"] == publication


@pytest.mark.parametrize("mode", SHADOW_MODES)
def test_a_shadow_observation_writes_the_routes_record_even_for_a_side_branch_merge(ledger, mode, caplog):
  before = _new_pr("submitting")
  after = {**before, "status": "merged", "url": URL, "number": 7}
  assert _stamp(ledger, before, after, _moves().observed(before, after, False), mode) == after
  assert "lifecycle_disagreement" not in caplog.text  # `merged` is the word for both


def test_an_engine_observation_of_a_sent_update_whose_answer_was_lost_settles_the_update(ledger):
  before = _update("submitting", **LOST)
  after = {**before, "status": "open", "last_submit_stage": "pushed"}
  record = _stamp(ledger, before, after, _moves().observed(before, after, None), "engine")
  assert _states(record) == {"publication": "OPEN", "update": "NONE", "followup": "OFF"}


def test_an_engine_stage_that_drops_the_journal_of_a_lost_send_settles_it_as_not_sent(ledger):
  before = _new_pr("prepared", last_submit_stage="push_ambiguous")
  after = {key: value for key, value in before.items() if key != "last_submit_stage"}
  record = _stamp(ledger, before, after, _moves().staged(before, after, False), "engine")
  assert record["status"] == "prepared" and _states(record)["publication"] == "PREPARED"


def test_a_stage_of_a_public_pr_in_place_raises_no_event_and_keeps_the_pr_as_it_is(ledger):
  before = _update("open", plan={"action": "pr_update", "head_sha": HEAD})
  after = {**before, "status": "open"}
  assert _moves().staged(before, after, True).events == ()
  record = _stamp(ledger, before, after, _moves().staged(before, after, True), "engine")
  assert record["status"] == "open" and _states(record)["update"] == "NONE"
