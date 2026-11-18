"""The stage and observe routes, whole, in both writer modes.

The writer unit tests pin each status writer on its own; here the real routes run, so the move
each route raises is the one the route really makes. Shadow (the default) writes the record the
route always wrote, with no `lifecycle` field; engine adds the vector and keeps the status word.
"""
import itertools
import json
import logging
import subprocess
from pathlib import Path

import pytest

from app import contribution_lifecycle as lifecycle
from app import contribution_lifecycle_engine as engine
from app import contribution_lifecycle_moves as moves
from app.config import get_settings
from tests.test_contribution_staging import REVISED, _commit, staging  # noqa: F401  (fixture and helpers)
from tests.test_publication_observation_routes import (  # noqa: F401  (fixtures and helpers)
  isolated_private_receipts, observation,
)
from app.routes import github as github_routes

PR_URL = "https://github.com/octo/project/pull/7"
NOW = 1_763_460_000
MODES = [None, "shadow", "engine"]


def _states(record: dict) -> dict:
  return {name: slot["state"] for name, slot in record["lifecycle"]["regions"].items()}


def _choose(app_id: int, mode: str | None) -> None:
  if mode is not None:
    path = Path(get_settings().data_dir) / "apps" / str(app_id) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lifecycle_writer": mode}))


# Stage


def _public(staging, monkeypatch, status: str = "open", revise: bool = True, **fields) -> Path:
  record = staging["new_record"]().json()["record"]
  path = Path(get_settings().data_dir) / "apps" / str(staging["app_id"]) / "contributions" / "greet.json"
  path.write_text(json.dumps({
    **record, "status": status, "number": 7, "url": PR_URL, "head_repository": "octo/project",
    "publication_stage": "draft" if status == "draft" else "ready", **fields,
  }))
  live = {
    "error": None, "head_sha": record["plan"]["head_sha"], "base_branch": "main",
    "base_sha": staging["base"], "title": "Add a greeting", "body": "Adds a greeting.",
  }
  monkeypatch.setattr(github_routes, "_autopilot_live_target", lambda *_: live)
  if revise:
    _commit(staging["worktree"], {"greet.py": REVISED}, "Answer review")
  return path


@pytest.mark.parametrize("mode", [None, "shadow"])
def test_shadow_stage_of_a_public_pr_writes_the_record_it_always_wrote(staging, monkeypatch, caplog, mode):
  _choose(staging["app_id"], mode)
  path = _public(staging, monkeypatch)
  with caplog.at_level(logging.WARNING):
    record = staging["stage"]().json()["record"]
  assert record == json.loads(path.read_text())
  assert "lifecycle" not in record and record["status"] == "prepared"
  assert "lifecycle_disagreement" not in caplog.text  # the engine agrees with the route


@pytest.mark.parametrize("status,publication", [("open", "OPEN"), ("draft", "DRAFT")])
def test_engine_stage_of_a_public_pr_prepares_an_update_and_the_pr_stays_public(
  staging, monkeypatch, status, publication,
):
  _choose(staging["app_id"], "engine")
  path = _public(staging, monkeypatch, status)
  record = staging["stage"]().json()["record"]
  assert record == json.loads(path.read_text())
  assert record["status"] == "prepared" and record["plan"]["action"] == "pr_update"
  assert _states(record) == {"publication": publication, "update": "PREPARED_UPDATE", "followup": "OFF"}
  assert record["lifecycle"]["history"][-1]["type"] == "STAGE_UPDATE"


def test_engine_restage_of_a_prepared_update_stays_prepared(staging, monkeypatch):
  _choose(staging["app_id"], "engine")
  _public(staging, monkeypatch)
  assert staging["stage"]().status_code == 200
  _commit(staging["worktree"], {"greet.py": REVISED + "# again\n"}, "Once more")
  record = staging["stage"]().json()["record"]
  assert record["status"] == "prepared" and _states(record)["update"] == "PREPARED_UPDATE"


# `stage_contribution` asks the engine only after `_stage_record` has run `_track_live_source`, which moves git
# refs in the live source (pins the draft, may commit the adopted revision). A refusal there would leave those
# moves behind with the record unchanged. None is reachable: the tables accept STAGE_UPDATE wherever a record can
# be staged, and a lost send's journal is dropped with a new head, so the send is settled as not sent first.
# These tests pin that, so a table change that makes a restage refusable fails here and the judgement must move
# before `_stage_record` (facts from `previous` and the inputs).

STAGED_WORDS = ("prepared", "open", "draft")  # `_STAGEABLE_STATUSES` of the stage route


def _vectors_of_a_stageable_record():
  """Every (publication, update) pair of states a record the stage route accepts can be in, with its word."""
  tables = lifecycle.TABLES
  base = {"id": "r", "type": "pr", "number": 7, "url": PR_URL, "status": "open", "plan": {"action": "pr_update"}}
  seed = lifecycle.adopted_vector(base, 1)
  for publication, update in itertools.product(tables["regions"]["publication"]["states"], tables["regions"]["update"]["states"]):
    if update == "STRANDED_UPDATE":
      continue  # only after the PR finished, and no stageable word is a finished PR
    slots = {"publication": {"state": publication, "since": 1, "deadline": None},
             "update": {"state": update, "since": 1, "deadline": None}}
    vector = {**seed, "regions": {**seed["regions"], **slots}}
    for word in STAGED_WORDS:
      if engine.status_of(tables, vector, word) == word:
        yield vector, word, publication not in ("PREPARED", "NOT_CONFIRMED") or update != "NONE"


def test_no_restage_a_stage_route_accepts_can_be_refused_by_the_engine(tmp_path):
  settings = tmp_path / "apps" / "7"
  (settings / "contributions").mkdir(parents=True)
  (settings / "settings.json").write_text(json.dumps({"lifecycle_writer": "engine"}))
  path = settings / "contributions" / "r.json"
  covered: set[str] = set()
  for vector, word, public in _vectors_of_a_stageable_record():
    for journal_kept, in_place in itertools.product((False, True), (False, True)):
      if in_place and word == "prepared":
        continue  # an Autopilot round restages only its open or draft pull request
      before = {"id": "r", "type": "pr", "status": word, "lifecycle": vector,
                "plan": {"action": "pr_update" if public else "pr", "head_sha": "h"}, **({"number": 7, "url": PR_URL} if public else {})}
      after = {**before, "status": word if in_place else "prepared"}
      if journal_kept:
        after.update(last_submit_stage="push_pending", last_submit_push_sha="h")
      move = moves.staged(before, after, in_place)
      lifecycle.stamp(before, after, move, path, NOW)  # raises LifecycleRefused if the engine would refuse it
      covered.add(vector["regions"]["update"]["state"])
  assert {"UPDATE_UNCERTAIN", "UPDATE_NOT_CONFIRMED"} <= covered  # the states a lost send leaves, both reached


def test_engine_accepts_a_restage_during_an_unsettled_update_push_and_changes_nothing_in_the_live_source(staging, monkeypatch):
  _choose(staging["app_id"], "engine")
  path = _unsettled_update(staging, monkeypatch, revise=False)
  before_source = _live_source_state(staging)
  response = staging["stage"]()
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  assert record["status"] == "prepared" and record["last_submit_stage"] == "push_pending"  # the same head: the journal stays
  assert _states(record)["update"] == "UPDATE_UNCERTAIN"
  assert record == json.loads(path.read_text())
  assert _live_source_state(staging) == before_source  # the same head: `_track_live_source` moves nothing


def test_engine_restage_of_a_new_head_drops_the_unsettled_push_and_settles_it_as_not_sent(staging, monkeypatch):
  _choose(staging["app_id"], "engine")
  _unsettled_update(staging, monkeypatch, revise=True)
  response = staging["stage"]()
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  assert "last_submit_stage" not in record and record["status"] == "prepared"
  assert "UPDATE_REFUSED" in [entry["type"] for entry in record["lifecycle"]["history"]]


def _live_source_state(staging) -> tuple[str, str]:
  """The live source's committed HEAD and every draft ref it holds: what `_track_live_source` can move."""
  def git(*args: str) -> str:
    return subprocess.run(
      ["git", "-C", str(staging["source"]), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()
  return git("rev-parse", "HEAD"), git("for-each-ref", "refs/mobius/contribution-drafts")


def _unsettled_update(staging, monkeypatch, *, revise: bool) -> Path:
  """A public PR whose update push is unconfirmed: the journal is kept for the head it pushed."""
  path = _public(staging, monkeypatch, "prepared", revise=revise)
  record = {key: value for key, value in json.loads(path.read_text()).items() if key != "lifecycle"}  # as an older app wrote it
  path.write_text(json.dumps({
    **record, "plan": {**record["plan"], "action": "pr_update"},
    "last_submit_stage": "push_pending", "last_submit_push_sha": record["plan"]["head_sha"],
  }))
  return path


@pytest.mark.parametrize("mode", MODES)
def test_a_first_stage_creates_a_private_record_in_every_mode(staging, mode):
  _choose(staging["app_id"], mode)
  record = staging["new_record"]().json()["record"]
  assert record["status"] == "prepared" and record["plan"]["action"] == "pr"
  assert ("lifecycle" in record) is (mode == "engine")
  if mode == "engine":
    assert _states(record)["publication"] == "PREPARED"


# Observe


def _merged(live: dict, default_branch: str | None) -> None:
  live.update(state="closed", merged_at="2026-11-18T09:00:00Z", closed_at="2026-11-18T09:00:00Z",
              merge_commit_sha="c" * 40, draft=False)
  if default_branch:
    live["base"]["repo"]["default_branch"] = default_branch


@pytest.mark.parametrize("mode", [None, "shadow"])
def test_shadow_observe_saves_the_record_it_always_saved(client, observation, caplog, mode):
  f = observation
  _choose(f["app_id"], mode)
  with caplog.at_level(logging.WARNING):
    body = client.post(f["url"], headers=f["headers"]).json()
  assert body["outcome"] == "settled", body
  saved = json.loads(f["path"].read_text())
  assert saved == body["records"][0] and saved["status"] == "draft" and "lifecycle" not in saved
  assert "lifecycle_disagreement" not in caplog.text


def test_engine_observe_of_a_lost_send_finds_the_draft_and_keeps_the_receipt_rules(client, observation):
  f = observation
  _choose(f["app_id"], "engine")
  body = client.post(f["url"], headers=f["headers"]).json()
  assert body["outcome"] == "settled", body
  saved = json.loads(f["path"].read_text())
  assert saved == body["records"][0] and saved["status"] == "draft" and saved["number"] == 7
  assert _states(saved)["publication"] == "DRAFT"
  assert saved["autopilot"] == f["record"]["autopilot"]  # unknown and foreign fields survive


@pytest.mark.parametrize("default_branch,publication", [
  ("main", "MERGED"),  # the PR landed on the default branch
  ("trunk", "MERGED_ELSEWHERE"),  # it merged into a branch that is not the default one
  (None, "MERGED"),  # GitHub did not say: nothing is said against landing
])
def test_engine_observe_of_a_merge_says_whether_it_reached_the_default_branch(
  client, observation, default_branch, publication,
):
  f = observation
  _choose(f["app_id"], "engine")
  _merged(f["live"], default_branch)
  body = client.post(f["url"], headers=f["headers"]).json()
  assert body["outcome"] == "settled", body
  saved = json.loads(f["path"].read_text())
  assert saved["status"] == "merged"  # the word is the same for both: readers that need more read `lifecycle`
  assert _states(saved)["publication"] == publication


def test_shadow_observe_of_a_side_branch_merge_writes_merged_and_the_engine_has_nothing_to_add(
  client, observation, caplog,
):
  f = observation
  _merged(f["live"], "trunk")
  with caplog.at_level(logging.WARNING):
    body = client.post(f["url"], headers=f["headers"]).json()
  saved = json.loads(f["path"].read_text())
  assert body["outcome"] == "settled" and saved["status"] == "merged" and "lifecycle" not in saved
  assert "lifecycle_disagreement" not in caplog.text
