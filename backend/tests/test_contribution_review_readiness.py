"""Draft readiness: explicit scope, shared admission and no replay."""
import asyncio
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import contribution_review_runs as domain, contribution_review_repairs as repairs, models
from app.contribution_review_effects import NotDispatched
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, CHECKS, report
from tests.test_contribution_review_workflows import takeover, configure_repair, publish_body, checkout_body, overlapping_grant, NEW


def ready_body(**changes):
  return routes.DraftReady(**{**ITEM, "reviewed_base_sha": BASE,
    "independent_receipt_id": "independent-clear", "summary": "Exact full diff is clear",
    "scope": sorted(routes.SCOPE), "tests": "Hosted checks and focused tests passed",
    "tests_passed": True, **changes})


def ready_takeover(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  row.options_json = {**row.options_json,
    "confirmation_scope": "named_pr_repairs_ready_and_reviewed_successors"}
  db.commit()
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


def test_draft_readiness_requires_new_frozen_scope_and_preserves_legacy_grant(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("legacy grant widened"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 403
  assert not domain.draft_ready_allowed(row)


def test_new_scope_is_distinct_frozen_capability_and_repair_stays_available(setup, monkeypatch):
  db, row, _ = ready_takeover(setup, monkeypatch)
  assert domain.draft_ready_allowed(row)
  routes._repair_allowed(row)
  assert "POST /api/github/contributions/1/review-runs/batch/ready" in domain.takeover_brief(row)
  row.options_json = {**row.options_json,
    "confirmation_scope": "named_pr_repairs_and_reviewed_successors"}
  db.commit()
  assert not domain.draft_ready_allowed(row)
  assert "does not authorize marking a draft ready" in domain.takeover_brief(row)


def test_new_draft_scope_admits_open_draft_without_widening_old_request_id(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: {**TARGET, "head_repo": REPO["full_name"],
    "head_repo_id": 1, "head_ref": "topic"})
  monkeypatch.setattr(domain, "repair_file_scope", lambda *a: ["owned.py"])
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": True}))
  async def admit(**kwargs):
    db.add(models.ChatRun(id="draft-admitted", chat_id=kwargs["chat_id"], status="stopped"))
    db.commit()
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  preview = routes.review_preview(1, routes.ReviewPreview(mode="review_fix_merge"), db, owner)
  body = routes.StartReviews(request_id="draft-grant-123", mode="review_fix_merge", items=[ITEM],
    confirmation_scope="named_pr_repairs_ready_and_reviewed_successors",
    preview_sha256=preview["preview_sha256"])
  result = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert result["run"]["options"]["confirmation_scope"] == body.confirmation_scope
  assert "POST /api/github/contributions/1/review-runs/" in result["brief"]
  old = body.model_copy(update={"confirmation_scope": "named_pr_repairs_and_reviewed_successors"})
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, old, db, owner))
  assert error.value.status_code == 409


def test_mark_ready_uses_github_documented_graphql_mutation_and_validates_exact_head():
  calls = []
  def gh(_cwd, *args, check=True):
    assert check is False
    calls.append(args)
    return SimpleNamespace(stdout='{"data":{"markPullRequestReadyForReview":{"pullRequest":{"id":"PR_7","isDraft":false,"headRefOid":"' + SHA + '"}}}}')
  assert domain.mark_ready(gh, "/tmp", TARGET)["isDraft"] is False
  assert "markPullRequestReadyForReview" in " ".join(calls[0])
  def changed(_cwd, *args, check=True):
    assert check is False
    return SimpleNamespace(stdout='{"data":{"markPullRequestReadyForReview":{"pullRequest":{"id":"PR_7","isDraft":false,"headRefOid":"' + NEW + '"}}}}')
  with pytest.raises(HTTPException):
    domain.mark_ready(changed, "/tmp", TARGET)


def test_draft_readiness_is_receipted_once_and_retry_only_observes(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": not stage["ready"]}))
  def mutate(*args):
    db.refresh(row)
    assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "attempting"
    calls.append(1)
    stage["ready"] = True
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mutate)
  first = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert first["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": False}))
  asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert calls == [1]


def test_draft_in_repository_without_checks_can_be_marked_ready(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
    "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}})
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": not stage["ready"]}))
  def mutate(*args):
    stage["ready"] = True
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mutate)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_uncertain_draft_readiness_never_replays_and_observe_reconciles(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  def lost(*args):
    calls.append(1)
    raise OSError("lost response")
  monkeypatch.setattr(domain, "mark_ready", lost)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "unknown"
  retry = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert retry["blocked"] and calls == [1]
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": False}))
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert observed["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert calls == [1]


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
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE,
      independent_receipt_id="independent-clear")
  assert error.value.status_code == 409
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  assert asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_stopped_run_can_only_observe_uncertain_readiness_read_only(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("stopped run repeated mutation"))
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  result = asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert db.get(models.ChatRun, principal.run_id).status == "stopped"


def test_draft_ready_then_exact_outcome_uses_existing_merge_gate(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  calls = []
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not stage["ready"]}))
  def ready(*a):
    stage["ready"] = True
    calls.append("ready")
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  def merge(*a):
    calls.append("merge")
    return {"merged": True, "sha": "landed"}
  monkeypatch.setattr(domain, "mark_ready", ready)
  monkeypatch.setattr(domain, "perform_merge", merge)
  assert asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  result = report(setup, tests_passed=True, reviewed_base_sha=BASE,
    independent_receipt_id="independent-clear")
  assert result["run"]["items"][0]["state"] == "merged"
  assert calls == ["ready", "merge"]


def test_ready_response_with_changed_base_confirms_exact_readiness(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not stage["ready"]}))
  def ready(*a):
    stage["ready"] = True
    monkeypatch.setattr(domain, "assert_current_base", lambda *a: (_ for _ in ()).throw(HTTPException(409, "base drift")))
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", ready)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"
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


def test_same_head_draft_unknown_explains_manual_or_new_head_recovery(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM), {"id": "old", "state": "unknown",
    "head_sha": SHA, "base_sha": BASE})
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": True}))
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert "mark it ready in GitHub" in result["blocked"]
  assert "different head" in result["run"]["items"][0]["summary"]
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "unknown"


def test_confirmed_earlier_head_receipt_requires_successor_grant_message(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM), {"id": "old", "state": "ready",
    "head_sha": SHA, "base_sha": BASE})
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior, "successor": {"head_sha": NEW,
    "base_sha": BASE}, "repair_attempts": [{"state": "pushed", "from_sha": SHA,
    "head_sha": NEW, "base_sha": BASE}]})
  monkeypatch.setattr(domain, "current_pull", lambda *a: pytest.fail("past receipt is already confirmed"))
  result = asyncio.run(routes.mark_draft_ready(1, row.id,
    ready_body(head_sha=NEW), db, principal))
  assert "new grant" in result["blocked"]
  assert "earlier head" in result["blocked"]
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"


@pytest.mark.parametrize("changed", ["actor", "model", "rights", "head", "stop"])
def test_confirmed_mutation_response_is_final_despite_later_drift(setup, monkeypatch, changed):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  calls = []
  def pull(*a):
    if changed == "head" and stage["ready"]:
      raise HTTPException(409, "The selected head moved")
    repo = {**REPO, "permissions": {"push": False}} if changed == "rights" and stage["ready"] else REPO
    return repo, {**PULL, "draft": not stage["ready"]}
  def actor(*a):
    return {"id": 999 if changed == "actor" and stage["ready"] else 42}
  def mutate(*a):
    stage["ready"] = True
    calls.append(1)
    if changed == "model":
      db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "another-model", "effort": "xhigh"}
    elif changed == "stop":
      db.get(models.ChatRun, principal.run_id).status = "stopped"
    db.commit()
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "current_pull", pull)
  monkeypatch.setattr(domain, "read", actor)
  monkeypatch.setattr(domain, "mark_ready", mutate)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert calls == [1]


@pytest.mark.parametrize("changed", ["actor", "rights"])
def test_lost_ready_response_confirms_same_head_without_current_authority(setup, monkeypatch, changed):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM),
    {"id": "old-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE})
  repo = {**REPO, "permissions": {"push": False}} if changed == "rights" else REPO
  monkeypatch.setattr(domain, "current_pull", lambda *a: (repo, {**PULL, "draft": False}))
  if changed == "actor":
    monkeypatch.setattr(domain, "read", lambda *a: (_ for _ in ()).throw(OSError("actor changed")))
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("never replay"))
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["items"][0]["ready_attempt"]["state"] == "ready"


def test_moved_head_retires_finished_unknown_without_attributing_ready_event(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM),
    {"id": "old-ready", "state": "unknown", "head_sha": SHA, "base_sha": BASE, "armed_at": CLOSED})
  moved_head_reads(monkeypatch)
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["items"][0]["ready_attempt"]["state"] == "unattributable"
  assert observed["items"][0]["state"] == "ready_unattributable"
  assert [a["state"] for a in observed["items"][0]["ready_audit"]] == ["unknown", "unattributable"]
  successor, new_head = successor_grant(db, row)
  domain.require_public_transition_clear(db, successor, new_head)
  with pytest.raises(HTTPException):
    domain.arm_ready(db, row, row.targets_json[0], {"id": "again", "state": "attempting",
      "head_sha": SHA, "base_sha": BASE}, principal)
  with pytest.raises(HTTPException):
    routes._require_ready_attempt_resolved(row.outcomes_json[domain.key(ITEM)], row.targets_json[0])
  routes._require_ready_attempt_resolved(row.outcomes_json[domain.key(ITEM)], new_head)
  domain.arm_ready(db, successor, new_head, {"id": "new-ready", "state": "attempting",
    "head_sha": NEW, "base_sha": BASE}, principal)
  assert successor.outcomes_json[domain.key(ITEM)]["ready_attempt"]["head_sha"] == NEW
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("old head must never replay"))
  retry = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert retry["blocked"] and retry["run"]["items"][0]["ready_attempt"]["state"] == "unattributable"
  reason = asyncio.run(routes._confirm_ready_attempt(row.targets_json[0], "/tmp"))
  assert "ready event" in reason


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


@pytest.mark.parametrize("draft", [True, False])
@pytest.mark.parametrize("route", ["observe", "ready"])
def test_moved_head_keeps_fence_while_an_orphaned_ready_request_can_land(setup, monkeypatch, draft, route):
  """GitHub's mark-ready names no head: an in-flight request marks the live head."""
  db, row, principal = ready_takeover(setup, monkeypatch)
  domain.save_ready_attempt(db, row, domain.key(ITEM), {"id": "old-ready", "state": "attempting",
    "head_sha": SHA, "base_sha": BASE, "armed_at": iso(time.time() - 60)})
  moved_head_reads(monkeypatch, draft=draft)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("never replayed"))
  successor, new_head = successor_grant(db, row)
  for observation in range(3):
    if route == "observe":
      item = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]
    else:
      result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
      item = result["run"]["items"][0]
      if observation:
        assert "an hour after it began" in result["blocked"]
    assert item["ready_attempt"]["state"] == "unknown"
    with pytest.raises(HTTPException):
      domain.require_public_transition_clear(db, successor, new_head)
    with pytest.raises(HTTPException):
      routes._require_ready_attempt_resolved(row.outcomes_json[domain.key(ITEM)], new_head)
  # Once the dispatch window has closed, no request for the attempt can land.
  monkeypatch.setattr(domain, "MERGE_DISPATCH_WINDOW_SECONDS", 0)
  asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "unattributable"
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


def test_ready_receipt_records_arm_time_before_dispatch(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  seen = []
  def mutate(*args):
    db.refresh(row)
    seen.append(row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["armed_at"])
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mutate)
  before = time.time()
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  armed_at = datetime.fromisoformat(seen[0].replace("Z", "+00:00")).timestamp()
  assert before - 1 <= armed_at <= time.time() + 1
  assert result["run"]["items"][0]["ready_attempt"]["armed_at"] == seen[0]


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



@pytest.mark.parametrize("invalid", [
  {"tests_passed": False}, {"scope": ["correctness"] * 6},
  {"reviewed_base_sha": "c" * 40}, {"independent_receipt_id": "missing"},
])
def test_draft_readiness_requires_exact_fresh_independent_evidence(setup, monkeypatch, invalid):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("insufficient evidence"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(**invalid), db, principal))
  assert error.value.status_code in {409, 422}


@pytest.mark.parametrize("case", ["checks", "actor", "stopped", "head"])
def test_draft_readiness_rechecks_checks_actor_stop_and_head(setup, monkeypatch, case):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("preflight failed"))
  if case == "checks":
    monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
      "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]}})
  elif case == "actor":
    monkeypatch.setattr(domain, "read", lambda *a: {"id": 999})
  elif case == "stopped":
    db.get(models.ChatRun, principal.run_id).status = "stopped"
    db.commit()
  else:
    monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL,
      "draft": True, "head": {"sha": "c" * 40}}))
    # The route must not trust a changed head even if a mocked fetch bypasses
    # current_pull's identity guard.
    monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "headRefOid": "c" * 40})
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))


@pytest.mark.parametrize("case", ["actor", "permissions", "stop", "app"])
def test_single_draft_preflight_refuses_revoked_identity_or_rights(setup, monkeypatch, case):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("no mutation after revocation"))
  reads = {"actor": 0, "pull": 0, "checks": 0}
  def actor(*a):
    reads["actor"] += 1
    return {"id": 999 if case == "actor" and reads["actor"] == 1 else 42}
  def pull(*a):
    reads["pull"] += 1
    repo = {**REPO, "permissions": {"push": False}} if case == "permissions" and reads["pull"] == 1 else REPO
    return repo, {**PULL, "draft": True}
  def checks(*a):
    reads["checks"] += 1
    if reads["checks"] == 1 and case in {"stop", "app"}:
      if case == "stop":
        db.get(models.ChatRun, principal.run_id).status = "stopped"
      else:
        db.get(models.App, row.app_id).github_access = False
      db.commit()
    return CHECKS
  monkeypatch.setattr(domain, "read", actor)
  monkeypatch.setattr(domain, "current_pull", pull)
  monkeypatch.setattr(domain, "pull_checks", checks)
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert reads["checks"] == (0 if case == "actor" else 1)
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("case", ["stop", "app", "model"])
def test_refusal_after_arming_readiness_is_not_attempted_and_can_retry(setup, monkeypatch, case):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  monkeypatch.setattr(domain, "mark_ready", lambda *a: calls.append(1))
  arm = domain.arm_ready
  def arm_then_revoke(*args):
    arm(*args)
    if case == "stop":
      db.get(models.ChatRun, principal.run_id).status = "stopped"
    elif case == "app":
      db.get(models.App, row.app_id).github_access = False
    else:
      db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "gpt-5", "effort": "low"}
    db.commit()
  monkeypatch.setattr(domain, "arm_ready", arm_then_revoke)
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  db.refresh(row)
  assert calls == []
  assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None
  if case == "stop":
    db.get(models.ChatRun, principal.run_id).status = "running"
  elif case == "app":
    db.get(models.App, row.app_id).github_access = True
  else:
    db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "gpt-5", "effort": "xhigh"}
  db.commit()
  monkeypatch.setattr(domain, "arm_ready", arm)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not calls}))
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert calls == [1]
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_leftover_attempting_readiness_escalates_to_needs_you_on_observe(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "attempting", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("never replayed"))
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["state"] == "needs_you"
  assert observed["items"][0]["state"] == "ready_unknown"
  assert observed["items"][0]["ready_attempt"]["state"] == "unknown"


def test_leftover_attempting_readiness_escalates_on_ready_retry(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "attempting", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("never replayed"))
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["blocked"]
  assert result["run"]["state"] == "needs_you"
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "unknown"


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


@pytest.mark.parametrize("state", ["pushing", "push_unknown"])
@pytest.mark.parametrize("overlap", [False, True])
def test_draft_ready_cannot_cross_pending_repair_with_lagging_head(setup, monkeypatch, state, overlap):
  db, row, principal = ready_takeover(setup, monkeypatch)
  grant = overlapping_grant(db, row) if overlap else row
  previous = grant.outcomes_json.get(domain.key(ITEM), {})
  domain.save_outcome(db, grant, domain.key(ITEM), {**previous, "state": "needs_you",
    "repair_attempts": [{"state": state, "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("PUBLIC_READY"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("state", ["merge_unknown", "queued"])
def test_draft_ready_cannot_cross_overlapping_pending_merge(setup, monkeypatch, state):
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  domain.save_outcome(db, peer, domain.key(ITEM), {"state": state,
    "head_sha": SHA, "merge_attempted": True})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("PUBLIC_READY"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("other_action", ["merge", "repair"])
@pytest.mark.parametrize("same_item", [False, True])
def test_readiness_and_incompatible_action_share_database_admission_fence(setup, monkeypatch, other_action, same_item):
  from concurrent.futures import ThreadPoolExecutor
  from threading import Barrier
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row, number=None if same_item else 8)
  target, peer_target = dict(row.targets_json[0]), dict(peer.targets_json[0])
  real_claim = agent_work_claims.claim_work
  barrier = Barrier(2)
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    barrier.wait(timeout=10)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  def arm(action):
    with SessionLocal() as session:
      current = session.get(models.ContributionReviewRun, row.id if action == "ready" else peer.id)
      try:
        if action == "ready":
          result = domain.arm_ready(session, current, target, {"id": "ready", "state": "attempting",
            "head_sha": SHA, "base_sha": BASE}, principal)
        elif action == "repair":
          result = domain.arm_repair(session, current, peer_target, {}, {"id": "repair", "state": "pushing",
            "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}, principal)
        else:
          result = domain.arm_merge(session, current, peer_target, {"head_sha": SHA}, principal)
        assert result is None
        return "admitted"
      except HTTPException as exc:
        session.rollback()
        assert exc.status_code == 409
        return "blocked"
  with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(arm, ["ready", other_action]))
  assert sorted(results) == ["admitted", "blocked"]
  with SessionLocal() as restarted:
    assert sum(bool(item.get("ready_attempt") or item.get("merge_attempted") or item.get("repair_attempts"))
      for grant in restarted.query(models.ContributionReviewRun).all()
      for item in grant.outcomes_json.values()) == 1


@pytest.mark.parametrize("pending", ["merge", "repair"])
def test_readiness_revalidates_pending_public_actions_after_claim_commit(setup, monkeypatch, pending):
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  target = dict(row.targets_json[0])
  real_claim = agent_work_claims.claim_work
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as writer:
      saved = writer.get(models.ContributionReviewRun, peer.id)
      outcome = ({"state": "queued", "merge_attempted": True, "head_sha": SHA}
        if pending == "merge" else {"state": "needs_you", "repair_attempts": [
          {"state": "push_unknown", "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
      domain.save_outcome(writer, saved, domain.key(ITEM), outcome)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  with pytest.raises(HTTPException) as error:
    domain.arm_ready(db, row, target, {"id": "ready", "state": "attempting",
      "head_sha": SHA, "base_sha": BASE}, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


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


def test_merge_retains_ready_receipt_resolved_during_claim_commit(setup, monkeypatch):
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  real_claim = agent_work_claims.claim_work
  receipt = {"id": "confirmed-ready", "state": "ready", "head_sha": SHA, "base_sha": BASE}
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as writer:
      current = writer.get(models.ContributionReviewRun, row.id)
      previous = current.outcomes_json[domain.key(ITEM)]
      domain.save_outcome(writer, current, domain.key(ITEM), {**previous, "ready_attempt": receipt})
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  monkeypatch.setattr(domain, "perform_merge", lambda *a: {"merged": True, "sha": "landed"})
  result = report(setup, tests_passed=True, reviewed_base_sha=BASE,
    independent_receipt_id="independent-clear")
  item = result["run"]["items"][0]
  assert item["state"] == "merged" and item["ready_attempt"] == receipt
  assert item["independent_reviews"][0]["id"] == "independent-clear"


def test_readiness_audit_preserves_admission_and_confirmation(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not stage["ready"]}))
  def mark_ready(*args):
    stage["ready"] = True
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mark_ready)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  attempt = result["run"]["items"][0]["ready_attempt"]
  audit = row.outcomes_json[domain.key(ITEM)]["ready_audit"]
  assert [event["state"] for event in audit] == ["attempting", "ready"]
  assert all(event["id"] == attempt["id"] for event in audit)


def test_known_not_dispatched_readiness_releases_exact_claim(setup, monkeypatch):
  from app.contribution_review_effects import NotDispatched
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: (_ for _ in ()).throw(NotDispatched()))
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  db.refresh(row)
  outcome = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in outcome
  assert [event["state"] for event in outcome["ready_audit"]] == ["attempting", "not_dispatched"]


def test_changed_readiness_receipt_cannot_release_a_claim(setup, monkeypatch):
  db, row, _ = ready_takeover(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, 'ready_attempt': {
    'id': 'other-receipt', 'state': 'unknown', 'head_sha': SHA}})
  with pytest.raises(HTTPException):
    domain.disarm_ready(db, row, ITEM, 'my-receipt')
  assert row.outcomes_json[domain.key(ITEM)]['ready_attempt']['state'] == 'unknown'


def test_mark_ready_parses_real_gh_graphql_exit_one_no_data_as_not_dispatched():
  calls = []
  def gh(_cwd, *args, check=True):
    calls.append((args, check))
    return SimpleNamespace(returncode=1, stdout=json.dumps({"errors": [{"type": "FORBIDDEN", "message": "forbidden"}]}),
      stderr="gh: GraphQL request failed")
  with pytest.raises(NotDispatched):
    domain.mark_ready(gh, "/tmp", TARGET)
  assert len(calls) == 1 and calls[0][1] is False


@pytest.mark.parametrize("status", [403, 503])
def test_ready_provider_http_refusal_classifies_receipt_or_release(setup, monkeypatch, status):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  def gh(_cwd, *args, check=True):
    assert check is False
    calls.append(1)
    return SimpleNamespace(returncode=1, stdout="not-json",
      stderr=f"gh: HTTP {status}: provider refusal")
  monkeypatch.setattr(routes, "_gh", gh)
  if status == 403:
    with pytest.raises(HTTPException) as error:
      asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
    assert error.value.status_code == 409
    db.refresh(row)
    assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
    claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
    assert claim.released_at is not None
  else:
    first = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
    assert first["run"]["items"][0]["ready_attempt"]["state"] == "unknown"
    second = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
    assert second["blocked"] and calls == [1]


def test_ready_graphql_no_data_disarms_exact_claim(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  def gh(_cwd, *args, check=True):
    assert check is False
    calls.append(1)
    return SimpleNamespace(returncode=1, stdout=json.dumps({"errors": [{"type": "FORBIDDEN", "message": "not allowed"}]}),
      stderr="gh: GraphQL error")
  monkeypatch.setattr(routes, "_gh", gh)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  db.refresh(row)
  assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None
  assert calls == [1]


def test_confirmed_ready_effect_survives_stop_after_dispatch(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not stage["ready"]}))
  def mutate(*args):
    stage["ready"] = True
    db.get(models.ChatRun, principal.run_id).status = "stopped"
    db.commit()
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mutate)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_programming_error_in_ready_dispatch_is_not_silenced(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  def broken(*args):
    raise RuntimeError("implementation bug")
  monkeypatch.setattr(domain, "mark_ready", broken)
  with pytest.raises(RuntimeError, match="implementation bug"):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))


@pytest.mark.parametrize("errors", ["forbidden", [{"message": "not allowed"}], [{"type": "INTERNAL", "message": "timeout"}]])
def test_malformed_graphql_error_shape_is_unknown_not_dispatched(setup, monkeypatch, errors):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  def gh(_cwd, *args, check=True):
    calls.append(1)
    return SimpleNamespace(returncode=1, stdout=json.dumps({"errors": errors}), stderr="gh error")
  monkeypatch.setattr(routes, "_gh", gh)
  first = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert first["run"]["items"][0]["ready_attempt"]["state"] == "unknown"
  retry = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert retry["blocked"] and calls == [1]


def test_readiness_timeout_keeps_unknown_receipt_without_replay(setup, monkeypatch):
  import subprocess
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  def timeout(*args):
    calls.append(1)
    raise subprocess.TimeoutExpired(['gh', 'api', 'graphql'], 30)
  monkeypatch.setattr(domain, 'mark_ready', timeout)
  first = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert first['run']['items'][0]['ready_attempt']['state'] == 'unknown'
  retry = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert retry['blocked'] and calls == [1]


def test_real_ready_gh_exit_one_preserves_structured_refusal(tmp_path, monkeypatch):
  from app import github_contribution_git as transport
  fake = tmp_path / 'gh'
  fake.write_text('#!/bin/sh\necho \'{"errors":[{"type":"FORBIDDEN","message":"not allowed"}],"data":null}\'\nexit 1\n')
  fake.chmod(0o755)
  monkeypatch.setattr(transport, '_git_env', lambda cwd: {'PATH': str(tmp_path)+':/usr/bin:/bin'})
  with pytest.raises(NotDispatched):
    domain.mark_ready(transport._gh, tmp_path, TARGET)


def test_stop_admission_lock_prevents_ready_arm_after_preflight(setup, monkeypatch):
  from app import chat_queue
  db, row, principal = ready_takeover(setup, monkeypatch)
  armed = []
  monkeypatch.setattr(domain, "arm_ready", lambda *a: armed.append(1))
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("no mutation after Stop"))

  async def race():
    loop = asyncio.get_running_loop()
    preflight = asyncio.Event()
    def checks(*a):
      loop.call_soon_threadsafe(preflight.set)
      return CHECKS
    monkeypatch.setattr(domain, "pull_checks", checks)
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      task = asyncio.create_task(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
      await asyncio.wait_for(preflight.wait(), 5)
      await asyncio.sleep(0.05)
      assert not task.done() and not armed
      domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
    with pytest.raises(HTTPException) as error:
      await task
    assert error.value.status_code == 409
  asyncio.run(race())
  db.refresh(row)
  assert not armed and "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]


def test_stop_after_ready_arm_refuses_dispatch_and_releases_exact_claim(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  dispatched = []
  monkeypatch.setattr(domain, "mark_ready", lambda *a: dispatched.append(1))
  original_arm = routes._arm_ready

  async def arm_then_stop(*args):
    receipt = await original_arm(*args)
    # The arm function has released review-admission. Stop wins before the
    # mutation can enter the lock again; the reservation is not an effect.
    domain.save_outcome(db, row, routes.REVIEW_STOP_KEY,
      {"state": "requested", "actor": "owner"})
    return receipt

  monkeypatch.setattr(routes, "_arm_ready", arm_then_stop)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  db.refresh(row)
  assert not dispatched
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"
  assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
  claim = db.query(models.AgentWorkClaim).filter_by(
    work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None


@pytest.mark.parametrize("drift", ["head", "base"])
def test_ready_refuses_target_drift_after_arm_without_dispatch(setup, monkeypatch, drift):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = {"pull": 0, "base": 0}
  dispatched = []
  monkeypatch.setattr(domain, "mark_ready", lambda *a: dispatched.append(1))

  def pull(*args):
    calls["pull"] += 1
    if drift == "head" and calls["pull"] > 1:
      raise HTTPException(409, "The selected PR head moved.")
    return REPO, {**PULL, "draft": True}

  def base(*args):
    calls["base"] += 1
    if drift == "base" and calls["base"] > 1:
      raise HTTPException(409, "The selected base moved.")

  monkeypatch.setattr(domain, "current_pull", pull)
  monkeypatch.setattr(domain, "assert_current_base", base)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert not dispatched
  assert calls["pull"] >= (2 if drift == "head" else 1)
  assert calls["base"] >= (2 if drift == "base" else 1)
  if drift == "base":
    assert "start a fresh independent reviewer through /reviewers" in error.value.detail
    assert "retry /ready with its new all-clear receipt" in error.value.detail
  db.refresh(row)
  assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
  claim = db.query(models.AgentWorkClaim).filter_by(
    work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None



def test_ready_transient_in_lock_read_disarms_only_its_unspent_attempt(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  armed = []
  dispatched = []
  reads = []
  original_arm = routes._arm_ready

  async def capture_arm(*args):
    receipt = await original_arm(*args)
    armed.append(dict(receipt))
    return receipt

  def pull(*args):
    reads.append(1)
    if len(reads) == 2:
      raise subprocess.TimeoutExpired("gh", 1)
    return REPO, {**PULL, "draft": True}

  monkeypatch.setattr(routes, "_arm_ready", capture_arm)
  monkeypatch.setattr(domain, "current_pull", pull)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: dispatched.append(1))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert error.value.detail == "Readiness preflight failed before dispatch."
  assert len(reads) == 2 and len(armed) == 1 and not dispatched
  db.refresh(row)
  outcome = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in outcome
  assert outcome["ready_audit"][-1] == {
    "id": armed[0]["id"], "head_sha": SHA, "state": "not_dispatched"}
  claim = db.query(models.AgentWorkClaim).filter_by(
    work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.owner_chat_id == row.chat_id and claim.released_at is not None


def test_disarmed_base_drift_can_advance_and_retry_after_fresh_review(setup, monkeypatch):
  from app import delegations
  new_base = "e" * 40
  db, row, principal = ready_takeover(setup, monkeypatch)
  live = {"sha": BASE, "compares": []}
  monkeypatch.setattr(domain, "base_head", lambda *a: live["sha"])
  def assert_base(_gh, _cwd, target):
    if live["sha"] != target["base_sha"]:
      raise HTTPException(409, "The target branch changed.")
  monkeypatch.setattr(domain, "assert_current_base", assert_base)
  def read(_gh, _cwd, endpoint):
    if "/compare/" in endpoint:
      old, new = endpoint.rsplit("/", 1)[1].split("...")
      live["compares"].append((old, new))
      return {"status": "ahead", "base_commit": {"sha": old},
        "merge_base_commit": {"sha": old}}
    return {"id": 42}
  monkeypatch.setattr(domain, "read", read)
  original_arm = routes._arm_ready
  dispatched = []
  monkeypatch.setattr(domain, "mark_ready", lambda *a: dispatched.append(1))

  async def arm_then_base_moves(*args):
    receipt = await original_arm(*args)
    live["sha"] = new_base
    return receipt

  monkeypatch.setattr(routes, "_arm_ready", arm_then_base_moves)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409 and not dispatched
  db.refresh(row)
  assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
  claim = db.query(models.AgentWorkClaim).filter_by(
    work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None

  # /reviewers observes a proved descendant and freezes a fresh review on
  # the new effective base; /observe is for uncertain public effects instead.
  monkeypatch.setattr(delegations, "ensure_delegation_started", lambda *a, **kw: asyncio.sleep(0, result=True))
  reviewed = asyncio.run(routes.start_independent_reviewer(
    1, row.id, routes.PullIdentity(**ITEM), db, principal))
  assert reviewed["run"]["items"][0]["current_base_sha"] == new_base
  assert live["compares"] == [(BASE, new_base)]
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "independent_reviews": [
    *previous["independent_reviews"], {"id": "fresh-base-clear", "head_sha": SHA,
      "base_sha": new_base, "state": "all_clear", "tests_passed": True}]})
  monkeypatch.setattr(routes, "_arm_ready", original_arm)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: dispatched.append(1) or
    {"id": "PR_7", "isDraft": False, "headRefOid": SHA})
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(
    reviewed_base_sha=new_base, independent_receipt_id="fresh-base-clear"), db, principal))
  assert dispatched == [1]
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_stop_waits_for_in_flight_ready_mutation_admission(setup, monkeypatch):
  from app import chat_queue
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  mutation_started = threading.Event()
  release_mutation = threading.Event()

  def mark_ready(*args):
    mutation_started.set()
    assert release_mutation.wait(5), "test did not release the provider call"
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}

  monkeypatch.setattr(domain, "mark_ready", mark_ready)

  async def race():
    ready_task = asyncio.create_task(routes.mark_draft_ready(
      1, row.id, ready_body(), db, principal))
    stop_started = asyncio.Event()
    stop_admitted = asyncio.Event()

    async def stop():
      stop_started.set()
      # This is Stop's durable admission boundary, using its own DB session.
      async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
        stop_admitted.set()
        with SessionLocal() as stop_db:
          stop_row = stop_db.get(models.ContributionReviewRun, row.id)
          domain.save_outcome(stop_db, stop_row, routes.REVIEW_STOP_KEY,
            {"state": "requested", "actor": "owner"})

    try:
      assert await asyncio.wait_for(asyncio.to_thread(mutation_started.wait, 5), 6)
      stop_task = asyncio.create_task(stop())
      await asyncio.wait_for(stop_started.wait(), 5)
      await asyncio.sleep(0.05)
      assert not stop_admitted.is_set(), "Stop landed during the admitted mutation"
    finally:
      release_mutation.set()
    result = await asyncio.wait_for(ready_task, 5)
    await asyncio.wait_for(stop_task, 5)
    assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"

  asyncio.run(race())
  db.refresh(row)
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"
  assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "ready"


def test_start_refuses_draft_takeover_without_ready_scope(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: {**TARGET, "is_draft": True})
  monkeypatch.setattr(domain, "repair_file_scope", lambda *a: pytest.fail("draft guard did not refuse before repair scope read"))
  async def no_start(**kwargs):
    pytest.fail("draft without ready scope admitted")
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", no_start)
  preview = routes.review_preview(1, routes.ReviewPreview(mode="review_fix_merge"), db, owner)
  body = routes.StartReviews(request_id="draft-without-ready", mode="review_fix_merge", items=[ITEM],
    confirmation_scope="named_pr_repairs_and_reviewed_successors",
    preview_sha256=preview["preview_sha256"])
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, owner))
  assert error.value.status_code == 422


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


def test_disarm_is_the_only_release_and_matches_the_exact_receipt(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  target = row.targets_json[0]
  receipt = {"id": "mine", "state": "attempting", "head_sha": SHA, "base_sha": BASE}
  domain.arm_ready(db, row, target, receipt, principal)
  with pytest.raises(HTTPException):
    domain.disarm_ready(db, row, target, "another")
  assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["id"] == "mine"
  domain.disarm_ready(db, row, target, "mine")
  outcome = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in outcome
  assert [e["state"] for e in outcome["ready_audit"]] == ["attempting", "not_dispatched"]
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(target)).one()
  assert claim.released_at is not None


@pytest.mark.parametrize("state, eligible", [
  ("attempting", False), ("unknown", False), ("ready", True), ("unattributable", True)])
def test_base_advance_uses_the_shared_readiness_fence_rule(setup, monkeypatch, state, eligible):
  db, row, _ = ready_takeover(setup, monkeypatch)
  item = {"state": "reviewing", "ready_attempt": {"state": state, "head_sha": SHA}}
  assert domain.ready_pending(item) is not eligible
  assert domain.base_advance_eligible(row, item) is eligible


def cancel_during_mutation(setup, monkeypatch, mutation):
  """Cancel the HTTP task while the provider call runs; return the cancelled outcome."""
  db, row, principal = ready_takeover(setup, monkeypatch)
  started, release = threading.Event(), threading.Event()
  def mark_ready(*args):
    started.set()
    assert release.wait(5), "test did not release the provider call"
    return mutation()
  monkeypatch.setattr(domain, "mark_ready", mark_ready)
  async def race():
    task = asyncio.create_task(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
    try:
      assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 6)
      task.cancel()
      await asyncio.sleep(0.05)
    finally:
      release.set()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, 5)
  asyncio.run(race())
  db.refresh(row)
  return db, row


def test_cancelled_request_still_records_confirmed_readiness(setup, monkeypatch):
  db, row = cancel_during_mutation(setup, monkeypatch,
    lambda: {"id": "PR_7", "isDraft": False, "headRefOid": SHA})
  item = row.outcomes_json[domain.key(ITEM)]
  assert item["ready_attempt"]["state"] == "ready"
  assert [e["state"] for e in item["ready_audit"]] == ["attempting", "ready"]


def test_cancelled_request_still_releases_a_certified_refusal(setup, monkeypatch):
  def refused():
    raise NotDispatched("GitHub refused readiness.")
  db, row = cancel_during_mutation(setup, monkeypatch, refused)
  item = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in item
  assert [e["state"] for e in item["ready_audit"]] == ["attempting", "not_dispatched"]
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None


def test_cancel_during_final_preflight_never_starts_the_mutation(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("mutation started after cancel"))
  reads, started, release = [], threading.Event(), threading.Event()
  def checks(*args):
    reads.append(1)
    if len(reads) == 2:  # The in-lock preflight after arming.
      started.set()
      assert release.wait(5), "test did not release the preflight read"
    return CHECKS
  monkeypatch.setattr(domain, "pull_checks", checks)
  async def race():
    task = asyncio.create_task(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
    try:
      assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 6)
      task.cancel()
      await asyncio.sleep(0.05)
    finally:
      release.set()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, 5)
  asyncio.run(race())
  db.refresh(row)
  item = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in item
  assert item["ready_audit"][-1]["state"] == "not_dispatched"
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None


@pytest.mark.parametrize("failure", [RuntimeError("bug in a read"), asyncio.CancelledError()])
def test_any_failure_before_the_send_is_a_known_non_dispatch(setup, monkeypatch, failure):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("mutation after failed preflight"))
  reads = []
  def checks(*args):
    reads.append(1)
    if len(reads) == 2:
      raise failure
    return CHECKS
  monkeypatch.setattr(domain, "pull_checks", checks)
  with pytest.raises((HTTPException, asyncio.CancelledError)):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  db.refresh(row)
  item = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in item
  assert item["ready_audit"][-1]["state"] == "not_dispatched"
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None
