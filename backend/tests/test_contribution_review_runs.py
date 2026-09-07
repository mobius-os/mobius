"""Exact selected heads, private reviews and guarded public merge receipts."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain, models
from app.database import SessionLocal
from app.deps import Principal
from app.routes import contribution_reviews as routes

SHA = "a" * 40
BASE = "b" * 40
ITEM = {"repo": "example/project", "number": 7, "head_sha": SHA,
        "base_ref": "main", "base_sha": BASE}
TARGET = {**ITEM, "repo_id": 1, "pr_id": "PR_7", "base_ref": "main",
          "base_sha": BASE, "title": "A change", "url": "https://github.com/example/project/pull/7"}
REPO = {"id": 1, "full_name": ITEM["repo"], "permissions": {"push": True},
        "allow_squash_merge": True}
PULL = {"node_id": "PR_7", "state": "open", "head": {"sha": SHA},
        "base": {"ref": "main", "sha": BASE}, "title": "A change", "html_url": TARGET["url"]}
CHECKS = {"headRefOid": SHA, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
          "reviewDecision": "APPROVED", "isMergeQueueEnabled": False,
          "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}


@pytest.fixture
def setup(fresh_db, monkeypatch):
  db = SessionLocal()
  owner = models.Owner(username="review-owner", hashed_password="unused")
  db.add(owner)
  db.flush()
  chat = models.Chat(id="review-chat", title="Review")
  db.add(chat)
  db.add(models.App(id=1, name="Contribute", slug="contribute", source_dir="test-contribute", github_access=True, token_nonce="nonce"))
  db.add(models.ChatRun(id="physical-run", chat_id=chat.id, status="running"))
  db.commit()
  row = models.ContributionReviewRun(id="batch", app_id=1, owner_id=owner.id,
    request_id="request-1", mode="review_merge", targets_json=[TARGET],
    outcomes_json={}, chat_id=chat.id, github_actor_id="42", app_nonce="nonce")
  db.add(row)
  db.commit()
  principal = Principal(owner=owner, app_id=None, chat_id=chat.id, run_id="physical-run")
  monkeypatch.setattr(routes, "_validate_submit_app", lambda *args: "nonce")
  monkeypatch.setattr(domain, "read", lambda *args: {"id": 42})
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, PULL))
  monkeypatch.setattr(domain, "pull_checks", lambda *args: CHECKS)
  yield db, row, principal
  db.close()


def report(setup, **changes):
  db, row, principal = setup
  data = {**ITEM, "state": "all_clear", "summary": "Full diff reviewed",
          "scope": sorted(routes.SCOPE), "tests": "Focused tests passed", **changes}
  return asyncio.run(routes.report_outcome(1, row.id, routes.ReviewOutcome(**data), db, principal))


def test_review_only_never_merges_even_own_pr(setup, monkeypatch):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("public write"))
  assert report(setup)["run"]["items"][0]["state"] == "all_clear"


def test_exact_clear_head_merges_once_and_replay_is_read_only(setup, monkeypatch):
  calls = []
  monkeypatch.setattr(domain, "perform_merge", lambda *args: calls.append(args[2]) or {"merged": True, "sha": "landed"})
  assert report(setup)["run"]["items"][0]["state"] == "merged"
  report(setup)
  assert calls == [TARGET]


def test_foreign_chat_cannot_report_or_consume_grant(setup):
  setup[2].chat_id = "someone-else"
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 403


def test_child_cannot_consume_parent_grant(setup):
  setup[2].delegation_id = "child"
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 403


def test_changed_selected_head_is_never_approved(setup):
  with pytest.raises(HTTPException) as error:
    report(setup, head_sha="c" * 40)
  assert error.value.status_code == 409


def test_all_clear_requires_full_rubric_and_test_evidence(setup):
  with pytest.raises(HTTPException) as error:
    report(setup, scope=["tests"])
  assert error.value.status_code == 422


def test_unsafe_item_stops_without_public_action(setup, monkeypatch):
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("public write"))
  assert report(setup, state="needs_you", summary="Unclear data migration")["run"]["state"] == "needs_you"


def test_lost_merge_receipt_is_not_replayed(setup, monkeypatch):
  calls = []
  def lose(*args):
    calls.append(1)
    raise TimeoutError()
  monkeypatch.setattr(domain, "perform_merge", lose)
  assert report(setup)["run"]["items"][0]["state"] == "merge_unknown"
  assert report(setup)["run"]["items"][0]["state"] == "merge_unknown"
  assert calls == [1]


def test_lost_merge_receipt_reconciles_exact_merged_head(setup, monkeypatch):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "merging"})
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "merged": True, "merge_commit_sha": "landed"}))
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("repeat"))
  assert report(setup)["run"]["items"][0]["merge_sha"] == "landed"


def test_temporarily_changed_head_cannot_erase_an_ambiguous_attempt(setup, monkeypatch):
  db, row, principal = setup
  calls = []
  def lose(*args):
    calls.append(1)
    raise TimeoutError()
  monkeypatch.setattr(domain, "perform_merge", lose)
  assert report(setup)["run"]["items"][0]["state"] == "merge_unknown"
  def unavailable(*args):
    raise HTTPException(409, "This head changed")
  monkeypatch.setattr(domain, "current_pull", unavailable)
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "needs_you"
  assert item["merge_attempted"] is True
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, PULL))
  assert report(setup)["run"]["items"][0]["state"] == "merge_unknown"
  # A second grant cannot resurrect the same attempt either, even if the
  # visible first-row state was changed by temporary remote unavailability.
  domain.save_outcome(db, row, domain.key(TARGET), {**item, "state": "needs_you"})
  second = models.ContributionReviewRun(id="retry-grant", app_id=1, owner_id=principal.owner.id,
    request_id="retry-request", mode="review_merge", targets_json=[TARGET],
    outcomes_json={}, chat_id=row.chat_id, github_actor_id="42", app_nonce="nonce")
  db.add(second)
  db.commit()
  result = report((db, second, principal))["run"]["items"][0]
  assert result["review_selection_id"] == row.id
  assert calls == [1]


def test_queue_is_used_instead_of_direct_merge(setup, monkeypatch):
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {**CHECKS, "isMergeQueueEnabled": True})
  monkeypatch.setattr(domain, "enqueue", lambda *args: {"id": "queue-1"})
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("queue bypass"))
  assert report(setup)["run"]["items"][0]["state"] == "queued"


@pytest.mark.parametrize("changes", [
  {"reviewDecision": "CHANGES_REQUESTED"}, {"reviewDecision": "REVIEW_REQUIRED"},
  {"mergeStateStatus": "UNKNOWN"}, {"mergeStateStatus": "BLOCKED"},
  {"headRefOid": "c" * 40}, {"mergeable": "CONFLICTING"},
  {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]}},
])
def test_github_blockers_stop_before_merge(changes):
  assert domain.merge_blocker(TARGET, REPO, PULL, {**CHECKS, **changes})


def test_removed_permission_and_changed_base_stop_merge():
  assert domain.merge_blocker(TARGET, {**REPO, "permissions": {}}, PULL, CHECKS)
  assert domain.merge_blocker(TARGET, REPO, {**PULL, "base": {"ref": "main", "sha": "c" * 40}}, CHECKS)


def test_merge_call_binds_sha_without_admin_bypass():
  calls = []
  def gh(*args):
    calls.append(args)
    return SimpleNamespace(stdout=json.dumps({"merged": True}))
  domain.perform_merge(gh, "/tmp", TARGET, REPO)
  assert f"sha={SHA}" in calls[0]
  assert not any("admin" in str(a) for a in calls[0])


def test_queue_call_binds_sha_and_never_jumps():
  calls = []
  def gh(*args):
    calls.append(args)
    return SimpleNamespace(stdout=json.dumps({"data": {"enqueuePullRequest": {
      "mergeQueueEntry": {"id": "queue-1", "headCommit": {"oid": SHA}}
    }}}))
  domain.enqueue(gh, "/tmp", TARGET)
  assert f"head={SHA}" in calls[0]
  assert "jump:false" in " ".join(str(a) for a in calls[0])
  assert "expectedHeadOid:$head" in " ".join(str(a) for a in calls[0])


def test_selection_rejects_changed_head_before_grant():
  def gh(cwd, command, endpoint):
    return SimpleNamespace(stdout=json.dumps(REPO if endpoint.endswith("project") else PULL))
  with pytest.raises(HTTPException):
    domain.inspect_target(gh, "/tmp", {**ITEM, "head_sha": "c" * 40}, "review")


def test_changed_remote_head_becomes_visible_needs_you(setup, monkeypatch):
  def changed(*args):
    raise HTTPException(409, "The remote head changed.")
  monkeypatch.setattr(domain, "current_pull", changed)
  assert report(setup)["run"]["state"] == "needs_you"


def test_unknown_queue_policy_never_uses_direct_merge():
  assert domain.merge_blocker(TARGET, REPO, PULL, {**CHECKS, "isMergeQueueEnabled": None})


def test_start_idempotency_reuses_exact_chat_and_does_not_regrant_mode(setup, monkeypatch):
  db, row, principal = setup
  db.add(models.ChatRun(id="existing-run", chat_id=row.chat_id, status="completed"))
  db.commit()
  async def forbidden(**kwargs):
    pytest.fail("Duplicate review start")
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", forbidden)
  principal = Principal(owner=principal.owner, app_id=None)
  body = routes.StartReviews(request_id=row.request_id, mode="review_merge", items=[ITEM])
  result = asyncio.run(routes.start_reviews(1, body, db, principal))
  assert result["run"]["chat_id"] == row.chat_id
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body.model_copy(update={"mode": "review"}), db, principal))
  assert error.value.status_code == 409


def test_start_persists_selection_before_task_admission(setup, monkeypatch):
  db, row, principal = setup
  monkeypatch.setattr(domain, "inspect_target", lambda *args: TARGET)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda db: {"provider": "codex", "model": "gpt-5"})
  called = []
  async def start(**kwargs):
    created = db.query(models.ContributionReviewRun).filter_by(request_id="brand-new").one()
    assert created.chat_id == kwargs["chat_id"]
    assert created.targets_json == [TARGET]
    called.append(kwargs)
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", start)
  principal = Principal(owner=principal.owner, app_id=None)
  body = routes.StartReviews(request_id="brand-new", mode="review", items=[ITEM])
  result = asyncio.run(routes.start_reviews(1, body, db, principal))
  assert result["run"]["mode"] == "review"
  assert len(called) == 1


def test_concurrent_outcome_cas_cannot_claim_second_public_action(setup):
  db, row, _ = setup
  other = SessionLocal()
  stale = other.get(models.ContributionReviewRun, row.id)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "merging"})
  with pytest.raises(HTTPException) as error:
    domain.save_outcome(other, stale, domain.key(ITEM), {"state": "merging"})
  assert error.value.status_code == 409
  other.close()


def test_agent_without_explicit_chat_consent_cannot_mint_merge_grant(setup):
  db, row, principal = setup
  body = routes.StartReviews(request_id="not-approved", mode="review_merge", items=[ITEM])
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, principal))
  assert error.value.status_code == 403


def approved_body(request_id="chat-approved"):
  return routes.StartReviews(request_id=request_id, mode="review_merge", items=[ITEM],
    chat_approval={"context": "The owner said: review this exact PR and merge it if safe."})


def test_explicit_chat_consent_stays_in_owning_chat_with_durable_provenance(setup, monkeypatch):
  db, _, principal = setup
  monkeypatch.setattr(domain, "inspect_target", lambda *args: TARGET)
  async def forbidden(**kwargs):
    pytest.fail("Approval must not create a second review conversation")
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", forbidden)
  body = approved_body()
  result = asyncio.run(routes.start_reviews(1, body, db, principal))
  row = db.get(models.ContributionReviewRun, result["run"]["id"])
  assert row.chat_id == principal.chat_id
  assert row.targets_json[0]["approval"] == {
    "source": "chat", "chat_id": principal.chat_id, "run_id": principal.run_id,
    "context": body.chat_approval.context,
  }
  assert row.outcomes_json == {}
  assert f"/{row.id}/outcomes" in result["brief"]
  again = asyncio.run(routes.start_reviews(1, body, db, principal))
  assert again["run"]["id"] == row.id
  # An app confirmation of the very same request observes the existing owner;
  # it neither requires chat provenance nor launches another review.
  owner = Principal(owner=principal.owner, app_id=None)
  app_body = body.model_copy(update={"chat_approval": None})
  assert asyncio.run(routes.start_reviews(1, app_body, db, owner))["run"]["id"] == row.id


@pytest.mark.parametrize("change", ["child", "wrong_app", "missing_chat", "missing_run", "stopped"])
def test_chat_consent_requires_live_top_level_owner_run(setup, change):
  db, _, principal = setup
  if change == "child":
    principal.delegation_id = "delegated-child"
  elif change == "wrong_app":
    principal.app_id = 2
    principal.scope = "app"
  elif change == "missing_chat":
    principal.chat_id = None
  elif change == "missing_run":
    principal.run_id = None
  else:
    db.get(models.ChatRun, principal.run_id).status = "stopped"
    db.commit()
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, approved_body(), db, principal))
  assert error.value.status_code in {403, 409}
  assert db.query(models.ContributionReviewRun).count() == 1


@pytest.mark.parametrize("app_id", [None, 1])
def test_owner_and_app_controls_cannot_forge_chat_provenance(setup, app_id):
  db, _, principal = setup
  principal = Principal(owner=principal.owner, app_id=app_id)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, approved_body(), db, principal))
  assert error.value.status_code == 403


def test_blank_consent_attestation_is_not_approval():
  from pydantic import ValidationError
  with pytest.raises(ValidationError):
    routes.ChatApproval(context="  ")


def test_real_app_scope_guard_still_rejects_other_apps(setup, monkeypatch):
  from app.github_contributions import _validate_submit_app
  db, _, principal = setup
  db.add(models.App(id=2, name="Other", slug="other", source_dir="test-other",
                    github_access=True, token_nonce="other-nonce"))
  db.commit()
  monkeypatch.setattr(routes, "_validate_submit_app", _validate_submit_app)
  principal = Principal(owner=principal.owner, app_id=2, scope="app", app_instance_id="other-nonce")
  body = approved_body().model_copy(update={"chat_approval": None})
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, principal))
  assert error.value.status_code == 403


def test_http_admission_accepts_explicit_chat_consent_but_not_an_unapproved_agent(setup, monkeypatch):
  from fastapi import FastAPI
  from fastapi.testclient import TestClient
  db, _, principal = setup
  application = FastAPI()
  application.include_router(routes.router)
  application.dependency_overrides[routes.get_db] = lambda: db
  application.dependency_overrides[routes.get_principal] = lambda: principal
  monkeypatch.setattr(domain, "inspect_target", lambda *args: TARGET)
  with TestClient(application) as client:
    body = approved_body().model_dump()
    response = client.post("/api/github/contributions/1/review-runs", json={**body, "chat_approval": None})
    assert response.status_code == 403
    response = client.post("/api/github/contributions/1/review-runs", json=body)
    assert response.status_code == 200
    assert response.json()["run"]["chat_id"] == principal.chat_id
    principal.delegation_id = "child"
    response = client.post("/api/github/contributions/1/review-runs", json=body)
    assert response.status_code == 403


def test_chat_cannot_rebind_an_existing_other_conversations_selection(setup):
  db, row, principal = setup
  db.add(models.Chat(id="other-chat", title="Other"))
  db.add(models.ChatRun(id="other-run", chat_id="other-chat", status="running"))
  db.commit()
  principal.chat_id, principal.run_id = "other-chat", "other-run"
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, approved_body(row.request_id), db, principal))
  assert error.value.status_code == 409
  assert error.value.detail["chat_id"] == row.chat_id


@pytest.mark.parametrize("revoke", ["stop", "capability", "nonce", "uninstall"])
def test_chat_admission_rechecks_revocation_after_remote_inspection(setup, monkeypatch, revoke):
  db, _, principal = setup
  def inspect(*args):
    if revoke == "stop":
      db.get(models.ChatRun, principal.run_id).status = "stopped"
    else:
      app = db.get(models.App, 1)
      if revoke == "capability":
        app.github_access = False
      elif revoke == "nonce":
        app.token_nonce = "changed"
      else:
        from app.timeutil import now_naive_utc
        app.deleted_at = now_naive_utc()
    db.commit()
    return TARGET
  monkeypatch.setattr(domain, "inspect_target", inspect)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, approved_body(), db, principal))
  assert error.value.status_code == 409
  assert db.query(models.ContributionReviewRun).count() == 1


def test_peer_owns_exact_merge_so_review_links_to_it_without_public_action(setup, monkeypatch):
  from app import agent_work_claims
  db, _, principal = setup
  db.add(models.Chat(id="peer-chat", title="Existing owner"))
  db.add(models.ChatRun(id="peer-run", chat_id="peer-chat", status="running"))
  db.commit()
  agent_work_claims.claim_work(db, owner_id=principal.owner.id,
    chat_id="peer-chat", run_id="peer-run", work_key=domain.merge_work_key(TARGET),
    summary="Own this exact merge")
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("Another chat owns it"))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "needs_you"
  assert item["review_chat_id"] == "peer-chat"


@pytest.mark.parametrize("state", ["merging", "merge_unknown", "queued", "merged"])
def test_overlapping_batches_in_same_chat_cannot_repeat_exact_public_attempt(setup, monkeypatch, state):
  db, row, principal = setup
  earlier = models.ContributionReviewRun(id="earlier-batch", app_id=1, owner_id=principal.owner.id,
    request_id="earlier-request", mode="review_merge", targets_json=[TARGET],
    outcomes_json={domain.key(TARGET): {"state": state, "head_sha": SHA}},
    chat_id=row.chat_id, github_actor_id="42", app_nonce="nonce")
  db.add(earlier)
  db.commit()
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("Repeated exact public action"))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "needs_you"
  assert item["review_selection_id"] == earlier.id
  assert item["review_chat_id"] == earlier.chat_id


def test_prior_attempt_of_different_head_does_not_consume_current_approval(setup, monkeypatch):
  db, row, principal = setup
  earlier = models.ContributionReviewRun(id="older-head", app_id=1, owner_id=principal.owner.id,
    request_id="older-request", mode="review_merge", targets_json=[{**TARGET, "head_sha": "c" * 40}],
    outcomes_json={domain.key(TARGET): {"state": "merged", "head_sha": "c" * 40}},
    chat_id=row.chat_id, github_actor_id="42", app_nonce="nonce")
  db.add(earlier)
  db.commit()
  monkeypatch.setattr(domain, "perform_merge", lambda *args: {"merged": True, "sha": "landed"})
  assert report(setup)["run"]["items"][0]["state"] == "merged"


@pytest.mark.parametrize("same_grant", [False, True])
def test_concurrent_overlapping_grants_share_one_database_attempt_fence(setup, monkeypatch, same_grant):
  from concurrent.futures import ThreadPoolExecutor
  from threading import Barrier
  from app import agent_work_claims
  db, row, principal = setup
  second = models.ContributionReviewRun(id="concurrent-batch", app_id=1, owner_id=principal.owner.id,
    request_id="concurrent-request", mode="review_merge", targets_json=[TARGET],
    outcomes_json={}, chat_id=row.chat_id, github_actor_id="42", app_nonce="nonce")
  db.add(second)
  db.commit()
  real_claim = agent_work_claims.claim_work
  ready = Barrier(2)
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    ready.wait(timeout=10)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  def arm(row_id):
    with SessionLocal() as session:
      current = session.get(models.ContributionReviewRun, row_id)
      return domain.arm_merge(session, current, TARGET,
        {"state": "all_clear", "head_sha": SHA}, principal)
  with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(arm, [row.id, row.id if same_grant else second.id]))
  assert sum(result is None for result in results) == 1
  assert sum(result is not None and result["state"] == ("merging" if same_grant else "needs_you") for result in results) == 1


def test_reconnected_github_identity_cannot_spend_grant(setup, monkeypatch):
  monkeypatch.setattr(domain, "read", lambda *args: {"id": 99})
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("wrong actor"))
  assert report(setup)["run"]["items"][0]["state"] == "needs_you"


@pytest.mark.parametrize("revoke", ["stop", "capability", "nonce", "uninstall"])
def test_stop_or_permission_revocation_during_preflight_prevents_merge(setup, monkeypatch, revoke):
  db, row, _ = setup
  def checks(*args):
    if revoke == "stop":
      db.get(models.ChatRun, "physical-run").status = "stopped"
    else:
      app = db.get(models.App, 1)
      if revoke == "capability":
        app.github_access = False
      elif revoke == "nonce":
        app.token_nonce = "replacement"
      else:
        from app.timeutil import now_naive_utc
        app.deleted_at = now_naive_utc()
    db.commit()
    return CHECKS
  monkeypatch.setattr(domain, "pull_checks", checks)
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("revoked grant"))
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 409


def test_stopped_review_is_visible_not_forever_reviewing(setup):
  db, row, principal = setup
  db.get(models.ChatRun, "physical-run").status = "stopped"
  db.commit()
  result = routes.list_reviews(1, db, principal)
  assert result["runs"][0]["state"] == "needs_you"
  assert "stopped" in result["runs"][0]["summary"]


def test_bound_run_permission_is_checked_after_remote_preflight(setup, monkeypatch):
  # Check order explicitly: no public attempt can be durably armed first.
  db, row, _ = setup
  def forbid(*args):
    assert row.outcomes_json == {}
    raise HTTPException(409, "Stopped")
  monkeypatch.setattr(routes, "_assert_execution_live", forbid)
  with pytest.raises(HTTPException):
    report(setup)
  assert row.outcomes_json == {}


def test_already_queued_exact_head_is_observed_not_enqueued_again(setup, monkeypatch):
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {
    **CHECKS, "isMergeQueueEnabled": True,
    "mergeQueueEntry": {"id": "existing", "headCommit": {"oid": SHA}},
  })
  monkeypatch.setattr(domain, "enqueue", lambda *args: pytest.fail("already queued"))
  assert report(setup)["run"]["items"][0]["queue_entry_id"] == "existing"


@pytest.mark.parametrize("field,value", [("base_ref", "release"), ("base_sha", "c" * 40)])
def test_same_head_retarget_before_approval_cannot_mint_grant(field, value):
  def gh(cwd, command, endpoint):
    return SimpleNamespace(stdout=json.dumps(REPO if endpoint.endswith("project") else PULL))
  with pytest.raises(HTTPException) as error:
    domain.inspect_target(gh, "/tmp", {**ITEM, field: value}, "review_merge")
  assert error.value.status_code == 409
