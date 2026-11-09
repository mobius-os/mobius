"""Starting a review run asks the PR machine first: role, one job per PR, and the owner's choices."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain, models
from app.database import SessionLocal
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


def comments(monkeypatch, state="confirmed"):
  from app import contribution_review_public as public
  from app.contribution_review_effects import DispatchResult
  posted = []
  def post(_gh, _cwd, target, body):
    posted.append((target["number"], body))
    return {"confirmed": DispatchResult.confirmed({"id": 99, "url": "https://github.com/c/99"}),
            "unknown": DispatchResult.unknown("lost answer"),
            "not_dispatched": DispatchResult.not_dispatched("GitHub refused the request (HTTP 403).")}[state]
  monkeypatch.setattr(public, "post_issue_comment", post)
  return posted


def test_taking_over_posts_the_owners_reason_once_as_a_comment(owner, monkeypatch):
  db, fixture_run, principal, started = owner
  settle_fixture_review(db, fixture_run)
  posted = comments(monkeypatch)
  run = start(db, principal, mode="review_fix_merge", takeover_reason=REASON)["run"]
  assert posted == [(7, f"Taking this PR over: {REASON}")]
  notice = run["takeover_notice"]["example/project#7"]
  assert (notice["state"], notice["id"], notice["body"]) == ("posted", 99, f"Taking this PR over: {REASON}")
  assert start(db, principal, mode="review_fix_merge", takeover_reason=REASON)["run"]["id"] == run["id"]
  assert len(posted) == 1 and len(started) == 1


@pytest.mark.parametrize("state, status", [("not_dispatched", 409), ("unknown", 502)])
def test_a_takeover_github_did_not_confirm_starts_nothing_and_is_never_resent(owner, monkeypatch, state, status):
  db, fixture_run, principal, started = owner
  settle_fixture_review(db, fixture_run)
  posted = comments(monkeypatch, state)
  assert refusal(db, principal, mode="review_fix_merge", takeover_reason=REASON)[0] == status
  assert started == [] and len(posted) == 1
  row = db.query(models.ContributionReviewRun).filter_by(request_id="machine-admission").one()
  assert row.outcomes_json[routes.TAKEOVER_NOTICE_KEY]["example/project#7"]["state"] == state
  # The same request never posts again; the stopped run holds nothing, so a new request may.
  status_again, detail = refusal(db, principal, mode="review_fix_merge", takeover_reason=REASON)
  assert status_again == status and len(posted) == 1 and started == []
  if state == "unknown":
    assert detail == routes.TAKEOVER_NOTICE_UNCONFIRMED
  comments(monkeypatch)
  assert start(db, principal, mode="review_fix_merge", request_id="takeover-again", takeover_reason=REASON)["run"]


def test_a_takeover_never_posts_from_an_account_other_than_the_one_that_approved_it(owner, monkeypatch):
  db, fixture_run, principal, started = owner
  settle_fixture_review(db, fixture_run)
  posted = comments(monkeypatch)
  accounts = [{"id": 42}, {"id": 43}]  # the connection is switched after admission read it
  monkeypatch.setattr(domain, "read", lambda *args: accounts.pop(0) if args[2] == "user" else {"id": 42})
  status, detail = refusal(db, principal, mode="review_fix_merge", takeover_reason=REASON)
  assert status == 409 and "connected GitHub account changed" in detail
  assert posted == [] and started == []
  row = db.query(models.ContributionReviewRun).filter_by(request_id="machine-admission").one()
  notice = row.outcomes_json[routes.TAKEOVER_NOTICE_KEY]["example/project#7"]
  assert notice["state"] == "not_dispatched" and "changed" in notice["summary"]
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"
  # Switching back does not revive it: the same request is refused and still posts nothing.
  monkeypatch.setattr(domain, "read", lambda *args: {"id": 42})
  assert refusal(db, principal, mode="review_fix_merge", takeover_reason=REASON)[0] == 409
  assert posted == [] and started == []


def test_a_takeover_stopped_while_its_account_was_read_never_posts(owner, monkeypatch):
  db, fixture_run, principal, started = owner
  settle_fixture_review(db, fixture_run)
  posted = comments(monkeypatch)

  def stopped_meanwhile(*args):
    # The owner's durable Stop, from another request, lands while the connected account is being read.
    if args[2] == "user":
      other = SessionLocal()
      try:
        run = other.query(models.ContributionReviewRun).filter_by(request_id="machine-admission").first()
        if run is not None:  # the admission read comes before the run exists
          domain.save_outcome(other, run, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
      finally:
        other.close()
    return {"id": 42}

  monkeypatch.setattr(domain, "read", stopped_meanwhile)
  status, detail = refusal(db, principal, mode="review_fix_merge", takeover_reason=REASON)
  assert status == 409 and "was stopped" in detail
  assert posted == [] and started == []
  row = db.query(models.ContributionReviewRun).filter_by(request_id="machine-admission").one()
  notice = row.outcomes_json[routes.TAKEOVER_NOTICE_KEY]["example/project#7"]
  assert notice["state"] == "not_dispatched" and "stopped" in notice["summary"]


def test_finishing_the_owners_own_pr_posts_nothing(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  posted = comments(monkeypatch)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42, "login": "me"}}))
  run = start(db, principal, mode="review_fix_merge")["run"]
  assert posted == [] and "takeover_notice" not in run


def test_a_draft_take_over_is_admitted_under_the_single_repair_scope_and_never_marked_ready(owner, monkeypatch):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  comments(monkeypatch)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": True}))
  monkeypatch.setattr(domain, "inspect_target", lambda *a: {**TARGET, "is_draft": True, "head_repo": "example/project",
                                                            "head_repo_id": 1, "head_ref": "topic"})
  run = start(db, principal, mode="review_fix_merge", takeover_reason=REASON)["run"]
  assert run["options"]["confirmation_scope"] == "named_pr_repairs_and_reviewed_successors"
  assert run["items"][0]["is_draft"] is True
  # Readiness stays the owner's own tap: no grant lets the run mark it ready.
  paths = {route.path for route in routes.router.routes}
  assert "/api/github/contributions/{app_id}/review-runs/{run_id}/ready" not in paths
  assert "never merges, enqueues or marks a takeover ready" in " ".join(domain.takeover_brief(
    db.get(models.ContributionReviewRun, run["id"])).split())
