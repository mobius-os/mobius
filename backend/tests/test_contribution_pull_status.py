"""The owner's own Open for review, Back to draft, Close and Ask for re-review: owner-only,
asked of the PR machine on GitHub's live answer, and never repeated when unconfirmed."""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app import contribution_pull_status as subject
from app.contribution_errors import ContributionSubmitError
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_direct_merge import FakeGitHub, NAME, NUMBER, _flags
from tests.test_contribution_review_runs import APPROVAL, PULL, SHA

ME = {"id": 42, "login": "alex", "type": "User"}
OLDER = "c" * 40
MOVED = "d" * 40
PULL_ENDPOINT = f"repos/{NAME}/pulls/{NUMBER}"


class StatusGitHub(FakeGitHub):
  """The merge fake, plus the connected account and the four status writes."""

  def __init__(self, *, mine=True, draft=False):
    super().__init__()
    self.pull = {**PULL, "number": NUMBER, "draft": draft, "auto_merge": None,
                 "user": ME if mine else {"id": 7, "login": "sam"}}
    self.answers = {}  # write name -> what GitHub answers, or an exception
    self.writes_seen = []

  def __call__(self, cwd, *args, check=True, input_text=None, **_ignored):
    self.calls.append(args)
    if args[:3] == ("api", "--method", "PATCH") and args[3] == PULL_ENDPOINT:
      return self._write("close", {**self.pull, "state": "closed"}, _flags(args[4:]))
    if args[:3] == ("api", "--method", "POST") and args[3] == f"{PULL_ENDPOINT}/requested_reviewers":
      asked = json.loads(input_text)["reviewers"]
      return self._write("reviewers", {**self.pull, "requested_reviewers": [{"login": login} for login in asked]}, asked)
    if args[:2] == ("api", "graphql") and "mutation" in _flags(args[2:])["query"]:
      field = "convertPullRequestToDraft" if "convertPullRequestToDraft" in _flags(args[2:])["query"] else "markPullRequestReadyForReview"
      node = {"id": PULL["node_id"], "isDraft": field == "convertPullRequestToDraft", "headRefOid": SHA}
      return self._write(field, {"data": {field: {"pullRequest": node}}}, _flags(args[2:])["id"])
    if args[1] == "user":
      return self._answer(ME)
    return super().__call__(cwd, *args, check=check)

  def _write(self, name, confirmed, sent):
    self.writes_seen.append((name, sent))
    return self._answer(self.answers.get(name, confirmed))


def body(**changes):
  return routes.PullStatusChange(**{"repo": NAME, "number": NUMBER, "head_sha": SHA, **changes})


def asking(reviewers, **changes):
  return routes.ReviewerRequest(**{"repo": NAME, "number": NUMBER, "head_sha": SHA, "reviewers": reviewers, **changes})


def change(github, name, request=None, job=None):
  return asyncio.run(subject.change_pull_status(github, None, name, request or body(), lambda: None, job=job))


def refused(github, name, request=None, job=None):
  with pytest.raises(HTTPException) as refusal:
    change(github, name, request, job)
  return refusal.value.status_code, refusal.value.detail


def test_the_owners_draft_opens_for_review_at_the_head_they_saw():
  github = StatusGitHub(draft=True)
  assert change(github, "ready") == {"state": "ready", "observed_head_sha": SHA,
                                      "repo": NAME, "number": NUMBER, "head_sha": SHA}
  assert [name for name, _ in github.writes_seen] == ["markPullRequestReadyForReview"]


@pytest.mark.parametrize("world, reason", [
  ({"mine": False, "draft": True}, "You can’t do that as the reviewer of this PR."),
  ({"mine": True, "draft": False}, "It is already open for review."),
])
def test_open_for_review_is_refused_in_the_machines_words(world, reason):
  github = StatusGitHub(**world)
  assert refused(github, "ready") == (409, reason)
  assert github.writes_seen == []


def test_no_status_change_while_an_agent_works_on_the_pr_but_a_stalled_one_holds_nothing():
  github = StatusGitHub(draft=True)
  assert refused(github, "ready", job={"state": "running"}) == (409, "An agent is already working on this PR.")
  assert github.writes_seen == []
  assert change(github, "ready", job={"state": "stalled"})["state"] == "ready"


def test_open_for_review_waits_while_auto_merge_is_armed():
  github = StatusGitHub(draft=True)
  github.pull["auto_merge"] = {"merge_method": "squash"}
  assert refused(github, "ready") == (409, subject.AUTO_MERGE_ON)
  assert github.writes_seen == []


def test_back_to_draft_turns_the_owners_open_pr_into_a_draft():
  github = StatusGitHub()
  assert change(github, "draft")["state"] == "draft"
  assert refused(StatusGitHub(draft=True), "draft") == (409, "It is not open for review.")


def test_close_is_the_authors_or_a_writers():
  assert change(StatusGitHub(), "close")["state"] == "closed"
  theirs = StatusGitHub(mine=False)
  assert change(theirs, "close")["state"] == "closed"
  reader = StatusGitHub(mine=False)
  reader.repo = {**reader.repo, "permissions": {"pull": True}}
  assert refused(reader, "close") == (409, "Needs write access.")
  assert reader.writes_seen == []


def test_ask_for_rereview_only_of_people_whose_review_is_of_an_older_version():
  github = StatusGitHub()
  github.reviews = [{**APPROVAL, "commit": {"oid": OLDER}}]
  assert change(github, "request-reviewers", asking(["Sam"]))["state"] == "requested"
  assert github.writes_seen == [("reviewers", ["Sam"])]
  status, reason = refused(github, "request-reviewers", asking(["bob"]))
  assert status == 409 and "sam" in reason
  current = StatusGitHub()
  assert refused(current, "request-reviewers", asking(["sam"])) == (409, "Nobody reviewed an older version.")


def test_a_head_that_moved_since_the_owner_looked_is_refused_before_any_write():
  github = StatusGitHub(draft=True)
  github.pull["head"] = {"sha": MOVED}
  assert refused(github, "ready")[0] == 409
  assert github.writes_seen == []


def test_an_unreadable_review_list_refuses_a_status_change_in_the_machines_words():
  github = StatusGitHub(draft=True)
  github.unreadable.add("reviews")
  assert refused(github, "ready") == (409, "This PR could not be read. Try again.")
  assert github.writes_seen == []


@pytest.mark.parametrize("name, write, code", [
  ("ready", "markPullRequestReadyForReview", "ready_unconfirmed"),
  ("draft", "convertPullRequestToDraft", "draft_unconfirmed"),
  ("close", "close", "close_unconfirmed"),
  ("request-reviewers", "reviewers", "reviewers_unconfirmed"),
])
def test_an_unconfirmed_write_is_a_502_with_its_code_and_is_never_retried(name, write, code):
  github = StatusGitHub(draft=name == "ready")
  github.reviews = [{**APPROVAL, "commit": {"oid": OLDER}}]
  github.answers[write] = "not json"
  request = asking(["sam"]) if name == "request-reviewers" else None
  status, detail = refused(github, name, request)
  assert status == 502 and detail["code"] == code and "Check the pull request" in detail["message"]
  assert len(github.writes_seen) == 1


def test_a_refusal_github_proves_is_a_409_that_changed_nothing():
  github = StatusGitHub()
  github.answers["close"] = ContributionSubmitError("gh: Validation Failed (HTTP 422)")
  assert refused(github, "close") == (409, "GitHub refused the request (HTTP 422); no effect was applied.")


def test_only_the_owner_can_change_a_pull_requests_status(monkeypatch):
  agent = Principal(owner=None, app_id=None, chat_id="review-chat", run_id="run")
  with pytest.raises(HTTPException) as refusal:
    asyncio.run(routes.close_pull_now(1, body(), None, agent))
  assert refusal.value.status_code == 403


def test_status_routes_are_not_taken_by_a_contribution_record_named_pulls():
  from starlette.routing import Match
  from app.main import app
  endpoints = {"ready": routes.mark_pull_ready_now, "draft": routes.back_to_draft_now,
               "close": routes.close_pull_now, "request-reviewers": routes.request_rereview_now}
  for name, endpoint in endpoints.items():
    scope = {"type": "http", "method": "POST", "path": f"/api/github/contributions/1/pulls/{name}", "root_path": ""}
    first = next(route for route in app.router.routes if route.matches(scope)[0] == Match.FULL)
    # FastAPI may keep an included router whole; then it must be this one, not github's records.
    assert getattr(first, "endpoint", None) is endpoint or getattr(first, "original_router", None) is routes.router


def test_reviewers_are_github_logins():
  from pydantic import ValidationError
  with pytest.raises(ValidationError):
    asking(["not a login"])
  assert asking(["sam", "sam"]).reviewers == ["sam"]
