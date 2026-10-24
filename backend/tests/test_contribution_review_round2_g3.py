"""Published #1920 follow-up: refusal, scope and independent evidence boundaries."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as reviews, contribution_review_repairs as repairs
from app import contribution_review_repair_actions as actions
from app import models
from app.chat_writer import create_chat
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, SHA, BASE, TARGET, report
from tests.test_contribution_review_workflows import configure_repair, publish_body, takeover, register_reviewer


def test_publish_repair_rejects_rewritten_base_before_push(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  live = "e" * 40
  monkeypatch.setattr(reviews, "base_head", lambda *a: live)
  monkeypatch.setattr(reviews, "read", lambda *_args: {"id": 42} if _args[-1] == "user" else
    {"status": "diverged", "base_commit": {"sha": BASE},
     "merge_base_commit": {"sha": "f" * 40}})
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **k: pytest.fail("validated against rewritten base"))
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **k: pytest.fail("public push"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[reviews.key(ITEM)].get("repair_attempts")


def test_repair_base_ancestry_requires_strict_descendant(monkeypatch):
  target = {**ITEM, "base_sha": BASE}
  approved = {"status": "ahead", "base_commit": {"sha": BASE},
    "merge_base_commit": {"sha": BASE}}
  monkeypatch.setattr(reviews, "read", lambda *a: approved)
  reviews.assert_approved_base_ancestry(None, None, target, BASE)
  reviews.assert_approved_base_ancestry(None, None, target, "e" * 40)
  approved["merge_base_commit"] = {"sha": "f" * 40}
  with pytest.raises(HTTPException):
    reviews.assert_approved_base_ancestry(None, None, target, "e" * 40)


def test_later_needs_you_supersedes_same_delegation_all_clear(setup):
  _db, row, _principal = setup
  row.mode = "review_fix_merge"
  target = row.targets_json[0]
  row.outcomes_json = {reviews.key(target): {"independent_reviews": [
    {"id": "clear", "delegation_id": "same-child", "head_sha": SHA,
      "base_sha": BASE, "state": "all_clear", "tests_passed": True},
    {"id": "followup", "delegation_id": "same-child", "head_sha": SHA,
      "base_sha": BASE, "state": "needs_you", "tests_passed": False}]}}
  body = SimpleNamespace(tests_passed=True, reviewed_base_sha=BASE,
    independent_receipt_id="clear")
  with pytest.raises(HTTPException) as error:
    reviews.require_independent_clear(row, target, body)
  assert error.value.status_code == 422
  row.outcomes_json[reviews.key(target)]["independent_reviews"].append(
    {"id": "fresh", "delegation_id": "different-child", "head_sha": SHA,
     "base_sha": BASE, "state": "all_clear", "tests_passed": True})
  body.independent_receipt_id = "fresh"
  reviews.require_independent_clear(row, target, body)


def test_prepare_intersects_frozen_scope_with_current_pr_files(monkeypatch, tmp_path):
  monkeypatch.setattr(repairs, "_live_head", lambda *a: ({}, {}))
  monkeypatch.setattr(repairs, "_head_destination", lambda *a: ("example/project", 1, "topic"))
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: {"still.py"})
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  monkeypatch.setattr(repairs, "fetch_target_base", lambda *a: BASE)
  def git(_path, *args, **_kwargs):
    return SimpleNamespace(stdout=SHA if args[:2] == ("rev-parse", "FETCH_HEAD^{commit}") else "")
  monkeypatch.setattr(repairs, "_git", git)
  snapshot = repairs.prepare_checkout(None, tmp_path, None,
    {**ITEM, "head_sha": SHA}, ["removed.py", "still.py"])
  assert snapshot["allowed_files"] == ["still.py"]
  with pytest.raises(HTTPException):
    repairs.prepare_checkout(None, tmp_path, None, {**ITEM, "head_sha": SHA}, ["removed.py"])


def test_legacy_uncertain_attempt_fences_only_repository_and_names_owner(setup):
  db, row, _principal = setup
  db.add(create_chat(id="legacy-chat", title="Legacy pending"))
  grant = models.ContributionReviewRun(id="legacy-grant", app_id=row.app_id,
    owner_id=row.owner_id, request_id="legacy-request", mode="review_merge",
    targets_json=[{**ITEM, "repo": "other/project"}],
    outcomes_json={reviews.key({**ITEM, "repo": "other/project"}):
      {"state": "merge_unknown", "merge_attempted": True}},
    chat_id="legacy-chat", github_actor_id=row.github_actor_id,
    app_nonce=row.app_nonce)
  db.add(grant)
  db.commit()
  reviews.require_public_transition_clear(db, row, row.targets_json[0])
  grant.targets_json = [ITEM]
  grant.outcomes_json = {reviews.key(ITEM): {"state": "merge_unknown", "merge_attempted": True}}
  db.commit()
  with pytest.raises(HTTPException) as error:
    reviews.require_public_transition_clear(db, row, row.targets_json[0])
  assert "legacy-grant" in str(error.value.detail) and "legacy-chat" in str(error.value.detail)
  assert "no delayed effect" in error.value.detail
  assert "complete the exact intended GitHub action" in error.value.detail
  assert "observe its matching result" in error.value.detail
  assert "do not replay" in error.value.detail


def test_same_reviewer_delegation_cannot_replace_needs_you_from_new_run(setup, monkeypatch):
  db, row, parent = takeover(setup, monkeypatch)
  db.add(create_chat(id="round2-reviewer", title="Independent reviewer"))
  db.add(models.ChatRun(id="round2-reviewer-run-1", chat_id="round2-reviewer", status="completed"))
  db.add(models.ChatRun(id="round2-reviewer-run-2", chat_id="round2-reviewer", status="running"))
  child = models.Delegation(id="round2-reviewer-delegation", parent_chat_id=row.chat_id,
    parent_root_run_id=parent.run_id, task_key="independent-review",
    child_chat_id="round2-reviewer", provider="codex", model="gpt-5", effort="xhigh",
    scope="write", cwd="/tmp", prompt_sha256=hashlib.sha256(
      reviews.independent_brief(row, row.targets_json[0]).encode()).hexdigest())
  db.add(child)
  db.commit()
  register_reviewer(db, row, child, row.targets_json[0])
  previous = row.outcomes_json[reviews.key(ITEM)]
  reviews.save_outcome(db, row, reviews.key(ITEM), {**previous, "independent_reviews": [{
    "id": "first", "review_run_id": "round2-reviewer-run-1",
    "delegation_id": child.id, "head_sha": SHA, "base_sha": BASE,
    "state": "needs_you", "tests_passed": False}]})
  reviewer = Principal(owner=parent.owner, app_id=None, chat_id=child.child_chat_id,
    run_id="round2-reviewer-run-2", delegation_id=child.id)
  body = routes.ReviewOutcome(**ITEM, state="all_clear", summary="Changed verdict",
    scope=sorted(routes.SCOPE), tests="Passed", tests_passed=True, reviewed_base_sha=BASE)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code == 409
  assert len(row.outcomes_json[reviews.key(ITEM)]["independent_reviews"]) == 1


@pytest.mark.parametrize("mode,expected_calls", [("review", 0), ("review_fix_merge", 1)])
def test_stop_gc_failure_is_best_effort_for_repair_only(setup, monkeypatch, mode, expected_calls):
  db, row, principal = setup
  row.mode = mode
  db.commit()
  calls = []
  def fail():
    calls.append(1)
    raise OSError("simulated GC fault")
  monkeypatch.setattr(actions, "sweep_checkout_receipts_once", fail)
  owner = Principal(owner=principal.owner, app_id=None)
  assert asyncio.run(routes.stop_review(1, row.id, db, owner))["stopped"]
  assert len(calls) == expected_calls


def test_gc_scope_only_classifies_repair_grants(setup):
  db, row, _principal = setup
  reviews.save_outcome(db, row, reviews.key(ITEM), {"state": "reviewing",
    "checkout": {"checkout": "/fake/legacy-checkout"}})
  assert "/fake/legacy-checkout" not in set().union(*actions.checkout_gc_scope(db).values())
  row.mode = "review_fix_merge"
  db.commit()
  assert "/fake/legacy-checkout" in actions.checkout_gc_scope(db)["protected_paths"]


def test_postgres_gc_skips_unsupported_fence_and_warns_once(monkeypatch, caplog):
  class FakeSession:
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def get_bind(self): return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    def execute(self, *_args): pytest.fail("unsupported writer fence used")
  monkeypatch.setattr(actions, "SessionLocal", FakeSession)
  monkeypatch.setattr(actions, "_unsupported_gc_warned", False)
  actions.sweep_checkout_receipts_once()
  actions.sweep_checkout_receipts_once()
  assert caplog.text.count("repair checkout GC skipped") == 1


def test_public_review_brief_keeps_exact_dispatch_freeze_guidance(setup):
  _db, row, _principal = setup
  row.mode = "review"
  row.options_json = {"post_review": True}
  assert "Evidence freezes once dispatch is armed; identical retry is read-only." in reviews.brief(row)


def test_frozen_and_live_scopes_use_one_file_reader(monkeypatch):
  calls = []
  monkeypatch.setattr(repairs, "_changed_files", lambda _gh, _cwd, target:
    calls.append(target["number"]) or {"current.py", "before.py"})
  assert reviews.repair_file_scope(None, None, ITEM) == ["before.py", "current.py"]
  assert calls == [ITEM["number"]]


def test_failed_base_observation_does_not_erase_new_receipt(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  def moved(*_args):
    raise HTTPException(409, "The target base moved")
  def failed_observation(*_args):
    current = row.outcomes_json.get(reviews.key(ITEM), {})
    reviews.save_outcome(db, row, reviews.key(ITEM), {**current,
      "repair_attempts": [{"id": "new-public-receipt", "state": "push_unknown"}]})
    raise HTTPException(409, "GitHub compare was unreadable")
  monkeypatch.setattr(reviews, "assert_current_base", moved)
  monkeypatch.setattr(reviews, "observe_base_advance", failed_observation)
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE)
  assert error.value.status_code == 409
  assert row.outcomes_json[reviews.key(ITEM)]["repair_attempts"][0]["id"] == "new-public-receipt"


def test_canonical_cli_finite_repair_preview_start_retry_and_limit(setup, monkeypatch):
  from scripts import github_review as cli
  db, _row, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(cli, "inspect_pr", lambda *_args: TARGET)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *_args:
    {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(routes.providers, "snapshot_chat_agent_settings", lambda *_args, **_kwargs:
    {"model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(reviews, "inspect_target", lambda *_args: {**TARGET,
    "head_repo": "example/project", "head_repo_id": 1, "head_ref": "topic"})
  monkeypatch.setattr(reviews, "repair_file_scope", lambda *_args: ["owned.py"])
  async def admitted(**kwargs):
    db.add(models.ChatRun(id="round2-cli-run", chat_id=kwargs["chat_id"], status="stopped"))
    db.commit()
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admitted)
  monkeypatch.setattr(cli, "api", lambda _path, **kwargs: routes.review_preview(1,
    routes.ReviewPreview(**kwargs["data"]), db, owner))
  selection = cli.prepare_selection(["example/project#7"], "review_fix_merge", None, 1)
  frozen = cli.freeze_preview(selection, {"fix_prompt": "Fix precisely", "max_rounds": 2})
  assert frozen["options"]["fix_prompt"] == "Fix precisely"
  assert frozen["options"]["max_rounds"] == 2
  body = routes.StartReviews(**cli.start_body(frozen))
  first = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert first["run"]["options"]["max_rounds"] == 2
  assert first["run"]["options"]["fix_prompt"] == "Fix precisely"
  second = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert second["run"]["id"] == first["run"]["id"]
  with pytest.raises(HTTPException):
    reviews.require_repair_round_available(db.get(models.ContributionReviewRun,
      first["run"]["id"]), [{"state": "pushed"}] * 2)


@pytest.mark.parametrize("mode,options", [
  ("review", {"fix_prompt": "Fix privately"}),
  ("review_merge", {"max_rounds": 2}),
])
def test_repair_only_options_rejected_before_review_grant(setup, mode, options):
  db, row, principal = setup
  body = routes.StartReviews(request_id="round2-options", mode=mode, items=[ITEM],
    options=routes.ReviewOptions(**options),
    chat_approval=routes.ChatApproval(context="Review selected PR"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, principal))
  assert error.value.status_code == 422
  assert db.query(models.ContributionReviewRun).count() == 1
