"""Readiness receipts that older builds armed: settled only by observation, fencing until then.

Agents no longer mark drafts ready (only the owner's own tap opens a PR for
review, contribution_pull_status), but an owner's database can still hold an
attempt an older build armed. It must stay visible, fence every other public
action until observed, and never be replayed.
"""
import asyncio
import time
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from app import contribution_review_runs as domain, contribution_review_repairs as repairs, models
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, SHA, BASE, REPO, PULL, report
from tests.test_contribution_review_workflows import (takeover, configure_repair, publish_body, checkout_body,
  overlapping_grant, propose_and_push, NEW)


def ready_takeover(setup, monkeypatch):
  """A takeover of a draft whose independent review is clear, as an older build left it."""
  db, row, principal = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "reviewing",
    "independent_reviews": [{"id": "independent-clear", "head_sha": SHA,
      "base_sha": BASE, "state": "all_clear", "tests_passed": True}]})
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": True}))
  return db, row, principal


def iso(timestamp):
  return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


# Armed long enough ago that every request for the attempt has been evaluated.
CLOSED = iso(time.time() - domain.MERGE_DISPATCH_WINDOW_SECONDS - 60)


def moved_head_reads(monkeypatch, draft=False):
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "head moved")))
  def read(_gh, _cwd, path):
    if path == f"repos/{ITEM['repo']}":
      return REPO
    if path == f"repos/{ITEM['repo']}/pulls/{ITEM['number']}":
      return {**PULL, "head": {"sha": NEW}, "draft": draft}
    pytest.fail("No actor read is needed")
  monkeypatch.setattr(domain, "read", read)


def successor_grant(db, row):
  grant = overlapping_grant(db, row)
  grant.targets_json = [{**grant.targets_json[0], "head_sha": NEW}]
  db.commit()
  return grant, grant.targets_json[0]


def test_uncertain_readiness_blocks_repair_and_merge_until_observed(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("repair after unknown ready"))
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("merge after unknown ready"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert error.value.status_code == 409
  with pytest.raises(HTTPException) as error:
    propose_and_push(db, row, principal, publish_body())
  assert error.value.status_code == 409
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE,
      independent_receipt_id="independent-clear")
  assert error.value.status_code == 409
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  assert asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"


@pytest.mark.parametrize("attempt_state, item_state, run_state", [
  ("attempting", "marking_ready", "marking_ready"),
  ("unknown", "ready_unknown", "needs_you"),
])
def test_ready_attempt_projection_keeps_private_review_evidence(setup, monkeypatch,
                                                                  attempt_state, item_state, run_state):
  db, row, _ = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": attempt_state, "head_sha": SHA, "base_sha": BASE}})
  result = domain.view(row)
  assert result["state"] == run_state
  assert result["items"][0]["state"] == item_state
  assert result["items"][0]["independent_reviews"] == prior["independent_reviews"]
  assert row.outcomes_json[domain.key(ITEM)]["state"] == "reviewing"
  if attempt_state == "unknown":
    assert "mark it ready in GitHub" in result["items"][0]["summary"]
    assert "different head" in result["items"][0]["summary"]
    assert row.outcomes_json[domain.key(ITEM)].get("summary") == prior.get("summary")


@pytest.mark.parametrize("changed", ["actor", "rights"])
def test_lost_ready_response_confirms_same_head_without_current_authority(setup, monkeypatch, changed):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM),
    {"id": "old-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE})
  repo = {**REPO, "permissions": {"push": False}} if changed == "rights" else REPO
  monkeypatch.setattr(domain, "current_pull", lambda *a: (repo, {**PULL, "draft": False}))
  if changed == "actor":
    monkeypatch.setattr(domain, "read", lambda *a: (_ for _ in ()).throw(OSError("actor changed")))
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["items"][0]["ready_attempt"]["state"] == "ready"


@pytest.mark.parametrize("state", ["attempting", "unknown"])
@pytest.mark.parametrize("invalid", ["missing_repo", "wrong_pr", "missing_head", "malformed_head"])
def test_moved_head_requires_safe_exact_identity_before_retirement(setup, monkeypatch, state, invalid):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM), {"id": "old-ready", "state": state,
    "head_sha": SHA, "base_sha": BASE, "armed_at": CLOSED})
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "head moved")))
  def read(_gh, _cwd, path):
    if path == f"repos/{ITEM['repo']}":
      return {} if invalid == "missing_repo" else REPO
    if path == f"repos/{ITEM['repo']}/pulls/{ITEM['number']}":
      return {**PULL, "node_id": "other" if invalid == "wrong_pr" else PULL["node_id"],
        "head": {} if invalid == "missing_head" else {"sha": "bad" if invalid == "malformed_head" else NEW}}
    pytest.fail("unexpected read")
  monkeypatch.setattr(domain, "read", read)
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]
  assert observed["ready_attempt"]["state"] == ("unknown" if state == "attempting" else state)
  successor, new_head = successor_grant(db, row)
  with pytest.raises(HTTPException):
    domain.require_public_transition_clear(db, successor, new_head)


def test_moved_head_attempting_must_first_surface_unknown_then_retire(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM), {"id": "old-ready", "state": "attempting",
    "head_sha": SHA, "base_sha": BASE, "armed_at": CLOSED})
  moved_head_reads(monkeypatch, draft=True)
  first = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]
  assert first["ready_attempt"]["state"] == "unknown"
  successor, new_head = successor_grant(db, row)
  with pytest.raises(HTTPException):
    domain.require_public_transition_clear(db, successor, new_head)
  second = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]
  assert second["ready_attempt"]["state"] == "unattributable"
  assert [a["state"] for a in second["ready_audit"]] == ["attempting", "unknown", "unattributable"]
  domain.require_public_transition_clear(db, successor, new_head)


def test_legacy_ready_receipt_without_arm_time_waits_a_window_from_process_load(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM), {"id": "old-ready", "state": "unknown",
    "head_sha": SHA, "base_sha": BASE})
  moved_head_reads(monkeypatch)
  monkeypatch.setattr(domain, "_LOADED_AT", time.time())
  asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "unknown"
  monkeypatch.setattr(domain, "_LOADED_AT", time.time() - domain.MERGE_DISPATCH_WINDOW_SECONDS - 1)
  asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "unattributable"


def test_observe_confirms_readiness_at_the_attempts_saved_head(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  # An effective successor head must not replace the receipt's own head.
  domain.save_outcome(db, row, domain.key(ITEM), {**prior, "successor": {"head_sha": NEW, "base_sha": BASE},
    "repair_attempts": [{"state": "pushed", "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}],
    "ready_attempt": {"id": "old-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  heads = []
  def pull(_gh, _cwd, target):
    heads.append(target["head_sha"])
    return REPO, {**PULL, "draft": False}
  monkeypatch.setattr(domain, "current_pull", pull)
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]
  assert heads == [SHA]
  assert observed["ready_attempt"]["state"] == "ready"


def test_leftover_attempting_readiness_escalates_to_needs_you_on_observe(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "attempting", "head_sha": SHA, "base_sha": BASE}})
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["state"] == "needs_you"
  assert observed["items"][0]["state"] == "ready_unknown"
  assert observed["items"][0]["ready_attempt"]["state"] == "unknown"


def test_observe_actor_read_failure_does_not_erase_same_head_ready_fact(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  def broken(*a):
    raise OSError("gh unavailable")
  monkeypatch.setattr(domain, "read", broken)
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["items"][0]["ready_attempt"]["state"] == "ready"


@pytest.mark.parametrize("action", ["merge", "repair"])
def test_pending_readiness_in_overlapping_grant_blocks_other_public_actions(setup, monkeypatch, action):
  db, row, principal = configure_repair(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  domain.save_outcome(db, peer, domain.key(ITEM), {"state": "reviewing", "ready_attempt": {
    "state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  target = dict(row.targets_json[0])
  with pytest.raises(HTTPException) as error:
    if action == "merge":
      domain.arm_merge(db, row, target, {"head_sha": SHA}, principal)
    else:
      domain.arm_repair(db, row, target, {}, {"state": "pushing", "from_sha": SHA,
        "head_sha": NEW, "base_sha": BASE}, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")
  assert not row.outcomes_json[domain.key(ITEM)].get("merge_attempted")


def test_lost_ready_response_confirms_same_head_after_base_ref_moves(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"id": "old-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "base ref moved")))
  def read(_gh, _cwd, path):
    if path == f"repos/{ITEM['repo']}":
      return {**REPO, "permissions": {"push": False}}
    if path == f"repos/{ITEM['repo']}/pulls/{ITEM['number']}":
      return {**PULL, "base": {"ref": "release", "sha": NEW}, "draft": False}
    pytest.fail("No actor read is needed to confirm an old ready effect")
  monkeypatch.setattr(domain, "read", read)
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["items"][0]["ready_attempt"]["state"] == "ready"


def test_confirmed_ready_receipt_merges_concurrent_stop_revision(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  receipt = {"id": "ready-race", "state": "attempting", "head_sha": SHA, "base_sha": BASE}
  domain.save_ready_attempt(db, row, domain.key(ITEM), receipt)
  revision = row.revision
  with SessionLocal() as writer:
    current = writer.get(models.ContributionReviewRun, row.id)
    domain.save_outcome(writer, current, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  assert row.revision == revision  # The dispatcher has not reloaded Stop yet.
  domain.save_ready_attempt(db, row, domain.key(ITEM), {**receipt, "state": "ready"})
  db.refresh(row)
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"
  assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "ready"


@pytest.mark.parametrize("current,incoming", [
  ("ready", "unattributable"), ("ready", "unknown"),
  ("unattributable", "unknown"), ("unattributable", "attempting"),
])
def test_stale_ready_observer_cannot_downgrade_saved_receipt(setup, monkeypatch, current, incoming):
  from app.database import SessionLocal
  db, row, _ = ready_takeover(setup, monkeypatch)
  item_key = domain.key(ITEM)
  receipt = {"id": "one-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE}
  domain.save_ready_attempt(db, row, item_key, receipt)
  stale = {**row.outcomes_json[item_key], "ready_attempt": {**receipt, "state": incoming}}
  with SessionLocal() as writer:
    live = writer.get(models.ContributionReviewRun, row.id)
    domain.save_ready_attempt(writer, live, item_key, {**receipt, "state": current})
  domain.save_effect_outcome(db, row, item_key, stale)
  db.refresh(row)
  assert row.outcomes_json[item_key]["ready_attempt"]["state"] == current
  assert row.outcomes_json[item_key]["ready_audit"][-1]["state"] == current


def test_ready_slot_rejects_other_receipt_but_accepts_confirmed_upgrade(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, _ = ready_takeover(setup, monkeypatch)
  item_key = domain.key(ITEM)
  receipt = {"id": "one-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE}
  domain.save_ready_attempt(db, row, item_key, receipt)
  stale_other = {**row.outcomes_json[item_key], "ready_attempt": {**receipt, "id": "other", "state": "ready"}}
  confirmed = {**row.outcomes_json[item_key], "ready_attempt": {**receipt, "state": "ready"},
    "ready_audit": [*row.outcomes_json[item_key]["ready_audit"],
      {"id": receipt["id"], "state": "ready", "head_sha": SHA}]}
  with SessionLocal() as writer:
    live = writer.get(models.ContributionReviewRun, row.id)
    domain.save_ready_attempt(writer, live, item_key, {**receipt, "state": "unattributable"})
  domain.save_effect_outcome(db, row, item_key, stale_other)
  assert row.outcomes_json[item_key]["ready_attempt"]["state"] == "unattributable"
  domain.save_effect_outcome(db, row, item_key, confirmed)
  assert row.outcomes_json[item_key]["ready_attempt"]["state"] == "ready"
  assert [a["state"] for a in row.outcomes_json[item_key]["ready_audit"]] == ["unknown", "unattributable", "ready"]


def test_stale_writers_cannot_erase_a_readiness_receipt_or_its_audit(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, _ = ready_takeover(setup, monkeypatch)
  item_key = domain.key(ITEM)
  receipt = {"id": "one-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE}
  domain.save_ready_attempt(db, row, item_key, receipt)
  saved = row.outcomes_json[item_key]
  assert not domain.keeps_public_receipts(saved, {"state": "reviewing"})
  assert not domain.keeps_public_receipts(saved, {**saved, "ready_attempt": {**receipt, "state": "attempting"}})
  assert not domain.keeps_public_receipts(saved, {**saved, "ready_audit": []})
  # A plain writer built from a snapshot without the receipt is refused.
  with pytest.raises(HTTPException) as error:
    domain.save_outcome(db, row, item_key, {"state": "needs_you", "summary": "stale"})
  assert error.value.status_code == 409
  db.refresh(row)
  assert row.outcomes_json[item_key] == saved
  # A remote fact written by a generic effect writer still lands, but keeps the receipt.
  with SessionLocal() as writer:
    live = writer.get(models.ContributionReviewRun, row.id)
    domain.save_effect_outcome(writer, live, item_key, {"state": "needs_you",
      "public_review": {"state": "posted", "marker": "m"}})
  db.refresh(row)
  item = row.outcomes_json[item_key]
  assert item["public_review"]["state"] == "posted"
  assert item["ready_attempt"] == receipt and item["ready_audit"] == saved["ready_audit"]


@pytest.mark.parametrize("state, eligible", [
  ("attempting", False), ("unknown", False), ("ready", True), ("unattributable", True)])
def test_base_advance_uses_the_shared_readiness_fence_rule(setup, monkeypatch, state, eligible):
  db, row, _ = ready_takeover(setup, monkeypatch)
  item = {"state": "reviewing", "ready_attempt": {"state": state, "head_sha": SHA}}
  assert domain.ready_pending(item) is not eligible
  assert domain.base_advance_eligible(row, item) is eligible
