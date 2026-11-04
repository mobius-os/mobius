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


def test_own_pull_request_is_never_reviewed_in_public(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42}}))
  run = report(setup, publish=True)["run"]
  item = run["items"][0]
  assert events == []
  assert item["state"] == "all_clear" and "verdict_proposal" not in item and "public_review" not in item


def test_own_pull_request_findings_stay_private_even_for_a_plain_comment_grant(setup, monkeypatch):
  _grant(setup)
  events = []
  _record(monkeypatch, events)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42}}))
  item = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]
  assert events == []
  assert item["state"] == "needs_you" and item["summary"] == "Blocking bug"
  assert "public_review" not in item and "verdict_proposal" not in item


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


def _unconfirmed_verdict(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  events = []
  _record(monkeypatch, events, state="unknown")
  _resumes(monkeypatch)
  proposal = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]["verdict_proposal"]
  _decide(setup, "submit", proposal["id"])
  assert _observe_saved(setup)["state"] == "unknown"
  return events, proposal


def _observe_saved(setup):
  db, row, _ = setup
  db.refresh(row)
  return row.outcomes_json[KEY]["verdict_proposal"]


def _observe(setup):
  db, row, _ = setup
  return asyncio.run(routes.observe_review(1, row.id, db, _owner(setup)))["run"]["items"][0]["verdict_proposal"]


def _look(monkeypatch, review=None, complete=True):
  """What GitHub's reviews list showed this observation: (exact review or None, whole list read)."""
  seen = []
  def find(gh, cwd, target, receipt, actor_id, event="COMMENT"):
    seen.append((target["head_sha"], receipt["marker"], event))
    return review, complete
  monkeypatch.setattr(public, "find_review", find)
  return seen


def _hours_after_submit(setup, hours):
  """Age the saved attempt: the dispatch window is measured from when it was armed."""
  from datetime import datetime, timedelta, timezone
  db, row, _ = setup
  item = {**row.outcomes_json[KEY]}
  armed = datetime.now(timezone.utc) - timedelta(hours=hours)
  item["verdict_proposal"] = {**item["verdict_proposal"], "submitted_at": armed.isoformat().replace("+00:00", "Z")}
  assert domain._cas_outcome(db, row, KEY, item), 'a test clock edit, not a product write'


def test_an_unconfirmed_verdict_that_github_shows_is_settled_as_posted_without_a_second_post(setup, monkeypatch):
  events, proposal = _unconfirmed_verdict(setup, monkeypatch)
  review = {"id": 77, "html_url": "https://github.com/x/pull/7#pullrequestreview-77"}
  seen = _look(monkeypatch, review)
  settled = _observe(setup)
  assert (settled["state"], settled["review_id"], settled["id"]) == ("posted", 77, proposal["id"])
  assert settled["url"].endswith("pullrequestreview-77") and "read-only" in settled["summary"]
  assert seen[0][0] == ITEM["head_sha"] and seen[0][2] == "REQUEST_CHANGES" and "mobius-review:" in seen[0][1]
  assert [e[0] for e in events] == ["post"], "observing never posts again"


def test_an_unconfirmed_verdict_missing_from_github_is_held_until_the_dispatch_window_has_passed(setup, monkeypatch):
  events, _ = _unconfirmed_verdict(setup, monkeypatch)
  _look(monkeypatch)
  assert _observe(setup)["state"] == "unknown", "just submitted: a request could still land"
  assert _observe(setup)["state"] == "unknown"
  assert [e[0] for e in events] == ["post"]


def test_a_verdict_absent_from_github_an_hour_on_is_released_as_never_sent_and_the_card_returns_to_its_findings(setup, monkeypatch):
  events, proposal = _unconfirmed_verdict(setup, monkeypatch)
  _hours_after_submit(setup, 2)
  _look(monkeypatch)
  released = _observe(setup)
  assert (released["state"], released["id"]) == ("not_dispatched", proposal["id"])
  assert released["summary"] == "This verdict was never sent to GitHub, so nothing is waiting on you."
  db, row, _ = setup
  run = routes.get_review(1, row.id, db, _owner(setup))["run"]
  assert run["items"][0]["state"] == "needs_you", "findings stand; no verdict banner is left waiting"
  assert [e[0] for e in events] == ["post"], "releasing never sends"
  with pytest.raises(HTTPException) as again:
    _decide(setup, "submit", proposal["id"])
  assert again.value.status_code == 409, "a released verdict cannot be submitted either"


def test_the_owner_can_dismiss_an_unconfirmed_verdict_and_nothing_is_sent(setup, monkeypatch):
  events, proposal = _unconfirmed_verdict(setup, monkeypatch)
  db, row, _ = setup
  run = _decide(setup, "dismiss", proposal["id"])["run"]
  saved = row.outcomes_json[KEY]["verdict_proposal"]
  assert (saved["state"], saved["summary"]) == ("not_dispatched", "You dismissed this verdict. GitHub never confirmed it and nothing more will be sent.")
  assert run["items"][0]["state"] == "needs_you"
  assert [e[0] for e in events] == ["post"]
  with pytest.raises(HTTPException) as again:
    _decide(setup, "dismiss", proposal["id"])
  assert again.value.status_code == 409


def test_a_stopped_review_can_still_have_its_unconfirmed_verdict_dismissed(setup, monkeypatch):
  _, proposal = _unconfirmed_verdict(setup, monkeypatch)
  db, row, _ = setup
  domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  assert _decide(setup, "dismiss", proposal["id"])["run"]["items"][0]["state"] == "needs_you"


def test_the_review_agent_cannot_dismiss_a_verdict(setup, monkeypatch):
  _, proposal = _unconfirmed_verdict(setup, monkeypatch)
  with pytest.raises(HTTPException) as agent:
    _decide(setup, "dismiss", proposal["id"], principal=setup[2])
  assert agent.value.status_code == 403
  assert _observe_saved(setup)["state"] == "unknown"


def test_a_verdict_still_waiting_for_the_owner_is_discarded_not_dismissed(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  _record(monkeypatch, [])
  proposal = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]["verdict_proposal"]
  with pytest.raises(HTTPException) as refused:
    _decide(setup, "dismiss", proposal["id"])
  assert refused.value.status_code == 409
  db, row, _ = setup
  db.refresh(row)
  assert row.outcomes_json[KEY]["verdict_proposal"]["state"] == "proposed"


def test_a_failed_or_partial_read_of_github_never_releases_an_unconfirmed_verdict(setup, monkeypatch):
  _unconfirmed_verdict(setup, monkeypatch)
  _hours_after_submit(setup, 2)
  _look(monkeypatch, complete=False)
  assert _observe(setup)["state"] == "unknown", "no proof of absence: a failed read or a capped scan"


def test_a_confirmed_verdict_is_never_rewound_by_a_later_observation(setup, monkeypatch):
  _unconfirmed_verdict(setup, monkeypatch)
  _look(monkeypatch, {"id": 5, "html_url": None})
  assert _observe(setup)["state"] == "posted"
  monkeypatch.setattr(public, "find_review", lambda *a, **k: pytest.fail("a posted verdict is not observed again"))
  assert _observe(setup)["state"] == "posted"


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
