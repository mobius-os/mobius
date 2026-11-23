"""A review the platform held back because the PR moved first: the reason, Post it anyway, and the send."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_held as held
from app import contribution_review_public as public
from app import contribution_review_runs as domain
from app import contribution_review_verdicts as verdicts
from app.contribution_review_effects import DispatchResult
from app.deps import Principal
from app.routes import contribution_reviews as routes
from app.contribution_review_runs import current_pull as real_current_pull, moved_pull as real_moved_pull
from tests.test_contribution_review_runs import CHECKS, ITEM, PULL, REPO, report, setup  # noqa: F401

KEY = domain.key(ITEM)
MOVED = "f" * 40


def _verdict_run(setup, **options):
  db, row, _ = setup
  row.mode = "review"
  row.options_json = {"post_review": True, "propose_verdict": True, **options}
  db.commit()


def _github(monkeypatch, *, head=MOVED, author_id=None):
  """GitHub as the real PR-identity proof sees it: the same PR, now at `head`."""
  def read(_gh, _cwd, path):
    if path == "user":
      return {"id": 42}
    if path.endswith("/pulls/7"):
      return {**PULL, "head": {"sha": head}, **({"user": {"id": author_id}} if author_id else {})}
    return REPO
  monkeypatch.setattr(domain, "read", read)
  monkeypatch.setattr(domain, "current_pull", real_current_pull)
  monkeypatch.setattr(domain, "moved_pull", real_moved_pull)
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {**CHECKS, "headRefOid": head})


def _held_review(setup, monkeypatch, **github):
  """A verdict-proposing run whose REQUEST_CHANGES was refused because the PR had moved."""
  _verdict_run(setup)
  _github(monkeypatch, **github)
  return report(setup, publish=True, state="needs_you", summary="Blocking bug")


def _owner(setup):
  return Principal(owner=setup[2].owner, app_id=None)


def _post_anyway(setup, principal=None):
  db, row, _ = setup
  body = routes.HeldVerdict(**{k: ITEM[k] for k in ("repo", "number", "head_sha")})
  return asyncio.run(routes.propose_held_verdict(1, row.id, body, db, principal or _owner(setup)))


def _submit(setup, proposal_id):
  db, row, _ = setup
  body = routes.VerdictDecision(**{k: ITEM[k] for k in ("repo", "number", "head_sha")}, proposal_id=proposal_id, decision="submit")
  return asyncio.run(routes.decide_verdict(1, row.id, body, db, _owner(setup)))


def _record_posts(monkeypatch):
  events = []
  def post(_gh, _cwd, target, body, event="COMMENT"):
    events.append((event, target["head_sha"], body))
    return DispatchResult.confirmed({"id": 7, "url": "https://github.com/x/review/7"})
  monkeypatch.setattr(public, "post_review", post)
  return events


def test_a_refused_verdict_tells_the_agent_plainly_that_nothing_was_proposed(setup, monkeypatch):
  reply = _held_review(setup, monkeypatch)
  assert reply["not_posted"]["reviewed_head"] == ITEM["head_sha"] and reply["not_posted"]["current_head"] == MOVED
  message = reply["not_posted"]["message"]
  assert "Nothing was proposed or posted" in message and f"moved from {ITEM['head_sha'][:7]} to {MOVED[:7]}" in message
  assert "do not retry" in message


def test_a_refusal_that_is_not_a_moved_head_is_told_in_the_servers_words(setup, monkeypatch):
  _verdict_run(setup)
  _github(monkeypatch, head=ITEM["head_sha"])
  monkeypatch.setattr(domain, "current_pull", lambda *args: (_ for _ in ()).throw(HTTPException(409, "The base branch changed.")))
  reply = report(setup, publish=True, state="needs_you", summary="Blocking bug")["not_posted"]
  assert reply["current_head"] is None and reply["reason"] == "The base branch changed."
  assert "Nothing was proposed or posted: The base branch changed." in reply["message"]


def test_the_run_view_marks_a_held_back_review_only_for_a_run_that_proposes_verdicts(setup, monkeypatch):
  item = _held_review(setup, monkeypatch)["run"]["items"][0]
  assert item["post_findings"]["held_back"] is True and "live_blocker" in item
  db, row, _ = setup
  row.options_json = {"post_review": True}
  db.commit()
  assert "held_back" not in routes._run_view(db, row)["items"][0]["post_findings"]


def test_post_it_anyway_saves_the_exact_proposal_worded_with_both_commits_and_sends_nothing(setup, monkeypatch):
  events = _record_posts(monkeypatch)
  _held_review(setup, monkeypatch)
  item = _post_anyway(setup)["run"]["items"][0]
  proposal = item["verdict_proposal"]
  assert events == []
  assert proposal["state"] == "proposed" and proposal["event"] == "REQUEST_CHANGES" and proposal["moved_to"] == MOVED
  assert proposal["head_sha"] == ITEM["head_sha"]
  assert f"_Reviewed at {ITEM['head_sha'][:12]}; the pull request has since moved to {MOVED[:12]}._" in proposal["body"]
  assert f"since moved to {MOVED[:12]}" in proposal["comment_body"]
  assert "live_blocker" not in item and "held_back" not in item["post_findings"]


def test_a_submitted_held_proposal_is_posted_against_the_reviewed_commit_while_the_pr_is_ahead(setup, monkeypatch):
  events = _record_posts(monkeypatch)
  _held_review(setup, monkeypatch)
  proposal = _post_anyway(setup)["run"]["items"][0]["verdict_proposal"]
  receipt = _submit(setup, proposal["id"])["verdict"]
  (event, commit, body), = events
  assert receipt["state"] == "posted" and (event, commit) == ("REQUEST_CHANGES", ITEM["head_sha"])
  assert body == proposal["body"]


def test_an_ordinary_proposal_is_still_refused_once_the_head_has_moved(setup, monkeypatch):
  events = _record_posts(monkeypatch)
  _verdict_run(setup)
  _github(monkeypatch, head=ITEM["head_sha"])
  proposal = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]["verdict_proposal"]
  assert "moved_to" not in proposal
  _github(monkeypatch, head=MOVED)
  with pytest.raises(HTTPException) as refused:
    _submit(setup, proposal["id"])
  assert refused.value.status_code == 409 and events == []


def test_a_held_proposal_is_refused_when_it_is_no_longer_the_same_pull_request(setup, monkeypatch):
  events = _record_posts(monkeypatch)
  _held_review(setup, monkeypatch)
  proposal = _post_anyway(setup)["run"]["items"][0]["verdict_proposal"]
  def other(_gh, _cwd, path):
    if path == "user":
      return {"id": 42}
    return {**PULL, "node_id": "PR_ELSE", "head": {"sha": MOVED}} if path.endswith("/pulls/7") else REPO
  monkeypatch.setattr(domain, "read", other)
  with pytest.raises(HTTPException) as refused:
    _submit(setup, proposal["id"])
  assert refused.value.status_code == 409 and events == []


def test_post_it_anyway_refuses_a_pull_request_that_has_not_moved(setup, monkeypatch):
  _verdict_run(setup)
  _github(monkeypatch, head=ITEM["head_sha"])
  monkeypatch.setattr(domain, "current_pull", lambda *args: (_ for _ in ()).throw(HTTPException(409, "Live read unavailable.")))
  report(setup, publish=True, state="needs_you", summary="Blocking bug")
  with pytest.raises(HTTPException) as refused:
    _post_anyway(setup)
  assert refused.value.status_code == 409 and refused.value.detail == held.NOT_MOVED


def test_post_it_anyway_is_never_offered_on_your_own_pull_request(setup, monkeypatch):
  _held_review(setup, monkeypatch, author_id=42)
  with pytest.raises(HTTPException) as refused:
    _post_anyway(setup)
  assert refused.value.status_code == 409 and "author" in refused.value.detail
  assert "verdict_proposal" not in (setup[1].outcomes_json or {}).get(KEY, {})


def test_post_it_anyway_waits_while_an_agent_works_on_the_pull_request(setup, monkeypatch):
  _held_review(setup, monkeypatch)
  monkeypatch.setattr(routes, "_pr_job", lambda *args, **kwargs: {"kind": "review", "state": "running"})
  with pytest.raises(HTTPException) as refused:
    _post_anyway(setup)
  assert refused.value.status_code == 409 and "agent is already working" in refused.value.detail


def test_an_agent_cannot_press_post_it_anyway(setup, monkeypatch):
  _held_review(setup, monkeypatch)
  with pytest.raises(HTTPException) as refused:
    _post_anyway(setup, principal=setup[2])
  assert refused.value.status_code == 403


def test_a_review_with_nothing_held_back_has_nothing_to_post(setup, monkeypatch):
  _verdict_run(setup)
  _github(monkeypatch, head=ITEM["head_sha"])
  report(setup, publish=True, state="needs_you", summary="Blocking bug")  # proposed normally: nothing was refused
  with pytest.raises(HTTPException) as refused:
    _post_anyway(setup)
  assert refused.value.status_code == 409 and refused.value.detail == held.NOT_HELD


def test_the_moved_wording_names_both_commits_and_the_ordinary_wording_is_unchanged():
  outcome = {"state": "needs_you", "summary": "Bug", "head_sha": ITEM["head_sha"]}
  plain = public.review_comment_body(outcome, event="REQUEST_CHANGES")
  moved = public.review_comment_body(outcome, event="REQUEST_CHANGES", moved_to=MOVED)
  assert plain.endswith(f"_Reviewed at {ITEM['head_sha'][:12]}._")
  assert moved.endswith(f"_Reviewed at {ITEM['head_sha'][:12]}; the pull request has since moved to {MOVED[:12]}._")
  assert verdicts.propose({**outcome, "blocking": True}, {"head_sha": ITEM["head_sha"]})["event"] == "REQUEST_CHANGES"
