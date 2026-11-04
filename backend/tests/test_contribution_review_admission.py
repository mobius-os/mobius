"""Starting a review run asks the PR machine first: role, one job per PR, and the owner's choices."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain, models
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import (CHECKS, ITEM, PULL, REPO, TARGET, VERDICTS, settle_fixture_review,
                                                 setup)  # noqa: F401  (fixture)

REASON = "The author is away for a month and asked a maintainer to finish it."
WRITER_REPO = {**REPO, "permissions": {"push": True}}


@pytest.fixture
def owner(setup, monkeypatch):
  db, row, principal = setup
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: {**TARGET, "head_repo": "example/project",
                                                            "head_repo_id": 1, "head_ref": "topic"})
  monkeypatch.setattr(domain, "repair_file_scope", lambda *a: ["owned.py"])
  started = []
  async def admit(**kwargs):
    started.append(kwargs["chat_id"])
    db.add(models.ChatRun(id=f"admitted-{len(started)}", chat_id=kwargs["chat_id"], status="running"))
    db.commit()
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  return db, row, Principal(owner=principal.owner, app_id=None), started


def start(db, owner_principal, mode="review", request_id="machine-admission", **fields):
  if mode == "review_fix_merge":
    preview = routes.review_preview(1, routes.ReviewPreview(mode=mode), db, owner_principal)
    fields = {"confirmation_scope": "named_pr_repairs_and_reviewed_successors",
              "preview_sha256": preview["preview_sha256"], **fields}
  body = routes.StartReviews(request_id=request_id, mode=mode, items=[ITEM], **fields)
  return asyncio.run(routes.start_reviews(1, body, db, owner_principal))


def refusal(db, owner_principal, **kwargs):
  with pytest.raises(HTTPException) as error:
    start(db, owner_principal, **kwargs)
  return error.value.status_code, error.value.detail


def test_second_run_on_same_pr_is_refused(owner):
  db, fixture_run, principal, started = owner
  assert refusal(db, principal) == (409, "An agent is already working on this PR.")
  settle_fixture_review(db, fixture_run)
  first = start(db, principal)
  assert refusal(db, principal, request_id="another-request") == (409, "An agent is already working on this PR.")
  assert start(db, principal)["run"]["id"] == first["run"]["id"]  # The same request still returns its run.
  assert len(started) == 1 and db.query(models.ContributionReviewRun).count() == 2


def test_a_stopped_run_holds_nothing_so_the_owner_can_run_it_again(owner):
  db, fixture_run, principal, _ = owner
  domain.save_outcome(db, fixture_run, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  assert start(db, principal)["run"]["mode"] == "review"


def test_review_merge_requires_owner_verdict(owner):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  status, detail = refusal(db, principal, mode="review_merge")
  assert status == 422 and "propose_verdict" in detail
  status, _ = refusal(db, principal, mode="review_merge", options={"post_review": True})
  assert status == 422
  run = start(db, principal, mode="review_merge", options=VERDICTS)["run"]
  assert run["options"]["propose_verdict"] is True


def test_review_merge_is_refused_on_the_owners_own_pr(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42, "login": "me"}}))
  assert refusal(db, principal, mode="review_merge", options=VERDICTS) == (409, routes.OWN_PR_REVIEW_MERGE)


def test_a_review_of_the_owners_own_pr_is_a_private_check(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42, "login": "me"}}))
  row = db.get(models.ContributionReviewRun, start(db, principal)["run"]["id"])
  assert row.targets_json[0]["role"] == "author"


def test_take_over_needs_a_maintainer_and_a_reason_that_is_frozen_into_the_grant(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  assert refusal(db, principal, mode="review_fix_merge") == (422, routes.TAKEOVER_REASON_NEEDED)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (WRITER_REPO, PULL))
  assert refusal(db, principal, mode="review_fix_merge", takeover_reason=REASON) == (
    409, "Only maintainers can take over someone else’s PR.")
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, PULL))
  run = start(db, principal, mode="review_fix_merge", takeover_reason=f"  {REASON} ")["run"]
  assert run["options"]["takeover_reason"] == REASON
  assert run["items"][0]["role"] == "reviewer"
  with pytest.raises(HTTPException) as changed:
    start(db, principal, mode="review_fix_merge", takeover_reason="A different reason.")
  assert changed.value.status_code == 409


def test_finishing_the_owners_own_pr_needs_no_reason_or_maintainer(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42, "login": "me"}}))
  assert start(db, principal, mode="review_fix_merge")["run"]["items"][0]["role"] == "author"


def test_a_takeover_reason_outside_a_takeover_is_refused(owner):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  status, _ = refusal(db, principal, takeover_reason=REASON)
  assert status == 422


def test_an_unreadable_pr_is_never_admitted(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "latestOpinionatedReviews": None})
  assert refusal(db, principal) == (409, "This PR could not be read. Try again.")
