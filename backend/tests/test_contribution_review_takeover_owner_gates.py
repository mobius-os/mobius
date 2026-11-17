"""A takeover proposes; the owner pushes, merges and changes the PR's status with their own taps."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_repairs as repairs, contribution_review_runs as domain
from app.contribution_review_effects import DispatchResult
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, BASE, PULL, REPO, SHA, report  # noqa: F401  (fixture)
from tests.test_contribution_review_workflows import NEW, configure_repair, publish_body


@pytest.fixture
def proposed(setup, monkeypatch):
  """A takeover whose agent proposed one repair; the owner has not decided yet."""
  db, row, principal = configure_repair(setup, monkeypatch)
  pushes, resumed = [], []
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pushes.append(a[4]["head_sha"]) or DispatchResult.confirmed({}))
  monkeypatch.setattr(repairs, "discard_checkout", lambda path: None)
  async def resume(**kwargs):
    resumed.append(kwargs["content"])
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", resume)
  proposal = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["proposed"]
  return db, row, principal, proposal, pushes, resumed


def decide(db, row, principal, proposal, decision="push", head=SHA):
  body = routes.RepairDecision(repo=ITEM["repo"], number=ITEM["number"], head_sha=head,
                               attempt_id=proposal["id"], decision=decision)
  return asyncio.run(routes.decide_repair(1, row.id, body, db, principal))


def owner_of(principal):
  return Principal(owner=principal.owner, app_id=None)


def attempts(row):
  return row.outcomes_json[domain.key(ITEM)].get("repair_attempts", [])


def test_takeover_push_waits_for_owner_decision(proposed):
  db, row, principal, proposal, pushes, resumed = proposed
  assert pushes == [] and resumed == []
  assert {key: proposal[key] for key in ("state", "from_sha", "head_sha", "base_sha", "summary", "tests")} == {
    "state": "proposed", "from_sha": SHA, "head_sha": NEW, "base_sha": BASE,
    "summary": "Fixed the scoped bug", "tests": "Focused tests passed"}
  assert proposal["files"] == ["owned.py"] and proposal["diff_sha256"] and proposal["author_run_id"] == principal.run_id
  item = row.outcomes_json[domain.key(ITEM)]
  assert item["state"] == "needs_you" and item["summary"] == routes.PUSH_DECISION_SUMMARY
  # The agent asking again only learns that the owner decides.
  again = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert again["blocked"] == routes.AWAITING_PUSH_DECISION and len(attempts(row)) == 1
  result = decide(db, row, owner_of(principal), proposal)
  assert pushes == [NEW] and result["repair"]["state"] == "pushed" and result["resumed"] is True
  assert domain.effective_target(row, row.targets_json[0])["head_sha"] == NEW
  assert "fresh independent review" in resumed[0] and "Never merge" in resumed[0]
  with pytest.raises(HTTPException) as twice:
    decide(db, row, owner_of(principal), proposal)
  assert twice.value.status_code == 409 and pushes == [NEW]


def test_an_agent_can_never_decide_its_own_push(proposed):
  db, row, principal, proposal, pushes, _ = proposed
  with pytest.raises(HTTPException) as refused:
    decide(db, row, principal, proposal)
  assert refused.value.status_code == 403 and pushes == []
  assert attempts(row)[0]["state"] == "proposed"


def test_owner_discard_pushes_nothing_and_tells_the_agent(proposed):
  db, row, principal, proposal, pushes, resumed = proposed
  result = decide(db, row, owner_of(principal), proposal, "discard")
  assert pushes == [] and result["repair"]["state"] == "discarded"
  assert "discarded" in resumed[0] and "Do not push" in resumed[0]
  # A discarded proposal is not a repair round and not part of the successor chain.
  domain.require_repair_round_available(row, attempts(row))
  assert domain.effective_target(row, row.targets_json[0])["head_sha"] == SHA
  assert "proposed" in asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))


def test_the_push_decision_asks_the_pr_machine_again(proposed, monkeypatch):
  db, row, principal, proposal, pushes, resumed = proposed
  monkeypatch.setattr(domain, "current_pull", lambda *a: ({**REPO, "permissions": {"push": True}}, PULL))
  with pytest.raises(HTTPException) as refused:
    decide(db, row, owner_of(principal), proposal)
  assert (refused.value.status_code, refused.value.detail) == (409, "Only maintainers can take over someone else’s PR.")
  assert pushes == [] and resumed == [] and attempts(row)[0]["state"] == "proposed"


def test_a_checkout_changed_after_the_proposal_is_never_pushed(proposed, monkeypatch):
  db, row, principal, proposal, pushes, _ = proposed
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **kw: {
    "checkout": "/fake/server-repair", "initial_head_sha": SHA, "allowed_files": ["owned.py"],
    "head_sha": "9" * 40, "files": ["owned.py"], "diff_sha256": "e" * 64})
  with pytest.raises(HTTPException) as refused:
    decide(db, row, owner_of(principal), proposal)
  assert refused.value.status_code == 409 and pushes == []


def test_a_stopped_takeover_takes_no_decision(proposed):
  db, row, principal, proposal, pushes, resumed = proposed
  domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  with pytest.raises(HTTPException) as refused:
    decide(db, row, owner_of(principal), proposal)
  assert refused.value.status_code == 409 and pushes == [] and resumed == []


def test_takeover_all_clear_never_merges_and_waits_for_the_owners_merge(setup, monkeypatch):
  from tests.test_contribution_review_readiness import ready_takeover
  db, row, _ = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, PULL))
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("a takeover merged"))
  monkeypatch.setattr(domain, "enqueue", lambda *a: pytest.fail("a takeover enqueued"))
  item = report(setup, tests_passed=True, reviewed_base_sha=BASE,
                independent_receipt_id="independent-clear")["run"]["items"][0]
  assert item["state"] == "all_clear" and routes.READY_FOR_YOUR_MERGE in item["summary"]
  assert not item.get("merge_attempted") and not item.get("ready_attempt")


def test_takeover_brief_carries_the_reason_and_leaves_every_push_merge_and_status_to_the_owner(setup, monkeypatch):
  from tests.test_contribution_review_workflows import takeover
  db, row, _ = takeover(setup, monkeypatch)
  row.options_json = {**row.options_json, "takeover_reason": "The author asked a maintainer to finish it."}
  row.targets_json = [{**row.targets_json[0], "role": "reviewer"}]
  db.commit()
  brief = domain.takeover_brief(row)
  assert "The author asked a maintainer to finish it." in brief and "never post it yourself" in brief
  assert "PROPOSAL" in brief and "never merges" in brief.replace("\n", " ")
  assert "/ready" not in brief and "attempts exact normal merge" not in brief
  row.targets_json = [{**row.targets_json[0], "role": "author"}]
  db.commit()
  assert domain.takeover_brief(row).startswith("Finish the owner's own named PRs")
