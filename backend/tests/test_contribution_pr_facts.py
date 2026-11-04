"""The server's PR machine facts come from GitHub's live answers, and an unread fact stays unknown."""
from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine
from app import contribution_review_runs as domain
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, CHECKS, PULL, REPO, SHA, TARGET  # noqa: F401  (fixture)

OLD = "c" * 40
AUTHOR = {"id": 9, "login": "Alex", "type": "User"}


def review(login, state, commit=SHA, kind="User"):
  """One writer's standing opinion as GitHub's latestOpinionatedReviews reports it."""
  return {"state": state, "author": {"login": login, "__typename": kind}, "commit": {"oid": commit}}


def read_with(nodes, latest=None, **changes):
  base = {key: value for key, value in CHECKS.items() if key != "latestOpinionatedReviews"}
  return {**base, **({} if nodes is None else {"latestOpinionatedReviews": {"nodes": nodes}}),
          "viewerLatestReview": latest, **changes}


def facts(nodes, *, latest=None, actor_id=42, login="me", checks=None, pull=None, job=None):
  pull = pull or {**PULL, "number": 7, "user": AUTHOR}
  return pr_facts.pr_facts(actor_id=actor_id, actor_login=login, repo=REPO, pull=pull,
                           checks=checks if checks is not None else read_with(nodes, latest), job=job)


def test_reviews_are_the_writers_standing_opinions_with_ghosts_and_lost_commits_kept():
  found = facts([review("Sam", "APPROVED", OLD), {"state": "CHANGES_REQUESTED", "author": None, "commit": None},
                 review("qa-bot", "APPROVED", kind="Bot")])
  assert found["reviews"] == [{"by": "Sam", "state": "APPROVED", "commit": OLD, "bot": False},
                              {"by": "ghost", "state": "CHANGES_REQUESTED", "commit": "", "bot": False},
                              {"by": "qa-bot", "state": "APPROVED", "commit": SHA, "bot": True}]
  # A change request whose commit is gone still blocks; a bot's approval never counts.
  assert machine.action_blocker(found, "merge") == "The approval is for an older version."


def test_a_change_request_with_no_commit_blocks_and_never_approves():
  found = facts([review("sam", "APPROVED"), {"state": "CHANGES_REQUESTED", "author": {"login": "bob"}, "commit": None}])
  assert machine.action_blocker(found, "merge") == "bob asked for changes and hasn’t approved since."
  lost = facts([{"state": "APPROVED", "author": {"login": "sam"}, "commit": None}])
  assert machine.action_blocker(lost, "merge") == "The approval is for an older version."


def test_the_authors_own_opinion_never_counts():
  found = facts([review("alex", "APPROVED")])
  assert machine.action_blocker(found, "merge") == "Nobody but the author has approved it yet."


def test_role_follows_the_connected_account_id_and_only_a_reviewer_has_my_review():
  assert facts([], latest={"state": "APPROVED", "commit": {"oid": SHA}})["myReview"] == {"state": "APPROVED", "commit": SHA}
  own = facts([], latest={"state": "APPROVED", "commit": {"oid": SHA}},
              pull={**PULL, "number": 7, "user": {"id": 42, "login": "Renamed"}})
  assert own["role"] == "author" and own["author"] == "renamed" and own["myReview"] is None


def test_a_later_comment_or_a_pending_draft_never_hides_my_standing_opinion():
  mine = [review("Me", "CHANGES_REQUESTED", OLD)]
  assert facts(mine, latest={"state": "COMMENTED", "commit": {"oid": SHA}})["myReview"] == {
    "state": "CHANGES_REQUESTED", "commit": OLD}
  assert facts(mine, latest={"state": "PENDING", "commit": {"oid": SHA}})["myReview"] == {
    "state": "CHANGES_REQUESTED", "commit": OLD}
  assert facts([], latest={"state": "COMMENTED", "commit": {"oid": SHA}})["myReview"] == {"state": "COMMENTED", "commit": SHA}
  assert facts(mine, latest={"state": "APPROVED", "commit": {"oid": SHA}})["myReview"] == {"state": "APPROVED", "commit": SHA}
  assert facts([], latest=None)["myReview"] is None


def test_checks_are_none_only_when_github_says_no_checks_exist():
  def with_rollup(rollup):
    return read_with([], commits={"nodes": [{"commit": {"statusCheckRollup": rollup}}]})
  assert facts([], checks=with_rollup(None))["checks"] == "none"
  assert facts([], checks=with_rollup({"state": "FAILURE"}))["checks"] == "fail"
  assert facts([], checks=with_rollup({"state": "EXPECTED"}))["checks"] == "pending"
  assert facts([], checks=read_with([], commits={"nodes": [{"commit": {}}]}))["checks"] == "unknown"
  assert facts([], checks=read_with([], headRefOid=OLD))["checks"] == "unknown"
  assert facts([], checks=read_with([], mergeable="UNKNOWN"))["mergeable"] == "unknown"


def test_a_failed_read_makes_the_whole_read_incomplete_and_refuses_merge():
  approved = [review("sam", "APPROVED")]
  assert machine.action_blocker(facts(approved), "merge") == ""
  missing = pr_facts.pr_facts(actor_id=42, repo=REPO, pull={**PULL, "number": 7}, checks=None, job=None)
  for incomplete in (facts(None), missing):
    assert incomplete["readComplete"] is False
    assert machine.action_blocker(incomplete, "merge") == "This PR could not be read. Try again."


def test_a_failed_check_read_is_logged_without_its_detail(monkeypatch, caplog):
  import logging
  monkeypatch.setattr(domain, "pull_checks", lambda *a: (_ for _ in ()).throw(OSError("token ghp_secret")))
  with caplog.at_level(logging.INFO, logger="app.contribution_pr_facts"):
    assert pr_facts.read_checks(None, None, TARGET) is None
  assert "OSError" in caplog.text and "ghp_secret" not in caplog.text


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
  found = pr_facts.read_facts(None, None, TARGET, REPO, PULL, actor_id=42, job=None, checks=CHECKS)
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
