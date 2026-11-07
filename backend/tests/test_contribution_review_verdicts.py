"""Posting and merging in one grant, and owner-submitted formal verdicts."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain, contribution_review_public as public
from app.contribution_review_effects import DispatchResult
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import CHECKS, ITEM, PULL, REPO, report, setup  # noqa: F401

KEY = domain.key(ITEM)
PENDING = {**CHECKS, "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]}}


def _grant(setup, **options):
  db, row, _ = setup
  row.mode = "review_merge"
  row.options_json = {"post_review": True, **options}
  db.commit()
  return db, row


def _record(monkeypatch, events, *, state="confirmed"):
  def post(_gh, _cwd, target, body, event="COMMENT"):
    events.append(("post", event, target["head_sha"], body))
    if state == "confirmed":
      return DispatchResult.confirmed({"id": len(events), "url": "https://github.com/x"})
    return DispatchResult.unknown("lost response")
  monkeypatch.setattr(public, "post_review", post)
  monkeypatch.setattr(domain, "perform_merge",
                      lambda *a: events.append(("merge",)) or {"merged": True, "sha": "landed"})


def _owner(setup):
  return Principal(owner=setup[2].owner, app_id=None)


def _decide(setup, decision, proposal_id, principal=None):
  db, row, _ = setup
  body = routes.VerdictDecision(**{k: ITEM[k] for k in ("repo", "number", "head_sha")},
                                proposal_id=proposal_id, decision=decision)
  return asyncio.run(routes.decide_verdict(1, row.id, body, db, principal or _owner(setup)))


def _resumes(monkeypatch):
  calls = []
  async def start(**kwargs):
    calls.append(kwargs)
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", start)
  return calls


# Posting and merging together

def test_posting_with_merge_posts_the_comment_before_merging(setup, monkeypatch):
  _grant(setup)
  events = []
  _record(monkeypatch, events)
  item = report(setup, publish=True)["run"]["items"][0]
  assert [e[0] for e in events] == ["post", "merge"]
  assert events[0][1] == "COMMENT"
  assert item["state"] == "merged" and item["public_review"]["state"] == "posted"


def test_an_unconfirmed_comment_never_merges_and_is_not_replayed(setup, monkeypatch):
  _grant(setup)
  events = []
  _record(monkeypatch, events, state="unknown")
  run = report(setup, publish=True)["run"]
  assert run["state"] == "needs_you" and run["items"][0]["public_review"]["state"] == "unknown"
  report(setup, publish=True)
  assert [e[0] for e in events] == ["post"]


def test_pending_checks_then_identical_retry_merges_without_reposting(setup, monkeypatch):
  db, row = _grant(setup)
  events = []
  _record(monkeypatch, events)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: PENDING)
  assert report(setup, publish=True)["run"]["items"][0]["state"] == "needs_you"
  monkeypatch.setattr(domain, "pull_checks", lambda *a: CHECKS)
  item = report(setup, publish=True)["run"]["items"][0]
  assert [e[0] for e in events] == ["post", "merge"] and item["state"] == "merged"
  # Changed judgment after posting is still refused.
  with pytest.raises(HTTPException) as changed:
    report(setup, publish=True, summary="Different verdict")
  assert changed.value.status_code == 409


def test_findings_with_merge_are_posted_but_never_merged(setup, monkeypatch):
  _grant(setup)
  events = []
  _record(monkeypatch, events)
  item = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]
  assert [e[0] for e in events] == ["post"] and item["state"] == "needs_you"


# Proposed verdicts

def test_a_verdict_is_only_proposed_and_blocks_the_merge(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  run = report(setup, publish=True)["run"]
  item = run["items"][0]
  assert events == []
  assert run["state"] == "needs_you" and item["state"] == "verdict_pending"
  proposal = item["verdict_proposal"]
  assert proposal["event"] == "APPROVE" and proposal["state"] == "proposed"
  assert "Approved" in proposal["body"] and "Full diff reviewed" in proposal["body"]
  report(setup, publish=True)
  assert events == []


def test_owner_submit_posts_the_exact_proposal_then_the_run_merges(setup, monkeypatch):
  db, row = _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  resumed = _resumes(monkeypatch)
  proposal = report(setup, publish=True)["run"]["items"][0]["verdict_proposal"]
  result = _decide(setup, "submit", proposal["id"])
  assert result["verdict"]["state"] == "posted" and result["merge_resumed"] is True
  (kind, event, head, body), = events
  assert (kind, event, head) == ("post", "APPROVE", ITEM["head_sha"])
  assert body.startswith(proposal["body"]) and "mobius-review:" in body
  assert resumed and resumed[0]["chat_id"] == row.chat_id
  item = report(setup, publish=True)["run"]["items"][0]
  assert [e[0] for e in events] == ["post", "merge"] and item["state"] == "merged"
  with pytest.raises(HTTPException) as again:
    _decide(setup, "submit", proposal["id"])
  assert again.value.status_code == 409 and len(events) == 2


def test_the_review_agent_cannot_submit_its_own_proposal(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  proposal = report(setup, publish=True)["run"]["items"][0]["verdict_proposal"]
  with pytest.raises(HTTPException) as refused:
    _decide(setup, "submit", proposal["id"], principal=setup[2])
  assert refused.value.status_code == 403 and events == []


def test_discarded_verdict_posts_and_merges_nothing(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  proposal = report(setup, publish=True)["run"]["items"][0]["verdict_proposal"]
  run = _decide(setup, "discard", proposal["id"])["run"]
  assert run["state"] == "needs_you" and run["items"][0]["verdict_proposal"]["state"] == "discarded"
  report(setup, publish=True)
  assert events == []


def test_request_changes_is_submitted_but_never_merges(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  resumed = _resumes(monkeypatch)
  proposal = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]["verdict_proposal"]
  assert proposal["event"] == "REQUEST_CHANGES"
  result = _decide(setup, "submit", proposal["id"])
  assert result["merge_resumed"] is None and resumed == []
  assert [e[:2] for e in events] == [("post", "REQUEST_CHANGES")]


def test_own_pull_request_falls_back_to_a_comment(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42}}))
  item = report(setup, publish=True)["run"]["items"][0]
  assert [e[:2] for e in events] == [("post", "COMMENT"), ("merge",)]
  assert "verdict_proposal" not in item


def test_a_stale_or_stopped_decision_posts_nothing(setup, monkeypatch):
  db, row = _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  proposal = report(setup, publish=True)["run"]["items"][0]["verdict_proposal"]
  with pytest.raises(HTTPException) as stale:
    _decide(setup, "submit", "0" * 32)
  assert stale.value.status_code == 409
  row.outcomes_json = {**row.outcomes_json, routes.REVIEW_STOP_KEY: {"state": "requested"}}
  db.commit()
  with pytest.raises(HTTPException) as stopped:
    _decide(setup, "submit", proposal["id"])
  assert stopped.value.status_code == 409 and events == []


def test_a_writer_that_never_saw_the_proposal_cannot_drop_it(setup, monkeypatch):
  db, row = _grant(setup, propose_verdict=True)
  _record(monkeypatch, [])
  report(setup, publish=True)
  db.refresh(row)
  stale = {k: v for k, v in row.outcomes_json[KEY].items() if k != "verdict_proposal"}
  domain.save_effect_outcome(db, row, KEY, {**stale, "state": "needs_you"})
  db.refresh(row)
  assert row.outcomes_json[KEY]["verdict_proposal"]["state"] == "proposed"


def test_verdict_option_freezes_its_own_instructions():
  from app import contribution_review_presets as presets
  choice = {"provider": "codex", "model": "m", "effort": None}
  frozen = presets.resolve_options(None, {"post_review": True, "propose_verdict": True},
                                   choice=choice, mode="review_merge")
  assert frozen["propose_verdict"] is True and "Only the owner can submit" in frozen["mandatory_instructions"]
  with pytest.raises(ValueError):
    presets.resolve_options(None, {"propose_verdict": True}, choice=choice, mode="review")
  with pytest.raises(ValueError):
    presets.resolve_options(None, {"post_review": True}, choice=choice, mode="review_fix_merge")
