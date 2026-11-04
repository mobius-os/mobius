"""The server's PR machine facts come from GitHub's live answers, and an unread fact stays unknown."""
import json
from types import SimpleNamespace

from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine
from app import contribution_review_runs as domain
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, CHECKS, PULL, REPO, SHA, TARGET  # noqa: F401  (fixture)

OLD = "c" * 40
AUTHOR = {"id": 9, "login": "Alex", "type": "User"}
USER_IDS = {"sam": 101, "bob": 102, "qa-bot": 103}


def review(login, state, commit=SHA, user_id=None, kind="User"):
  return {"user": {"id": user_id or USER_IDS[login], "login": login, "type": kind},
          "state": state, "commit_id": commit}


def facts(review_list, *, actor_id=42, checks=CHECKS, pull=None, repo=REPO, job=None):
  pull = pull or {**PULL, "number": 7, "user": AUTHOR}
  return pr_facts.pr_facts(actor_id=actor_id, repo=repo, pull=pull, checks=checks,
                           review_list=review_list, job=job)


def test_a_later_dismissal_clears_an_opinion_and_a_comment_never_replaces_one():
  found = facts([review("sam", "APPROVED", OLD), review("sam", "COMMENTED"),
                 review("bob", "CHANGES_REQUESTED"), review("bob", "DISMISSED")])
  assert found["reviews"] == [{"by": "sam", "state": "APPROVED", "commit": OLD, "bot": False}]


def test_the_authors_own_reviews_never_count_and_bots_are_tagged():
  found = facts([review("alex", "APPROVED", user_id=9), review("qa-bot", "APPROVED", kind="Bot")])
  assert found["reviews"] == [{"by": "qa-bot", "state": "APPROVED", "commit": SHA, "bot": True}]
  assert machine.action_blocker(found, "merge") == "Nobody but the author has approved it yet."


def test_role_follows_the_connected_account_id_and_only_a_reviewer_has_my_review():
  mine = [review("me", "APPROVED", OLD, user_id=42), review("me", "COMMENTED", user_id=42),
          review("me", "PENDING", user_id=42)]
  assert facts(mine)["role"] == "reviewer"
  assert facts(mine)["myReview"] == {"state": "COMMENTED", "commit": SHA}
  own = facts(mine, pull={**PULL, "number": 7, "user": {"id": 42, "login": "Renamed"}})
  assert own["role"] == "author" and own["author"] == "renamed" and own["myReview"] is None


def test_checks_are_none_only_when_github_says_no_checks_exist():
  def with_rollup(rollup):
    return {**CHECKS, "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}
  assert facts([], checks=with_rollup(None))["checks"] == "none"
  assert facts([], checks=with_rollup({"state": "FAILURE"}))["checks"] == "fail"
  assert facts([], checks=with_rollup({"state": "EXPECTED"}))["checks"] == "pending"
  assert facts([], checks={**CHECKS, "commits": {"nodes": [{"commit": {}}]}})["checks"] == "unknown"
  assert facts([], checks={**CHECKS, "headRefOid": OLD})["checks"] == "unknown"
  assert facts([], checks={**CHECKS, "mergeable": "UNKNOWN"})["mergeable"] == "unknown"


def test_a_failed_read_makes_the_whole_read_incomplete_and_refuses_merge():
  approved = [review("sam", "APPROVED")]
  assert machine.action_blocker(facts(approved), "merge") == ""
  for incomplete in (facts(None), facts(approved, checks=None)):
    assert incomplete["readComplete"] is False
    assert machine.action_blocker(incomplete, "merge") == "This PR could not be read. Try again."


def test_review_list_is_read_whole_and_a_list_past_the_page_cap_is_incomplete(monkeypatch):
  pages = []
  def read(_gh, _cwd, endpoint):
    pages.append(endpoint)
    return [review("sam", "APPROVED")] * (100 if len(pages) < 3 else 1)
  monkeypatch.setattr(domain, "read", read)
  assert len(pr_facts.read_reviews(None, None, "example/project", 7)) == 201
  assert pages[-1].endswith("/pulls/7/reviews?per_page=100&page=3")
  monkeypatch.setattr(domain, "read", lambda *a: [review("sam", "APPROVED")] * 100)
  assert pr_facts.read_reviews(None, None, "example/project", 7) is None
  monkeypatch.setattr(domain, "read", lambda *a: {"message": "Not Found"})
  assert pr_facts.read_reviews(None, None, "example/project", 7) is None


def test_permission_is_the_strongest_grant_and_an_archived_repository_takes_no_writes():
  assert pr_facts.permission_of({"permissions": {"push": True, "maintain": True}}) == "MAINTAIN"
  assert pr_facts.permission_of({"permissions": {"admin": True}}) == "ADMIN"
  assert pr_facts.permission_of({"permissions": {"pull": True}}) == "READ"
  assert pr_facts.permission_of({"archived": True, "permissions": {"admin": True}}) == "READ"


def test_lifecycle_reads_merged_closed_draft_and_open():
  assert pr_facts.lifecycle_of({"state": "closed", "merged": True}) == "merged"
  assert pr_facts.lifecycle_of({"state": "closed", "merged": False}) == "closed"
  assert pr_facts.lifecycle_of({"state": "open", "draft": True}) == "draft"
  assert pr_facts.lifecycle_of({"state": "open"}) == "open"
  assert pr_facts.lifecycle_of({}) == "unknown"


def test_read_facts_reuses_checks_the_caller_already_read(monkeypatch):
  monkeypatch.setattr(domain, "pull_checks", lambda *a: (_ for _ in ()).throw(AssertionError("read twice")))
  gh = lambda _cwd, *args, **kw: SimpleNamespace(stdout=json.dumps([review("sam", "APPROVED")]))  # noqa: E731
  found = pr_facts.read_facts(gh, None, TARGET, REPO, PULL, actor_id=42, job=None, checks=CHECKS)
  assert found["readComplete"] is True and found["reviews"][0]["by"] == "sam"


def test_a_live_run_on_the_pr_is_its_job_and_a_stopped_one_holds_nothing(setup):
  db, row, _ = setup
  owner_id = row.owner_id
  assert routes._pr_job(db, owner_id, "Example/Project", 7) == {"state": "running", "run_id": row.id}
  assert routes._pr_job(db, owner_id, "example/project", 7, exclude_run_id=row.id) is None
  assert routes._pr_job(db, owner_id, "example/project", 8) is None
  domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  assert routes._pr_job(db, owner_id, "example/project", 7) == {"state": "stalled", "run_id": row.id}


def test_a_run_that_settled_its_pr_or_waits_on_the_owner_holds_no_job(setup):
  db, row, _ = setup
  item_key = domain.key({"repo": "example/project", "number": 7})
  domain.save_outcome(db, row, item_key, {"state": "all_clear", "head_sha": SHA})
  assert routes._pr_job(db, row.owner_id, "example/project", 7) is None
  domain.save_outcome(db, row, item_key, {"state": "needs_you", "head_sha": SHA})
  assert routes._pr_job(db, row.owner_id, "example/project", 7) is None
