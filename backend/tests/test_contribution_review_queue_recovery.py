"""Confirmed queue removal permits only a newly reviewed repair successor."""
import asyncio
import json
import subprocess
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import contribution_review_runs as domain, contribution_review_repairs as repairs, models
from app.contribution_review_effects import DispatchResult, NotDispatched
from app.routes import contribution_reviews as routes
from app.deps import Principal
from app.chat_writer import create_chat
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, CHECKS, report
from tests.test_contribution_review_workflows import takeover, configure_repair, publish_body, checkout_body, overlapping_grant, NEW
from tests.test_contribution_review_queue import queue_timeline, saved_queue_receipt, queue_graphql, QUEUE_TIME, REMOVED_TIME
CURRENT_PULL = domain.current_pull


def removed_takeover(setup, monkeypatch, *, legacy=False):
  db, row, principal = takeover(setup, monkeypatch)
  node = queue_timeline()
  queue_graphql(monkeypatch, node)
  def read_live(gh, cwd, endpoint):
    if endpoint == "user": return {"id": 42}
    if endpoint == "repos/example/project": return REPO
    assert endpoint == "repos/example/project/pulls/7"
    return {**PULL, "node_id": node["id"], "state": node["state"].lower(),
      "merged": node["merged"], "head": {"sha": node["headRefOid"],
        "ref": "topic", "repo": {"id": 1, "full_name": "example/project"}}}
  monkeypatch.setattr(domain, "read", read_live)
  monkeypatch.setattr(domain, "current_pull", CURRENT_PULL)
  saved = saved_queue_receipt(summary="Original independent review", tests="Original passing tests",
    review_run_id=principal.run_id, scope=sorted(routes.SCOPE), tests_passed=True,
    reviewed_base_sha=BASE, independent_receipt_id="old-clear",
    independent_reviews=[{"id": "old-clear", "head_sha": SHA, "base_sha": BASE,
      "review_run_id": "original-independent-run", "state": "all_clear", "tests_passed": True}])
  if legacy:
    saved.pop("queue_enqueued_at")
  domain.save_outcome(db, row, domain.key(ITEM), saved)
  monkeypatch.setattr(domain, "enqueue", lambda *a: pytest.fail("same-head enqueue"))
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("same-head merge"))
  return db, row, principal, node


def observe_removed(db, row, principal):
  return asyncio.run(routes.observe_review(1, row.id, db,
    Principal(owner=principal.owner, app_id=None)))["run"]["items"][0]


def prepare_removed(setup, monkeypatch):
  db, row, principal, node = removed_takeover(setup, monkeypatch)
  observe_removed(db, row, principal)
  checkout = {"checkout": "/fake/server-repair", "initial_head_sha": SHA,
    "allowed_files": ["owned.py"]}
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: checkout)
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **kw: {**checkout,
    "head_sha": NEW, "files": ["owned.py"], "diff_sha256": "d" * 64})
  asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  return db, row, principal, node


@pytest.mark.parametrize("legacy", [False, True])
def test_confirmed_removal_observation_permits_checkout_not_same_head_retry(setup, monkeypatch, legacy):
  db, row, principal, _ = removed_takeover(setup, monkeypatch, legacy=legacy)
  prepared = []
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: prepared.append(1) or {
    "checkout": "/fake/server-repair", "initial_head_sha": SHA})
  # An entry's absence alone is not permission; first persist the proof.
  with pytest.raises(HTTPException):
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  item = observe_removed(db, row, principal)
  assert item["state"] == "needs_you" and item["queue_removal"]["removed_event_id"] == "removed-1"
  history = item["merge_attempts"]
  assert history[0]["summary"] == "Original independent review"
  assert history[0]["review_run_id"] == principal.run_id
  assert history[0]["independent_receipt_id"] == "old-clear"
  assert history[0]["tests"] == "Original passing tests"
  assert "PRIVATE" not in str(item) and "failed_checks" not in str(item)
  assert report(setup, tests_passed=True, reviewed_base_sha=BASE,
    independent_receipt_id="old-clear")["run"]["items"][0]["state"] == "needs_you"
  result = asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert result["checkout"]["initial_head_sha"] == SHA and prepared == [1]
  assert row.outcomes_json[domain.key(ITEM)]["merge_attempted"] is True
  assert row.outcomes_json[domain.key(ITEM)]["merge_attempts"] == history


def test_transient_queue_history_failure_does_not_erase_saved_removal_proof(setup, monkeypatch):
  db, row, principal, _ = removed_takeover(setup, monkeypatch)
  proven = observe_removed(db, row, principal)
  assert proven["state"] == "needs_you" and proven["queue_removal"]
  saved = dict(row.outcomes_json[domain.key(ITEM)])
  def timed_out(*args):
    raise subprocess.TimeoutExpired(["gh", "api", "graphql"], 30)
  monkeypatch.setattr(routes, "_gh", timed_out)
  observed = observe_removed(db, row, principal)
  assert observed["state"] == "needs_you"
  assert observed["queue_removal"] == proven["queue_removal"]
  assert row.outcomes_json[domain.key(ITEM)] == saved


@pytest.mark.parametrize("case", ["unknown", "missing", "ambiguous", "stale", "readded",
  "merged", "live_queue", "changed_head", "wrong_pr", "incomplete"])
def test_queue_recovery_route_refuses_unproven_removal(setup, monkeypatch, case):
  db, row, principal, node = removed_takeover(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(ITEM)]
  if case == "unknown":
    previous = {k: v for k, v in previous.items() if k not in {"queue_entry_id", "queue_enqueued_at"}}
    previous["state"] = "merge_unknown"
  elif case == "missing": node["timelineItems"]["nodes"] = []
  elif case == "ambiguous":
    previous.pop("queue_enqueued_at")
    node["timelineItems"]["nodes"] *= 2
  elif case == "stale": previous["queue_enqueued_at"] = "2026-10-07T19:00:00Z"
  elif case == "readded":
    node["timelineItems"]["nodes"].append({"__typename": "AddedToMergeQueueEvent",
      "id": "later", "createdAt": "2026-10-07T21:00:00Z"})
  elif case == "merged":
    node["merged"] = True
    monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "merged": True}))
  elif case == "live_queue": node["mergeQueueEntry"] = {"id": "entry-2"}
  elif case == "changed_head": node["headRefOid"] = NEW
  elif case == "wrong_pr": node["id"] = "PR_other"
  elif case == "incomplete": node["timelineItems"]["pageInfo"]["hasPreviousPage"] = True
  domain.save_outcome(db, row, domain.key(ITEM), previous)
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("unproven repair"))
  item = observe_removed(db, row, principal)
  assert not item.get("queue_removal")
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert error.value.status_code == 409
  assert row.outcomes_json[domain.key(ITEM)]["merge_attempted"] is True


def test_stopped_run_only_observes_removal_without_starting_execution(setup, monkeypatch):
  db, row, principal, _ = removed_takeover(setup, monkeypatch)
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  async def no_start(**kw):
    pytest.fail("observation cannot start execution")
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", no_start)
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("stopped checkout"))
  item = observe_removed(db, row, principal)
  assert item["queue_removal"] and item["merge_attempted"] is True
  assert db.get(models.ChatRun, principal.run_id).status == "stopped"
  with pytest.raises(HTTPException):
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))


@pytest.mark.parametrize("gate", ["checkout", "publish", "final_push"])
@pytest.mark.parametrize("change", ["requeue", "head", "pr", "merged"])
def test_recovery_revalidates_at_every_action_boundary(setup, monkeypatch, gate, change):
  db, row, principal, node = prepare_removed(setup, monkeypatch)
  history = row.outcomes_json[domain.key(ITEM)]["merge_attempts"]
  def drift():
    if change == "requeue": node["mergeQueueEntry"] = {"id": "external"}
    elif change == "head": node["headRefOid"] = NEW
    elif change == "pr": node["id"] = "PR_other"
    else: node["merged"] = True
  if gate == "final_push":
    def push(*a, before_push=None):
      drift()
      try:
        before_push()
      except NotDispatched as exc:
        return DispatchResult.not_dispatched(str(exc))
      pytest.fail("stale proof reached public push")
    monkeypatch.setattr(repairs, "push_repair", push)
    item = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["run"]["items"][0]
    assert not item.get("repair_attempts")
  else:
    drift()
    monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("stale proof push"))
    action = routes.repair_checkout if gate == "checkout" else routes.publish_repair
    body = checkout_body() if gate == "checkout" else publish_body()
    with pytest.raises(HTTPException) as error:
      asyncio.run(action(1, row.id, body, db, principal))
    assert error.value.status_code == 409
  saved = row.outcomes_json[domain.key(ITEM)]
  assert saved["merge_attempts"] == history and saved["merge_attempted"] is True
  assert not saved.get("successor")


@pytest.mark.parametrize("finish", ["success", "lost_then_publish", "lost_then_observe", "failed"])
def test_removed_attempt_history_survives_push_and_confirmed_successor(setup, monkeypatch, finish):
  db, row, principal, node = prepare_removed(setup, monkeypatch)
  history = row.outcomes_json[domain.key(ITEM)]["merge_attempts"]
  pushes = []
  def push(*a, before_push=None):
    before_push()
    pushes.append(1)
    if finish != "success": return DispatchResult.unknown("lost push response")
    return DispatchResult.confirmed({})
  monkeypatch.setattr(repairs, "push_repair", push)
  item = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["run"]["items"][0]
  if finish != "success":
    assert item["merge_attempted"] is True and item["queue_removal"]
    assert item["merge_attempts"] == history
    if finish == "failed":
      def unchanged(*a):
        if a[2]["head_sha"] != SHA: raise HTTPException(409, "Not pushed")
        return REPO, PULL
      monkeypatch.setattr(domain, "current_pull", unchanged)
      item = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["run"]["items"][0]
      assert not item.get("successor") and item["merge_attempts"] == history
      assert pushes == [1]
      return
    monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "head": {"sha": NEW}}))
    if finish == "lost_then_publish":
      item = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["run"]["items"][0]
    else:
      db.get(models.ChatRun, principal.run_id).status = "stopped"
      db.commit()
      item = observe_removed(db, row, principal)
      assert db.get(models.ChatRun, principal.run_id).status == "stopped"
  assert item["head_sha"] == NEW and item["state"] == "reviewing"
  assert not item.get("merge_attempted") and not item.get("queue_entry_id")
  assert not item.get("queue_removal") and not item.get("tests_passed")
  assert item["merge_attempts"] == history and pushes == [1]
  assert row.targets_json[0]["head_sha"] == SHA
  if finish == "lost_then_observe":
    db.get(models.ChatRun, principal.run_id).status = "running"
    db.commit()
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "head": {"sha": NEW}}))
  with pytest.raises(HTTPException) as error:
    report(setup, head_sha=NEW, tests_passed=True, reviewed_base_sha=BASE,
      independent_receipt_id="old-clear")
  assert error.value.status_code == 422
  with pytest.raises(HTTPException):
    report(setup, head_sha=SHA, tests_passed=True, reviewed_base_sha=BASE,
      independent_receipt_id="old-clear")


def test_removed_repair_cannot_publish_unchanged_head(setup, monkeypatch):
  db, row, principal, _ = prepare_removed(setup, monkeypatch)
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **kw: {"head_sha": SHA})
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("same-head repair"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409


def test_archived_removed_head_cannot_merge_under_another_grant_after_claim_release(setup, monkeypatch):
  from app import agent_work_claims
  db, row, principal, _ = prepare_removed(setup, monkeypatch)
  work_key = domain.merge_work_key(TARGET)
  agent_work_claims.claim_work(db, owner_id=row.owner_id, chat_id=row.chat_id,
    run_id=principal.run_id, work_key=work_key, summary="Prior attempt")
  agent_work_claims.finish_work(db, owner_id=row.owner_id, chat_id=row.chat_id,
    work_key=work_key, outcome="Queue removed", release=True)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: DispatchResult.confirmed({}))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  second = models.ContributionReviewRun(id="old-head-grant", app_id=1, owner_id=row.owner_id,
    request_id="old-head-request", mode="review_merge", targets_json=[TARGET],
    outcomes_json={}, chat_id=row.chat_id, github_actor_id="42", app_nonce="nonce")
  db.add(second)
  db.commit()
  item = report((db, second, principal))["run"]["items"][0]
  assert item["state"] == "needs_you" and item["review_selection_id"] == row.id
