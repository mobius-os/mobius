"""The server's PR machine facts come from GitHub's live answers, and an unread fact stays unknown."""
import json
from types import SimpleNamespace

from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine, contribution_same_work as same_work
from app.contribution_errors import ContributionSubmitError
from app import contribution_review_runs as domain
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, BASE, CHECKS, PULL, REPO, SHA, TARGET  # noqa: F401  (fixture)

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
  assert machine.not_ready_to_merge(found) == "The approval is for an older version."


def comment(login, commit=SHA, kind="User"):
  """One writer's newest review, as GitHub's latestReviews reports it when it was a plain comment."""
  return review(login, "COMMENTED", commit, kind)


def test_reviews_end_with_each_writers_newest_review_when_it_was_a_plain_comment():
  found = facts([review("Sam", "CHANGES_REQUESTED", OLD)], checks=read_with(
    [review("Sam", "CHANGES_REQUESTED", OLD)], latestReviews={"nodes": [
      comment("Sam"), review("Kim", "APPROVED"), comment("qa-bot", kind="Bot"), {"state": "COMMENTED", "author": None}]}))
  assert found["reviews"] == [{"by": "Sam", "state": "CHANGES_REQUESTED", "commit": OLD, "bot": False},
                              {"by": "Sam", "state": "COMMENTED", "commit": SHA, "bot": False},
                              {"by": "qa-bot", "state": "COMMENTED", "commit": SHA, "bot": True},
                              {"by": "ghost", "state": "COMMENTED", "commit": "", "bot": False}]


def test_a_reviewer_who_looked_again_and_only_commented_is_found_so_by_the_machine():
  nodes = [review("sam", "CHANGES_REQUESTED", OLD)]
  checks = read_with(nodes, latestReviews={"nodes": [comment("sam")]})
  found = facts(None, actor_id=9, checks=checks)
  decision = machine.decide(found)
  assert (decision["state"], decision["looked_again"], decision["rereview_from"]) == ("author.looked_again", ["sam"], [])
  assert machine.action_blocker(found, "ask_approval") == ""
  assert machine.decide(facts(None, actor_id=9, checks=read_with(nodes)))["state"] == "author.waiting_rereview"


def test_comments_are_evidence_never_an_opinion_and_never_my_standing_review():
  mine = [review("Me", "CHANGES_REQUESTED", OLD)]
  found = facts(None, checks=read_with(mine, latestReviews={"nodes": [comment("Me")]}), login="me")
  assert found["myReview"] == {"state": "CHANGES_REQUESTED", "commit": OLD}
  commented = facts(None, checks=read_with([], latestReviews={"nodes": [comment("sam")]}))
  assert machine.not_ready_to_merge(commented) == "Nobody but the author has approved it yet."


def test_a_missing_or_malformed_comment_read_leaves_no_comments_and_never_makes_the_read_incomplete():
  for latest in ({}, {"latestReviews": None}, {"latestReviews": "no"}, {"latestReviews": {"nodes": None}}):
    found = facts(None, checks=read_with([review("sam", "APPROVED")], **latest))
    assert found["readComplete"] is True and [r["state"] for r in found["reviews"]] == ["APPROVED"]


def test_the_check_read_asks_for_every_persons_latest_review_but_only_writers_opinions():
  asked = []

  def gh(cwd, *args, **_ignored):
    asked.append(next(arg for arg in args if arg.startswith("query=")))
    return SimpleNamespace(stdout=json.dumps({"data": {"node": CHECKS}}))

  domain.pull_checks(gh, None, TARGET)
  query = asked[0]
  # GitHub rejects writersOnly on latestReviews, which would fail every check read; mocks cannot catch it.
  assert "latestReviews(first:100){" in query and "latestReviews(first:100," not in query
  assert "latestOpinionatedReviews(first:100, writersOnly:true){" in query


def test_a_change_request_with_no_commit_blocks_and_never_approves():
  found = facts([review("sam", "APPROVED"), {"state": "CHANGES_REQUESTED", "author": {"login": "bob"}, "commit": None}])
  assert machine.not_ready_to_merge(found) == "bob asked for changes and hasn’t approved since."
  lost = facts([{"state": "APPROVED", "author": {"login": "sam"}, "commit": None}])
  assert machine.not_ready_to_merge(lost) == "The approval is for an older version."


def test_the_authors_own_opinion_never_counts():
  found = facts([review("alex", "APPROVED")])
  assert machine.not_ready_to_merge(found) == "Nobody but the author has approved it yet."


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


def test_a_run_whose_first_turn_never_started_stops_holding_the_pr(setup):
  from datetime import timedelta
  from app import models
  from app.chat_writer import create_chat
  db, row, _ = setup
  domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  db.add(create_chat(id="never-started", title="Review"))
  late = models.ContributionReviewRun(id="late", app_id=1, owner_id=row.owner_id, request_id="late-request",
    mode="review", targets_json=row.targets_json, outcomes_json={}, chat_id="never-started",
    github_actor_id="42", app_nonce="nonce")
  db.add(late)
  db.commit()
  # Just admitted, its first turn may still be starting: it holds the PR.
  assert routes._pr_job(db, row.owner_id, "example/project", 7) == {"state": "running", "run_id": "late"}
  late.created_at = late.created_at - timedelta(seconds=routes.FIRST_TURN_GRACE_SECONDS + 5)
  db.commit()
  assert routes._pr_job(db, row.owner_id, "example/project", 7)["state"] == "stalled"


class ComparingGitHub:
  """A stand-in for `gh` that answers the base tip and the comparisons the same-work proof reads."""

  def __init__(self, compares, base=BASE):
    self.compares = compares  # commit -> a comparison; a commit not listed is unreadable
    self.base = base
    self.reads = []

  def __call__(self, cwd, *args, **_ignored):
    self.reads.append(args[1])
    if args[1] == "repos/example/project/git/ref/heads/main":
      return self._answer({"object": {"sha": self.base}})
    base, commit = args[1].removeprefix("repos/example/project/compare/").split("...")
    assert base == self.base, "compared against something other than the base tip"
    if commit not in self.compares:
      raise ContributionSubmitError("gh: Not Found (HTTP 404)")
    return self._answer(self.compares[commit])

  @staticmethod
  def _answer(value):
    return SimpleNamespace(stdout=json.dumps(value), stderr="", returncode=0)


WORK = {"files": [{"filename": "a.js", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1 @@\n+x"}]}
APPROVED_OLD = [review("sam", "APPROVED", OLD)]


def read_facts(github, nodes=None, *, target=TARGET, pull=None, repo=REPO, **changes):
  pull = pull or {**PULL, "number": 7, "user": AUTHOR}
  checks = read_with(APPROVED_OLD if nodes is None else nodes, **changes)
  return pr_facts.read_facts(github, None, target, repo, pull, actor_id=9, job=None, checks=checks)


def test_an_approval_of_an_older_commit_carries_over_when_the_prs_own_change_is_the_same():
  github = ComparingGitHub({OLD: WORK, SHA: WORK})
  found = read_facts(github)
  assert found["reviews"][0]["sameWork"] is True
  assert machine.not_ready_to_merge(found) == ""
  assert github.reads == [f"repos/example/project/compare/{BASE}...{commit}" for commit in (SHA, OLD)]


def test_an_approval_of_an_older_commit_that_could_not_be_proved_the_same_stays_old():
  changed = {"files": [{**WORK["files"][0], "patch": "@@ -1 +1 @@\n+y"}]}
  for compares, proof in (({OLD: WORK, SHA: changed}, False), ({OLD: WORK}, None), ({}, None)):
    found = read_facts(ComparingGitHub(compares))
    assert found["reviews"][0]["sameWork"] is proof
    assert machine.not_ready_to_merge(found) == "The approval is for an older version."


def test_the_base_tip_comes_from_the_callers_target_or_else_from_the_prs_base_branch():
  own = ComparingGitHub({OLD: WORK, SHA: WORK})
  read_facts(own)
  assert not any("git/ref" in endpoint for endpoint in own.reads)
  without = {key: value for key, value in TARGET.items() if key not in ("base_sha", "base_ref")}
  fresh = ComparingGitHub({OLD: WORK, SHA: WORK}, base="e" * 40)
  assert read_facts(fresh, target=without)["reviews"][0]["sameWork"] is True
  assert fresh.reads[0] == "repos/example/project/git/ref/heads/main"


def test_an_unreadable_base_branch_leaves_the_approval_unproved_and_never_breaks_the_read():
  without = {key: value for key, value in TARGET.items() if key != "base_sha"}
  broken = ComparingGitHub({OLD: WORK, SHA: WORK})
  broken.base = None  # GitHub's answer names no sha
  found = read_facts(broken, target=without)
  assert "sameWork" not in found["reviews"][0] and found["readComplete"] is True
  nameless = ComparingGitHub({OLD: WORK, SHA: WORK})
  assert "sameWork" not in read_facts(nameless, target=without, pull={**PULL, "base": {}, "user": AUTHOR})["reviews"][0]
  assert nameless.reads == []


def test_nothing_is_compared_unless_an_approval_of_an_older_commit_could_decide_an_open_pr():
  github = ComparingGitHub({OLD: WORK, SHA: WORK})
  here = [review("sam", "APPROVED", OLD), review("kim", "APPROVED", SHA)]
  assert "sameWork" not in read_facts(github, here)["reviews"][0]
  assert "sameWork" not in read_facts(github, [review("Alex", "APPROVED", OLD)])["reviews"][0]
  assert "sameWork" not in read_facts(github, [review("qa-bot", "APPROVED", OLD, "Bot")])["reviews"][0]
  assert "sameWork" not in read_facts(github, [review("sam", "CHANGES_REQUESTED", OLD)])["reviews"][0]
  assert "sameWork" not in read_facts(github, pull={**PULL, "state": "closed", "user": AUTHOR})["reviews"][0]
  assert "sameWork" not in read_facts(github, pull={**PULL, "merged": True, "state": "closed", "user": AUTHOR})["reviews"][0]
  assert read_facts(github, None, latestOpinionatedReviews=None)["readComplete"] is False
  assert github.reads == []


def test_nothing_is_compared_for_a_draft_or_for_someone_who_cannot_merge():
  github = ComparingGitHub({OLD: WORK, SHA: WORK})
  assert "sameWork" not in read_facts(github, pull={**PULL, "draft": True, "user": AUTHOR})["reviews"][0]
  assert "sameWork" not in read_facts(github, repo={**REPO, "permissions": {"pull": True}})["reviews"][0]
  assert github.reads == []
  assert read_facts(github, repo={**REPO, "permissions": {"push": True}})["reviews"][0]["sameWork"] is True


def test_only_a_few_approved_commits_are_proved_so_the_merge_path_never_waits_on_many_reads():
  many = [f"{index:040x}" for index in range(1, 7)]
  github = ComparingGitHub({**{commit: WORK for commit in many}, SHA: WORK})
  found = read_facts(github, [review(f"person{index}", "APPROVED", commit) for index, commit in enumerate(many)])
  proved = [item for item in found["reviews"] if "sameWork" in item]
  assert len(proved) == same_work.MAX_APPROVED_COMMITS and len(github.reads) == 1 + same_work.MAX_APPROVED_COMMITS


def test_an_approval_at_the_head_by_a_bot_or_by_the_author_does_not_stop_an_older_approval_being_compared():
  for at_head in (review("qa-bot", "APPROVED", SHA, "Bot"), review("Alex", "APPROVED", SHA)):
    github = ComparingGitHub({OLD: WORK, SHA: WORK})
    found = read_facts(github, [review("sam", "APPROVED", OLD), at_head])
    assert found["reviews"][0]["sameWork"] is True
    assert machine.not_ready_to_merge(found) == ""
    assert len(github.reads) == 2
