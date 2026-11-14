"""The summary form of the review-runs list: what a polling client keeps, and what stays in the run."""
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import contribution_review_list as list_view, contribution_review_runs as domain, models
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, SHA, setup

LONG = "x" * 50_000
ITEM_KEY = domain.key(ITEM)


def heavy_outcome(text):
  """A review the way a busy instance holds one: findings, a drafted verdict and the evidence behind them."""
  repair = {"id": "fix-1", "state": "proposed", "head_sha": "c" * 40, "from_sha": SHA, "decided_at": None,
            "checkout": "/data/.contribution-runtime/fix-1", "allowed_files": ["a.py"] * 500,
            "files": ["a.py"], "summary": text, "diff_stat": text}
  return {
    "state": "needs_you", "summary": text, "tests": text, "scope": sorted(routes.SCOPE), "head_sha": SHA,
    "frozen_evidence": {"diff": text}, "independent_reviews": [{"summary": text}], "reviewer_steps": [text],
    "verdict_proposal": {"id": "v-1", "state": "proposed", "event": "REQUEST_CHANGES", "head_sha": SHA,
                         "review_id": 77, "body": text, "comment_body": text, "marker": "m", "body_sha256": "h"},
    "public_review": {"state": "posted", "id": 5, "url": "https://example.test/r/5", "marker": "m"},
    "repair_attempts": [repair],
  }


def heavy_options(text):
  return {"post_review": True, "propose_verdict": True, "model": "frozen-model", "review_prompt": text,
          "mandatory_instructions": text, "input_options": {"review_prompt": text}}


def add_run(db, like, run_id, *, app_id=1, text=None, owner_id=None):
  """Another run for the same chat and PR, holding `text`-sized documents when given."""
  db.add(models.ContributionReviewRun(
    id=run_id, app_id=app_id, owner_id=owner_id or like.owner_id, request_id=f"request-{run_id}", mode="review",
    targets_json=like.targets_json, chat_id=like.chat_id, github_actor_id="42",
    app_nonce=like.app_nonce if app_id else None,
    options_json=heavy_options(text) if text else {}, outcomes_json={ITEM_KEY: heavy_outcome(text)} if text else {}))
  db.commit()


@pytest.fixture
def heavy(setup):
  db, row, _principal = setup
  row.options_json = heavy_options(LONG)
  db.commit()
  domain.save_outcome(db, row, ITEM_KEY, heavy_outcome(LONG))
  return setup


def listed(setup, view=None):
  db, _row, principal = setup
  return routes.list_reviews(1, db, principal, **({"view": view} if view else {}))


def test_summary_list_drops_documents_and_keeps_what_a_row_needs(heavy):
  run = listed(heavy, "summary")["runs"][0]
  item = run["items"][0]
  assert item["state"] == "verdict_pending" and item["head_sha"] == SHA and item["number"] == ITEM["number"]
  assert item["verdict_proposal"] == {"id": "v-1", "state": "proposed", "event": "REQUEST_CHANGES",
                                      "head_sha": SHA, "review_id": 77}
  assert item["public_review"] == {"state": "posted", "id": 5, "url": "https://example.test/r/5"}
  assert item["repair_attempts"] == [{"id": "fix-1", "state": "proposed", "head_sha": "c" * 40,
                                      "from_sha": SHA, "decided_at": None}]
  assert {"tests", "scope", "frozen_evidence", "independent_reviews", "reviewer_steps"}.isdisjoint(item)
  assert run["options"] == {"post_review": True, "model": "frozen-model"}
  full = listed(heavy)["runs"][0]
  kept = {key: value for key, value in run.items() if key not in ("items", "options")}
  assert kept == {key: full[key] for key in kept}
  assert {"id", "request_id", "mode", "chat_id", "state", "execution_state", "created_at", "can_stop"} <= set(kept)


def test_summary_list_says_so_and_the_default_list_does_not(setup):
  assert listed(setup, "summary")["view"] == "summary"
  assert "view" not in listed(setup)
  assert listed(setup, "full") == listed(setup)


def test_default_list_still_carries_every_document_whole(heavy):
  item = listed(heavy)["runs"][0]["items"][0]
  assert item["summary"] == LONG and item["tests"] == LONG
  assert item["verdict_proposal"]["body"] == LONG and item["frozen_evidence"] == {"diff": LONG}
  assert listed(heavy)["runs"][0]["options"]["review_prompt"] == LONG


def test_summary_findings_are_cut_with_a_visible_omission_and_the_run_keeps_them_whole(heavy):
  db, row, principal = heavy
  item = listed(heavy, "summary")["runs"][0]["items"][0]
  assert len(item["summary"]) <= 240 and item["summary_truncated"] is True
  assert item["summary"].endswith("… open this run for the full findings")
  whole = routes.get_review(1, row.id, db, principal)["run"]
  assert whole["items"][0]["summary"] == LONG and whole["items"][0]["tests"] == LONG
  assert whole["items"][0]["verdict_proposal"]["body"] == LONG
  assert whole["items"][0]["repair_attempts"][0]["summary"] == LONG
  assert whole["options"]["review_prompt"] == LONG


def test_short_findings_pass_through_the_summary_unmarked(setup):
  db, row, _ = setup
  domain.save_outcome(db, row, ITEM_KEY, {"state": "needs_you", "summary": "One short finding."})
  item = listed(setup, "summary")["runs"][0]["items"][0]
  assert item["summary"] == "One short finding." and "summary_truncated" not in item


def test_summary_of_a_run_does_not_grow_with_its_documents(setup):
  db, row, _ = setup
  add_run(db, row, "small-1", text="x" * 1_000)
  add_run(db, row, "large-2", text="x" * 500_000)
  runs = {run["id"]: run for run in listed(setup, "summary")["runs"]}
  full = {run["id"]: run for run in listed(setup)["runs"]}
  identity = ("id", "request_id", "created_at")
  small, large = ({k: v for k, v in runs[name].items() if k not in identity} for name in ("small-1", "large-2"))
  assert small == large
  assert len(json.dumps(full["large-2"])) > 100 * len(json.dumps(runs["large-2"]))


def test_summary_run_leaves_out_the_takeover_notice_text(setup):
  db, row, _ = setup
  row.outcomes_json = {routes.TAKEOVER_NOTICE_KEY: {ITEM_KEY: {"state": "posted", "body": LONG}}}
  db.commit()
  assert "takeover_notice" in listed(setup)["runs"][0]
  assert "takeover_notice" not in listed(setup, "summary")["runs"][0]


def test_summary_list_asks_the_same_permission_as_the_full_list(setup, monkeypatch):
  db, _row, principal = setup
  def refused(*args):
    raise HTTPException(403, "Contribute's GitHub permission was revoked.")
  monkeypatch.setattr(routes, "_validate_submit_app", refused)
  for view in ("full", "summary"):
    with pytest.raises(HTTPException) as error:
      routes.list_reviews(1, db, principal, view=view)
    assert error.value.status_code == 403
  for bad in [Principal(owner=principal.owner, app_id=1, scope="app"),
              Principal(owner=principal.owner, app_id=None, scope="media")]:
    with pytest.raises(HTTPException) as error:
      routes.list_reviews(None, db, bad, view="summary")
    assert error.value.status_code == 403


def test_summary_list_names_only_the_asking_owners_and_apps_runs(setup):
  db, row, _ = setup
  other = models.Owner(username="someone-else", hashed_password="unused")
  db.add(other)
  db.add(models.App(id=2, name="Other", slug="other", source_dir="test-other",
                    github_access=True, token_nonce="other-nonce"))
  db.flush()
  add_run(db, row, "other-owner", owner_id=other.id)
  add_run(db, row, "other-app", app_id=2)
  for view in ("full", "summary"):
    assert [run["id"] for run in listed(setup, view)["runs"]] == ["batch"]


def test_summary_leaves_a_core_run_read_through_exactly_as_the_navigation_view_was(setup):
  db, row, _ = setup
  row.app_id = None
  row.options_json = heavy_options(LONG)
  db.commit()
  domain.save_outcome(db, row, ITEM_KEY, heavy_outcome(LONG))
  full, summary = listed(setup)["runs"][0], listed(setup, "summary")["runs"][0]
  assert summary == full and summary["details_in_owning_chat"] is True
  assert "verdict_proposal" not in summary["items"][0] and "repair_attempts" not in summary["items"][0]
  assert "options" not in summary and LONG not in json.dumps(summary)


def test_one_summary_answer_projects_an_owned_run_and_a_core_run_each_its_own_way(setup):
  db, row, _ = setup
  add_run(db, row, "core-run", app_id=None, text=LONG)
  runs = {run["id"]: run for run in listed(setup, "summary")["runs"]}
  assert runs["core-run"]["details_in_owning_chat"] is True and "verdict_proposal" not in runs["core-run"]["items"][0]
  assert "details_in_owning_chat" not in runs["batch"] and runs["batch"]["app_id"] == 1


def test_http_view_option_is_opt_in_and_unknown_values_are_refused(heavy):
  db, _row, principal = heavy
  application = FastAPI()
  application.include_router(routes.router)
  application.dependency_overrides[routes.get_db] = lambda: db
  application.dependency_overrides[routes.get_principal] = lambda: principal
  path = "/api/github/contributions/1/review-runs"
  with TestClient(application) as client:
    default = client.get(path)
    full = client.get(path, params={"view": "full"})
    summary = client.get(path, params={"view": "summary"})
    assert default.status_code == 200 and "view" not in default.json() and full.content == default.content
    assert default.json()["runs"][0]["items"][0]["tests"] == LONG
    assert summary.status_code == 200 and summary.json()["view"] == "summary"
    assert "tests" not in summary.json()["runs"][0]["items"][0]
    assert len(summary.content) * 20 < len(default.content)
    assert client.get(path, params={"view": "everything"}).status_code == 422


def test_core_list_has_no_summary_form_and_answers_in_full(setup):
  db, row, principal = setup
  row.app_id = None
  row.app_nonce = None
  db.commit()
  domain.save_outcome(db, row, ITEM_KEY, heavy_outcome(LONG))
  application = FastAPI()
  application.include_router(routes.router)
  application.dependency_overrides[routes.get_db] = lambda: db
  application.dependency_overrides[routes.get_principal] = lambda: Principal(owner=principal.owner, app_id=None)
  with TestClient(application) as client:
    answer = client.get("/api/github/review-runs", params={"view": "summary"}).json()
  assert "view" not in answer and answer["runs"][0]["items"][0]["tests"] == LONG


def test_a_field_added_to_runs_later_stays_out_of_the_summary():
  light = list_view.summary_run({
    "id": "r", "state": "complete", "new_run_field": LONG, "options": {"post_review": True, "new_option": LONG},
    "items": [{"repo": "a/b", "number": 1, "state": "all_clear", "new_item_field": LONG,
               "verdict_proposal": {"state": "posted", "new_verdict_field": LONG},
               "repair_attempts": [{"state": "pushed", "new_attempt_field": LONG}]}]})
  assert LONG not in json.dumps(light)
  assert light["items"][0]["verdict_proposal"] == {"state": "posted"}
  assert light["items"][0]["repair_attempts"] == [{"state": "pushed"}]
