"""Exact selected heads, private reviews and guarded public merge receipts."""
from app import chat_writer
from app.chat_writer import create_chat
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain, models
from app.contribution_errors import ContributionSubmitError
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
REF = {"object": {"sha": BASE}}
CHECKS = {"headRefOid": SHA, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
          "reviewDecision": "APPROVED", "isMergeQueueEnabled": False,
          "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}


@pytest.fixture
def setup(fresh_db, monkeypatch):
  db = SessionLocal()
  owner = models.Owner(username="review-owner", hashed_password="unused")
  db.add(owner)
  db.flush()
  chat = create_chat(id="review-chat", title="Review")
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
  monkeypatch.setattr(domain, "assert_current_base", lambda *args: None)
  monkeypatch.setattr(domain, "pull_checks", lambda *args: CHECKS)
  yield db, row, principal
  db.close()


def report(setup, **changes):
  db, row, principal = setup
  data = {**ITEM, "state": "all_clear", "summary": "Full diff reviewed",
          "scope": sorted(routes.SCOPE), "tests": "Focused tests passed", **changes}
  return asyncio.run(routes.report_outcome(1, row.id, routes.ReviewOutcome(**data), db, principal))


def test_review_evidence_over_4000_characters_survives_reporting(setup):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  summary = "finding " * 700
  tests = "check " * 900
  item = report(setup, summary=summary, tests=tests)["run"]["items"][0]
  assert item["summary"] == summary
  assert item["tests"] == tests
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)]["summary"] == summary
  assert row.outcomes_json[domain.key(ITEM)]["tests"] == tests


def test_review_evidence_models_do_not_cap_prose_at_4000_characters():
  evidence = "e" * 4001
  assert routes.ReviewOutcome(**ITEM, state="needs_you", summary=evidence, tests=evidence).summary == evidence
  assert routes.RepairCheckout(**ITEM, findings=evidence).findings == evidence
  assert routes.RepairPublish(**ITEM, summary=evidence, tests=evidence, tests_passed=True).tests == evidence
  assert routes.DraftReady(**ITEM, reviewed_base_sha=BASE, independent_receipt_id="receipt",
    summary=evidence, scope=sorted(routes.SCOPE), tests=evidence, tests_passed=True).summary == evidence


def test_public_review_formatter_preserves_evidence_past_60000_characters():
  summary = "s" * 61000
  tests = "checks completed after the long summary"
  body = domain.review_comment_body({"state": "all_clear", "summary": summary,
    "tests": tests, "head_sha": SHA})
  assert summary in body
  assert f"**Checks:** {tests}" in body
  assert f"_Reviewed at {SHA[:12]}._" in body
  assert len(body) > 60000


def test_owner_requested_public_review_posts_full_long_evidence(setup, monkeypatch):
  _public_review_run(setup)
  posted = []
  monkeypatch.setattr(domain, "post_review", lambda _gh, _cwd, _target, body:
    posted.append(body) or {"id": 9, "url": "https://github.com/example/project/pull/7#pullrequestreview-9"})
  summary = "s" * 61000
  tests = "checks after the long summary"
  item = report(setup, summary=summary, tests=tests)["run"]["items"][0]
  assert item["public_review"]["state"] == "posted"
  assert len(posted) == 1
  assert summary in posted[0] and tests in posted[0]
  assert f"_Reviewed at {SHA[:12]}._" in posted[0]


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


def test_merge_failure_summary_keeps_safe_github_diagnostic():
  summary = domain.merge_failure_summary(ContributionSubmitError(
    "gh: HTTP 422: merge blocked token=should-not-appear"
  ))
  assert "HTTP 422" in summary
  assert "should-not-appear" not in summary
  assert "[redacted]" in summary


def test_merge_failure_summary_keeps_untrusted_exception_generic():
  assert domain.merge_failure_summary(TimeoutError("internal socket detail")) == (
    "GitHub did not confirm the merge or queue request. Reconcile it before any new action."
  )


def test_lost_merge_receipt_preserves_safe_diagnostic(setup, monkeypatch):
  def lose(*args):
    raise ContributionSubmitError("gh: HTTP 422: merge blocked")
  monkeypatch.setattr(domain, "perform_merge", lose)
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "merge_unknown"
  assert "HTTP 422" in item["summary"]


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


@pytest.mark.parametrize("rollup", [None, {"state": "SUCCESS"}])
def test_merge_and_draft_readiness_agree_on_passing_or_absent_checks(rollup):
  checks = {**CHECKS, "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}
  assert domain.merge_blocker(TARGET, REPO, PULL, checks) is None
  assert domain.readiness_blocker(TARGET, REPO, {**PULL, "draft": True}, checks) is None


@pytest.mark.parametrize("rollup", [{"state": "PENDING"}, {"state": "FAILURE"}, {}])
def test_merge_and_draft_readiness_refuse_any_unsuccessful_rollup(rollup):
  checks = {**CHECKS, "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}
  assert domain.merge_blocker(TARGET, REPO, PULL, checks)
  assert domain.readiness_blocker(TARGET, REPO, {**PULL, "draft": True}, checks)


def test_draft_readiness_still_requires_the_head_commit_check_result():
  checks = {**CHECKS, "commits": {"nodes": []}}
  assert domain.readiness_blocker(TARGET, REPO, {**PULL, "draft": True}, checks)


def test_removed_permission_stops_merge():
  assert domain.merge_blocker(TARGET, {**REPO, "permissions": {}}, PULL, CHECKS)


def test_live_target_branch_not_pull_comparison_base_binds_selection():
  pull = {**PULL, "base": {"ref": "main", "sha": "c" * 40}}
  def gh(cwd, command, endpoint):
    value = REPO if endpoint == "repos/example/project" else REF if "/git/ref/" in endpoint else pull
    return SimpleNamespace(stdout=json.dumps(value))
  target = domain.inspect_target(gh, "/tmp", ITEM, "review_merge")
  assert target["base_sha"] == BASE


@pytest.mark.parametrize("queued", [False, True])
def test_target_branch_advance_after_preflight_stops_before_public_action(setup, monkeypatch, queued):
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {**CHECKS, "isMergeQueueEnabled": queued})
  monkeypatch.setattr(domain, "assert_current_base", lambda *args: (_ for _ in ()).throw(
    HTTPException(409, "The target branch changed.")))
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("stale-base merge"))
  monkeypatch.setattr(domain, "enqueue", lambda *args: pytest.fail("stale-base enqueue"))
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 409
  assert setup[1].outcomes_json == {}


def test_merge_call_binds_sha_without_admin_bypass():
  calls = []
  def gh(*args):
    calls.append(args)
    return SimpleNamespace(stdout=json.dumps({"merged": True}))
  domain.perform_merge(gh, "/tmp", TARGET, REPO)
  assert f"sha={SHA}" in calls[0]
  assert not any("admin" in str(a) for a in calls[0])


@pytest.mark.parametrize("payload", ["null", "[]", '"merged"'])
def test_merge_call_rejects_non_object_confirmation(payload):
  def gh(*args):
    return SimpleNamespace(stdout=payload)
  with pytest.raises(ContributionSubmitError) as error:
    domain.perform_merge(gh, "/tmp", TARGET, REPO)
  assert error.value.code == "merge_response_invalid_shape"
  assert error.value.message == "GitHub returned an unexpected merge confirmation shape."


def test_merge_call_rejects_unreadable_confirmation():
  def gh(*args):
    return SimpleNamespace(stdout="not json")
  with pytest.raises(ContributionSubmitError) as error:
    domain.perform_merge(gh, "/tmp", TARGET, REPO)
  assert error.value.code == "merge_response_unreadable"
  assert error.value.message == "GitHub returned an unreadable merge confirmation."


def test_queue_call_binds_sha_and_never_jumps():
  calls = []
  def gh(*args):
    calls.append(args)
    return SimpleNamespace(stdout=json.dumps({"data": {"enqueuePullRequest": {
      "mergeQueueEntry": {"id": "queue-1"}
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
  monkeypatch.setattr(routes, "resolve_round_choice", lambda db: {
    "provider": "codex", "model": "gpt-5", "effort": "xhigh",
  })
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
  created = db.get(models.Chat, result["run"]["chat_id"])
  assert created.provider == "codex"
  assert created.agent_settings_json == {
    "model": "gpt-5", "effort": "xhigh",
  }


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
  db.add(create_chat(id="other-chat", title="Other"))
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
  db.add(create_chat(id="peer-chat", title="Existing owner"))
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


def test_failed_review_says_why_the_conversation_stopped(setup):
  """Out of credits or a provider block is shown, not only 'stopped'."""
  db, row, principal = setup
  db.get(models.ChatRun, "physical-run").status = "failed"
  db.commit()
  chat = db.get(models.Chat, row.chat_id)
  chat_writer.get_writer().submit(chat_writer.ReplaceTranscript(chat_id=chat.id, messages=[
    {"role": "assistant", "id": "a1", "content": "",
     "blocks": [{"type": "error", "message": "Your workspace is out of credits. Add credits to continue."}]}
  ])).result(30)
  db.expire_all()
  summary = routes.list_reviews(1, db, principal)["runs"][0]["summary"]
  assert summary == "The review conversation stopped: Your workspace is out of credits. Add credits to continue."


def test_open_owner_card_is_waiting_not_stopped(setup):
  """A turn that ended on a question card reads as waiting, whatever its run status."""
  db, row, principal = setup
  db.get(models.ChatRun, "physical-run").status = "interrupted"
  db.get(models.Chat, row.chat_id).pending_question_id = "question-1"
  db.commit()
  for view in (routes.list_reviews(1, db, principal)["runs"][0],
               routes.get_review(1, row.id, db, principal)["run"]):
    assert view["state"] == "needs_you"
    assert view["execution_state"] == "awaiting_owner"
    assert view["summary"] == "The review conversation asked you a question. It continues when you answer."


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
    # GitHub's headCommit is the synthetic merge-group commit, not the PR head.
    "mergeQueueEntry": {"id": "existing", "headCommit": {"oid": "c" * 40}},
  })
  monkeypatch.setattr(domain, "enqueue", lambda *args: pytest.fail("already queued"))
  assert report(setup)["run"]["items"][0]["queue_entry_id"] == "existing"


def test_ambiguous_queue_attempt_reconciles_attached_entry(setup, monkeypatch):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), {
    "state": "merge_unknown", "head_sha": SHA, "merge_attempted": True,
  })
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {
    **CHECKS, "isMergeQueueEnabled": True,
    "mergeQueueEntry": {"id": "existing", "headCommit": {"oid": "c" * 40}},
  })
  monkeypatch.setattr(domain, "enqueue", lambda *args: pytest.fail("must not retry"))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "queued"
  assert item["queue_entry_id"] == "existing"


@pytest.mark.parametrize("field,value", [("base_ref", "release"), ("base_sha", "c" * 40)])
def test_same_head_retarget_before_approval_cannot_mint_grant(field, value):
  def gh(cwd, command, endpoint):
    result = REPO if endpoint == "repos/example/project" else REF if "/git/ref/" in endpoint else PULL
    return SimpleNamespace(stdout=json.dumps(result))
  with pytest.raises(HTTPException) as error:
    domain.inspect_target(gh, "/tmp", {**ITEM, field: value}, "review_merge")
  assert error.value.status_code == 409


@pytest.mark.parametrize("attempted", [False, True])
@pytest.mark.parametrize("head", [None, "d" * 40])
def test_queue_observation_cannot_reconcile_an_unconfirmed_or_new_head(setup, monkeypatch, attempted, head):
  db, row, _ = setup
  if attempted:
    domain.save_outcome(db, row, domain.key(ITEM), {
      "state": "merge_unknown", "head_sha": SHA, "merge_attempted": True,
    })
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {
    **CHECKS, "headRefOid": head, "isMergeQueueEnabled": True,
    "mergeQueueEntry": {"id": "new-head-entry"},
  })
  monkeypatch.setattr(domain, "enqueue", lambda *args: pytest.fail("must not enqueue"))
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("must not merge"))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == ("merge_unknown" if attempted else "needs_you")
  assert "queue_entry_id" not in item
  if attempted:
    assert item["merge_attempted"] is True


def test_live_target_lookup_preserves_slash_containing_branch():
  endpoints = []
  def gh(cwd, command, endpoint):
    endpoints.append(endpoint)
    return SimpleNamespace(stdout=json.dumps(REF))
  assert domain.base_head(gh, "/tmp", ITEM["repo"], "release/stable") == BASE
  assert endpoints == ["repos/example/project/git/ref/heads/release%2Fstable"]


@pytest.mark.parametrize("sha", [None, "", "short", 123])
def test_missing_live_target_head_cannot_confirm_selection(sha):
  def gh(*args):
    return SimpleNamespace(stdout=json.dumps({"object": {"sha": sha}}))
  with pytest.raises(HTTPException) as error:
    domain.base_head(gh, "/tmp", ITEM["repo"], "main")
  assert error.value.status_code == 409


def test_base_advance_rejects_real_ref_lookup():
  def gh(*args):
    return SimpleNamespace(stdout=json.dumps({"object": {"sha": "c" * 40}}))
  with pytest.raises(HTTPException) as error:
    domain.assert_current_base(gh, "/tmp", TARGET)
  assert error.value.status_code == 409


@pytest.mark.parametrize("state", ["queued", "merge_unknown"])
def test_attempted_queue_reconciliation_does_not_require_unchanged_base(setup, monkeypatch, state):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), {
    "state": state, "head_sha": SHA, "merge_attempted": True,
  })
  monkeypatch.setattr(domain, "assert_current_base", lambda *args: pytest.fail("read-only reconciliation"))
  monkeypatch.setattr(domain, "pull_checks", lambda *args: {
    **CHECKS, "isMergeQueueEnabled": True, "mergeQueueEntry": {"id": "existing"},
  })
  monkeypatch.setattr(domain, "enqueue", lambda *args: pytest.fail("must not retry"))
  assert report(setup)["run"]["items"][0]["state"] == "queued"


def _public_review_run(setup):
  db, row, _ = setup
  row.mode = "review"
  row.options_json = {"post_review": True}
  db.commit()
  return db, row


def test_owner_requested_review_is_posted_once_as_a_comment_on_the_exact_head(setup, monkeypatch):
  db, row = _public_review_run(setup)
  posts = []
  def post(_gh, _cwd, target, body):
    db.refresh(row)
    assert row.outcomes_json[domain.key(ITEM)]["public_review"]["state"] == "posting", "receipt before I/O"
    posts.append((target["head_sha"], body))
    return {"id": 9, "url": "https://github.com/example/project/pull/7#pullrequestreview-9"}
  monkeypatch.setattr(domain, "post_review", post)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("review only merged"))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "all_clear"
  assert item["public_review"] == {"state": "posted", "id": 9, "url": "https://github.com/example/project/pull/7#pullrequestreview-9"}
  assert posts[0][0] == SHA and "All clear" in posts[0][1] and "Full diff reviewed" in posts[0][1]
  report(setup, state="needs_you", summary="A later continuation")
  assert len(posts) == 1, "an item's review is posted at most once"


def test_lost_review_post_is_unknown_and_never_replayed(setup, monkeypatch):
  _public_review_run(setup)
  calls = []
  def lost(*args):
    calls.append(1)
    raise RuntimeError("lost response")
  monkeypatch.setattr(domain, "post_review", lost)
  assert report(setup)["run"]["items"][0]["public_review"]["state"] == "unknown"
  report(setup)
  assert calls == [1]


def test_reviews_are_private_without_the_opt_in_or_outside_review_only(setup, monkeypatch):
  db, row, _ = setup
  monkeypatch.setattr(domain, "post_review", lambda *a: pytest.fail("posted without consent"))
  row.mode = "review"
  db.commit()
  assert "public_review" not in report(setup)["run"]["items"][0]
  row.mode = "review_merge"
  row.options_json = {"post_review": True}
  row.outcomes_json = {}
  db.commit()
  monkeypatch.setattr(domain, "perform_merge", lambda *a: {"merged": True, "sha": "landed"})
  assert "public_review" not in report(setup)["run"]["items"][0]


def test_changed_github_account_skips_the_public_review(setup, monkeypatch):
  _public_review_run(setup)
  monkeypatch.setattr(domain, "read", lambda *args: {"id": 99})
  monkeypatch.setattr(domain, "post_review", lambda *a: pytest.fail("posted as another account"))
  assert report(setup)["run"]["items"][0]["public_review"]["state"] == "skipped"


def test_post_review_writes_a_comment_review_bound_to_the_head():
  calls = []
  def gh(_cwd, *args, input_text):
    calls.append((args, json.loads(input_text)))
    return SimpleNamespace(stdout='{"id": 5, "html_url": "https://github.com/x"}')
  assert domain.post_review(gh, "/tmp", TARGET, "Body") == {"id": 5, "url": "https://github.com/x"}
  args, payload = calls[0]
  assert args[-2:] == ("--input", "-")
  assert payload == {"event": "COMMENT", "commit_id": SHA, "body": "Body"}


def test_public_review_transports_large_unicode_evidence_through_subprocess_stdin(tmp_path, monkeypatch):
  from app import github_contribution_git as git

  executable = tmp_path / "gh"
  executable.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
assert sys.argv[1:] == ["api", "--method", "POST", "repos/example/project/pulls/7/reviews", "--input", "-"]
pathlib.Path("received.json").write_text(sys.stdin.read())
print(json.dumps({"id": 5, "html_url": "https://github.com/example/project/pull/7#pullrequestreview-5"}))
''')
  executable.chmod(0o755)
  monkeypatch.setattr(git, "_git_env", lambda _cwd: {"PATH": str(tmp_path), "LC_ALL": "C.UTF-8"})
  body = "finding " * 25000 + '\nRésumé 🧭 "quoted" evidence'
  assert domain.post_review(git._gh, tmp_path, TARGET, body)["id"] == 5
  payload = json.loads((tmp_path / "received.json").read_text())
  assert payload == {"event": "COMMENT", "commit_id": SHA, "body": body}


QUEUE_TIME = "2026-10-07T20:13:24Z"
REMOVED_TIME = "2026-10-07T20:18:09Z"


def queue_timeline():
  # Actual GraphQL shapes: removal has no entry id and beforeCommit is NOT
  # the PR head. Neither reason nor this synthetic SHA supplies identity.
  return {"id": TARGET["pr_id"], "headRefOid": SHA, "state": "OPEN",
    "merged": False, "mergeQueueEntry": None, "timelineItems": {
      "pageInfo": {"hasPreviousPage": False, "hasNextPage": False},
      "nodes": [
        {"__typename": "AddedToMergeQueueEvent", "id": "added-1", "createdAt": QUEUE_TIME},
        {"__typename": "RemovedFromMergeQueueEvent", "id": "removed-1",
         "createdAt": REMOVED_TIME, "beforeCommit": {"oid": "e" * 40},
         "reason": "failed_checks token=PRIVATE ghp_do_not_disclose"}]}}


def queue_graphql(monkeypatch, node):
  """Schema fake for only the read-only fields these routes actually request."""
  calls = []
  def gh(cwd, *args):
    query = next(a[len("query="):] for a in args if a.startswith("query="))
    assert query.startswith("query(") and "mutation" not in query
    assert f"id={TARGET['pr_id']}" in args
    assert "timelineItems(last:100," in query
    assert "ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT" in query
    assert "pageInfo{hasPreviousPage hasNextPage}" in query
    assert "... on AddedToMergeQueueEvent{id createdAt}" in query
    assert "... on RemovedFromMergeQueueEvent{id createdAt}" in query
    assert "beforeCommit" not in query and "reason" not in query
    calls.append(query)
    return SimpleNamespace(stdout=json.dumps({"data": {"node": node}}))
  monkeypatch.setattr(routes, "_gh", gh)
  return gh, calls


def saved_queue_receipt(**changes):
  return {"state": "queued", "head_sha": SHA, "merge_attempted": True,
    "queue_entry_id": "entry-1", "queue_enqueued_at": QUEUE_TIME, **changes}


@pytest.mark.parametrize("legacy", [False, True])
def test_confirmed_queue_removal_uses_admission_not_synthetic_commit(monkeypatch, legacy):
  gh, calls = queue_graphql(monkeypatch, queue_timeline())
  previous = saved_queue_receipt()
  if legacy:
    previous.pop("queue_enqueued_at")
  proof = domain.confirmed_queue_removal(gh, "/tmp", TARGET, previous)
  assert proof == {"queue_entry_id": "entry-1", "head_sha": SHA,
    "added_event_id": "added-1", "enqueued_at": QUEUE_TIME,
    "removed_event_id": "removed-1", "removed_at": REMOVED_TIME}
  assert len(calls) == 1 and "PRIVATE" not in json.dumps(proof)


@pytest.mark.parametrize("case", ["missing_receipt", "unknown_receipt", "missing_events",
  "incomplete", "later_page", "missing_paging", "stale", "ambiguous_legacy", "readded",
  "readded_removed", "equal_time", "unordered", "duplicate_id", "bad_date", "wrong_pr",
  "changed_head", "merged", "closed", "live_entry", "unknown_entry", "prior_proof_changed"])
def test_removal_proof_fails_closed(monkeypatch, case):
  node = queue_timeline()
  previous = saved_queue_receipt()
  timeline = node["timelineItems"]
  events = timeline["nodes"]
  later = [{**events[0], "id": "added-2", "createdAt": "2026-10-07T21:00:00Z"},
           {**events[1], "id": "removed-2", "createdAt": "2026-10-07T21:05:00Z"}]
  if case == "missing_receipt": previous.pop("queue_entry_id")
  elif case == "unknown_receipt": previous["queue_receipt_unconfirmed"] = True
  elif case == "missing_events": timeline["nodes"] = []
  elif case == "incomplete": timeline["pageInfo"]["hasPreviousPage"] = True
  elif case == "later_page": timeline["pageInfo"]["hasNextPage"] = True
  elif case == "missing_paging": timeline.pop("pageInfo")
  elif case == "stale": previous["queue_enqueued_at"] = "2026-10-07T20:00:00Z"
  elif case == "ambiguous_legacy":
    previous.pop("queue_enqueued_at")
    events.extend(later)
  elif case == "readded": events.extend(later[:1])
  elif case == "readded_removed": events.extend(later)
  elif case == "equal_time": events[1]["createdAt"] = QUEUE_TIME
  elif case == "unordered": events.reverse()
  elif case == "duplicate_id": events[1]["id"] = events[0]["id"]
  elif case == "bad_date": events[1]["createdAt"] = "unparseable"
  elif case == "wrong_pr": node["id"] = "PR_other"
  elif case == "changed_head": node["headRefOid"] = "c" * 40
  elif case == "merged": node["merged"] = True
  elif case == "closed": node["state"] = "CLOSED"
  elif case == "live_entry": node["mergeQueueEntry"] = {"id": "entry-2"}
  elif case == "unknown_entry": node.pop("mergeQueueEntry")
  elif case == "prior_proof_changed": previous["queue_removal"] = {"removed_event_id": "older"}
  gh, _ = queue_graphql(monkeypatch, node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, previous) is None


def test_timestamp_receipt_can_match_latest_pair_in_complete_history(monkeypatch):
  node = queue_timeline()
  events = node["timelineItems"]["nodes"]
  events[:0] = [
    {**events[0], "id": "added-older", "createdAt": "2026-10-07T19:00:00Z"},
    {**events[1], "id": "removed-older", "createdAt": "2026-10-07T19:05:00Z"}]
  gh, _ = queue_graphql(monkeypatch, node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt())["added_event_id"] == "added-1"


def test_enqueue_timestamp_is_requested_and_durably_saved(setup, monkeypatch):
  calls = []
  def gh(cwd, *args):
    query = next(a for a in args if a.startswith("query="))
    assert "mergeQueueEntry{id enqueuedAt}" in query
    calls.append(query)
    return SimpleNamespace(stdout=json.dumps({"data": {"enqueuePullRequest": {
      "mergeQueueEntry": {"id": "entry-1", "enqueuedAt": QUEUE_TIME}}}}))
  monkeypatch.setattr(routes, "_gh", gh)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "isMergeQueueEnabled": True})
  result = report(setup)["run"]["items"][0]
  assert result["queue_enqueued_at"] == QUEUE_TIME and result["merge_attempted"]
  assert len(calls) == 1
  assert setup[1].outcomes_json[domain.key(ITEM)]["queue_enqueued_at"] == QUEUE_TIME


def test_requeue_observation_never_overwrites_original_receipt(setup, monkeypatch):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), saved_queue_receipt())
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
    "mergeQueueEntry": {"id": "entry-2", "enqueuedAt": "2026-10-07T21:00:00Z"}})
  result = report(setup)["run"]["items"][0]
  assert result["state"] == "queued" and result["queue_entry_id"] == "entry-1"
  assert result["queue_enqueued_at"] == QUEUE_TIME


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-10-07T20:13:24"])
def test_new_receipt_missing_valid_timestamp_cannot_use_legacy_exception(monkeypatch, timestamp):
  previous = saved_queue_receipt(**domain.queue_receipt({"id": "entry-1", "enqueuedAt": timestamp}))
  assert previous["queue_receipt_unconfirmed"] is True
  gh, calls = queue_graphql(monkeypatch, queue_timeline())
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, previous) is None
  assert calls == []


def test_unknown_attempt_observed_in_queue_never_becomes_legacy_repair_receipt(setup, monkeypatch):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), {
    "state": "merge_unknown", "head_sha": SHA, "merge_attempted": True})
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
    "mergeQueueEntry": {"id": "entry-1", "enqueuedAt": QUEUE_TIME}})
  queued = report(setup)["run"]["items"][0]
  assert queued["state"] == "queued" and queued["queue_receipt_unconfirmed"] is True
  monkeypatch.setattr(domain, "pull_checks", lambda *a: CHECKS)
  queue_graphql(monkeypatch, queue_timeline())
  removed = report(setup)["run"]["items"][0]
  assert removed["state"] == "merge_unknown" and not removed.get("queue_removal")
