"""Durable core workflows: frozen consent, guarded successors and no replay."""
import asyncio
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.chat_writer import create_chat
from app import contribution_review_runs as domain, contribution_review_repairs as repairs, models
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, CHECKS, report

NEW = "c" * 40


def takeover(setup, monkeypatch):
  db, row, principal = setup
  row.mode = "review_fix_merge"
  row.options_json = {"confirmation_scope": "named_pr_repairs_and_reviewed_successors",
    "max_rounds": 2, "provider": "codex", "model": "gpt-5", "reasoning_effort": "xhigh",
    "choice": {"provider": "codex", "model": "gpt-5", "effort": "xhigh"},
    "review_prompt": "Review completely", "fix_prompt": "Fix minimally"}
  row.targets_json = [{**TARGET, "head_repo": "example/project", "head_repo_id": 1,
    "head_ref": "topic", "allowed_files": ["owned.py"]}]
  chat = db.get(models.Chat, row.chat_id)
  chat.provider = "codex"
  chat.agent_settings_json = {"model": "gpt-5", "effort": "xhigh"}
  db.commit()
  monkeypatch.setattr(domain, "base_head", lambda *args: BASE)
  return db, row, principal


def checkout_body(head=SHA):
  return routes.RepairCheckout(**{**ITEM, "head_sha": head}, findings="Fix an exact reviewed bug")


def publish_body(head=SHA, **changes):
  return routes.RepairPublish(**{**ITEM, "head_sha": head, "summary": "Fixed the scoped bug",
    "tests": "Focused tests passed", "tests_passed": True, **changes})


def register_reviewer(db, row, child, target):
  previous = row.outcomes_json.get(domain.key(target), {})
  domain.save_outcome(db, row, domain.key(target), {**previous,
    "reviewer_steps": [*previous.get("reviewer_steps", []), {
      "delegation_id": child.id, "head_sha": target["head_sha"], "base_sha": target["base_sha"],
      "prompt_sha256": child.prompt_sha256, "provider": child.provider,
      "model": child.model, "effort": child.effort}]})


def completed_repairs(db, row, count):
  """A saved, contiguous guarded successor chain, not arbitrary head drift."""
  head = SHA
  attempts = []
  for index in range(count):
    successor = f"{index + 1:040x}"
    attempts.append({"id": f"repair-{index}", "from_sha": head,
      "head_sha": successor, "base_sha": BASE, "state": "pushed"})
    head = successor
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "reviewing",
    "repair_attempts": attempts, "successor": {"head_sha": head, "base_sha": BASE}})
  return head


def test_review_presets_advertises_capabilities_outside_preview_hash(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  before = routes.review_preview(1, routes.ReviewPreview(), db, owner)
  assert routes.review_presets(1, db, owner)["capabilities"] == {"post_review": True}
  assert routes.core_review_presets(db, owner)["capabilities"] == {"post_review": True}
  assert routes.review_preview(1, routes.ReviewPreview(), db, owner) == before
  assert "capabilities" not in before["options"]


@pytest.mark.parametrize("post_review", [False, True])
def test_cli_frozen_review_options_reproduce_the_server_preview(setup, monkeypatch, post_review):
  spec = importlib.util.spec_from_file_location("github_review_cli", Path(__file__).parents[1] / "scripts/github_review.py")
  cli = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(cli)
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  requested = {"post_review": True} if post_review else None
  preview = routes.core_review_preview(routes.ReviewPreview(options=requested), db, owner)
  frozen = cli.frozen_options(preview["options"])
  assert frozen.get("post_review", False) is post_review
  # Start re-resolves exactly these options and must match the previewed hash.
  replay = routes._snapshot(db, routes.ReviewPreview(options=frozen), owner)
  assert routes._fingerprint(replay) == preview["preview_sha256"]


@pytest.mark.parametrize("options", [None, {}, {"max_rounds": None}])
def test_uncapped_preview_admission_and_retry_freeze_the_same_hash(setup, monkeypatch, options):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: TARGET)
  monkeypatch.setattr(domain, "repair_file_scope", lambda *a: ["owned.py"])
  async def admit(**kwargs):
    db.add(models.ChatRun(id="uncapped-admitted", chat_id=kwargs["chat_id"], status="stopped"))
    db.commit()
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  preview = routes.review_preview(1, routes.ReviewPreview(options=options), db, owner)
  assert preview["options"]["max_rounds"] is None
  body = routes.StartReviews(request_id="uncapped-123", mode="review_fix_merge", items=[ITEM],
    options=options, confirmation_scope="named_pr_repairs_and_reviewed_successors",
    preview_sha256=preview["preview_sha256"])
  first = asyncio.run(routes.start_reviews(1, body, db, owner))
  frozen = first["run"]["options"]
  assert frozen["max_rounds"] is None
  assert frozen["preview_sha256"] == preview["preview_sha256"]
  assert "No maximum repair round count" in first["brief"]
  assert "At most" not in first["brief"] and "bounded steps" not in first["brief"]
  monkeypatch.setattr(routes, "_snapshot", lambda *a: pytest.fail("retry must not re-resolve defaults"))
  second = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert second["run"]["options"] == frozen
  assert second["run"]["execution_state"] == "stopped"


@pytest.mark.parametrize("options, expected", [({}, None), ({"max_rounds": None}, None),
  ({"max_rounds": 2}, 2), ({"max_rounds": 21}, 21)])
def test_request_round_limit_matches_resolver(options, expected):
  body = routes.ReviewPreview(options=options)
  assert routes._options_input(body)["max_rounds"] == expected


@pytest.mark.parametrize("limit", [True, False, 0, -1, "5", 2.0])
def test_request_round_limit_rejects_nonpositive_or_noninteger_values(limit):
  with pytest.raises(ValueError):
    routes.ReviewOptions(max_rounds=limit)


@pytest.mark.parametrize("limit", [None, 30])
def test_both_repair_gates_allow_guarded_successors_beyond_former_caps(setup, monkeypatch, limit):
  db, row, principal = takeover(setup, monkeypatch)
  row.options_json = {**row.options_json, "max_rounds": limit}
  db.commit()
  head = completed_repairs(db, row, 25)
  original = dict(row.targets_json[0])
  checkout = {"checkout": "/fake/server-repair", "initial_head_sha": head,
    "allowed_files": ["owned.py"]}
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: checkout)
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: {**checkout,
    "head_sha": NEW, "files": ["owned.py"], "diff_sha256": "d" * 64})
  calls = []
  def push(*a, **kwargs):
    db.refresh(row)
    assert row.outcomes_json[domain.key(ITEM)]["repair_attempts"][-1]["state"] == "pushing"
    calls.append(1)
  monkeypatch.setattr(repairs, "push_repair", push)
  assert asyncio.run(routes.repair_checkout(1, row.id, checkout_body(head), db, principal))["checkout"] == checkout
  result = asyncio.run(routes.publish_repair(1, row.id, publish_body(head), db, principal))
  assert len(result["run"]["items"][0]["repair_attempts"]) == 26
  assert domain.effective_target(row, original)["head_sha"] == NEW
  assert row.targets_json == [original] and row.options_json["max_rounds"] == limit
  asyncio.run(routes.publish_repair(1, row.id, publish_body(head), db, principal))
  assert calls == [1]
  with pytest.raises(HTTPException) as error:
    report(setup, head_sha=NEW, tests_passed=True, reviewed_base_sha=BASE,
      independent_receipt_id="prior-head-review")
  assert error.value.status_code == 422


@pytest.mark.parametrize("limit", [2, 5, 21, "missing"])
@pytest.mark.parametrize("gate", ["checkout", "publish"])
def test_both_repair_gates_keep_saved_finite_and_missing_limit_grants(setup, monkeypatch, limit, gate):
  db, row, principal = takeover(setup, monkeypatch)
  options = dict(row.options_json)
  if limit == "missing":
    options.pop("max_rounds")
    count = 5
  else:
    options["max_rounds"] = limit
    count = limit
  row.options_json = options
  db.commit()
  head = completed_repairs(db, row, count)
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous,
    "checkout": {"initial_head_sha": head, "checkout": "/fake/server-repair"}})
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("exhausted grant"))
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: pytest.fail("exhausted grant"))
  action = routes.repair_checkout if gate == "checkout" else routes.publish_repair
  body = checkout_body(head) if gate == "checkout" else publish_body(head)
  with pytest.raises(HTTPException) as error:
    asyncio.run(action(1, row.id, body, db, principal))
  assert error.value.status_code == 409 and "limit" in str(error.value.detail)
  assert row.options_json == options


@pytest.mark.parametrize("options, limit", [(None, 5), ({}, 5), ({"max_rounds": 2}, 2),
  ({"max_rounds": None}, None)])
def test_brief_reads_frozen_limit_without_widening_historical_grants(setup, options, limit):
  row = setup[1]
  row.mode = "review_fix_merge"
  row.options_json = options
  brief = domain.takeover_brief(row)
  assert domain.repair_round_limit(row) == limit
  if limit is None:
    assert "No maximum repair round count" in brief
  else:
    assert f"At most {limit} repair rounds per PR." in brief
    domain.require_repair_round_available(row, [{}] * (limit - 1))
    with pytest.raises(HTTPException):
      domain.require_repair_round_available(row, [{}] * limit)
  assert row.options_json == options


def test_legacy_default_limit_retry_keeps_frozen_hash_and_rejects_explicit_uncapping(setup, monkeypatch):
  db, row, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  row.options_json = {"max_rounds": 5, "input_options": {"review_prompt": "Frozen", "max_rounds": 5, "autopilot": False},
    "input_agent": None, "confirmation_scope": None, "preview_sha256": "d" * 64}
  db.commit()
  frozen = dict(row.options_json)
  monkeypatch.setattr(routes, "_snapshot", lambda *a: pytest.fail("old retry must not resolve current defaults"))
  body = routes.StartReviews(request_id=row.request_id, mode=row.mode, items=[ITEM],
    options={"review_prompt": "Frozen"}, preview_sha256=frozen["preview_sha256"])
  assert asyncio.run(routes.start_reviews(1, body, db, owner))["run"]["options"] == frozen
  for options in [{"review_prompt": "Frozen", "max_rounds": None}, {"review_prompt": "Frozen", "max_rounds": 21}]:
    changed = body.model_copy(update={"options": routes.ReviewOptions(**options)})
    with pytest.raises(HTTPException) as error:
      asyncio.run(routes.start_reviews(1, changed, db, owner))
    assert error.value.status_code == 409
  assert row.options_json == frozen


@pytest.mark.parametrize("limit", [2, 21])
def test_saved_explicit_finite_retry_keeps_hash_and_cannot_omit_or_uncap_limit(setup, monkeypatch, limit):
  db, row, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  row.options_json = {"max_rounds": limit, "input_options": {"max_rounds": limit, "autopilot": False},
    "input_agent": None, "confirmation_scope": None, "preview_sha256": "d" * 64}
  db.commit()
  frozen = dict(row.options_json)
  monkeypatch.setattr(routes, "_snapshot", lambda *a: pytest.fail("frozen retry must not resolve"))
  body = routes.StartReviews(request_id=row.request_id, mode=row.mode, items=[ITEM],
    options={"max_rounds": limit}, preview_sha256=frozen["preview_sha256"])
  assert asyncio.run(routes.start_reviews(1, body, db, owner))["run"]["options"] == frozen
  for options in [{}, {"max_rounds": None}]:
    changed = body.model_copy(update={"options": routes.ReviewOptions(**options)})
    with pytest.raises(HTTPException) as error:
      asyncio.run(routes.start_reviews(1, changed, db, owner))
    assert error.value.status_code == 409
  assert row.options_json == frozen


def test_legacy_no_snapshot_retry_does_not_uncap_or_resume_stopped_work(setup, monkeypatch):
  db, row, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  monkeypatch.setattr(routes, "_snapshot", lambda *a: pytest.fail("old grant must not resolve new defaults"))
  async def admit(**kwargs):
    pytest.fail("retry must not revive stopped work")
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  body = routes.StartReviews(request_id=row.request_id, mode=row.mode, items=[ITEM])
  result = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert row.options_json is None and domain.repair_round_limit(row) == 5
  assert result["run"]["execution_state"] == "stopped"
  assert "new defaults as old consent" in result["brief"]


def test_review_only_all_clear_replay_rechecks_current_base(setup, monkeypatch):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  assert report(setup)["run"]["state"] == "complete"
  monkeypatch.setattr(domain, "assert_current_base", lambda *a: (_ for _ in ()).throw(HTTPException(409, "The target base drifted")))
  value = report(setup)["run"]
  assert value["state"] == "needs_you"
  assert "drifted" in value["items"][0]["summary"]


@pytest.mark.parametrize("mode", ["review", "review_merge"])
def test_non_takeover_modes_deny_public_repair(setup, monkeypatch, mode):
  db, row, principal = setup
  row.mode = mode
  db.commit()
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("repair denied"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert error.value.status_code == 403


def test_takeover_needs_distinct_confirmation(setup):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  body = routes.StartReviews(request_id="takeover-1", mode="review_fix_merge", items=[ITEM])
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, owner))
  assert error.value.status_code == 422


def test_preview_fingerprint_freezes_bytes_and_model(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  choice = {"provider": "codex", "model": "gpt-5", "effort": "xhigh"}
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: choice)
  preview = routes.review_preview(1, routes.ReviewPreview(options={"review_prompt": "Exact UTF8 π"}), db, owner)
  assert len(preview["preview_sha256"]) == 64
  assert preview["options"]["review_prompt"] == "Exact UTF8 π"
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {**choice, "effort": "high"})
  body = routes.StartReviews(request_id="preview-1", mode="review", items=[ITEM],
    options={"review_prompt": "Exact UTF8 π"}, preview_sha256=preview["preview_sha256"])
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, owner))
  assert error.value.status_code == 409


def test_retry_uses_frozen_snapshot_not_changed_defaults(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: TARGET)
  async def admit(**kwargs):
    db.add(models.ChatRun(id="first-review", chat_id=kwargs["chat_id"], status="stopped"))
    db.commit()
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  body = routes.StartReviews(request_id="frozen-123", mode="review", items=[ITEM], options={"review_prompt": "Frozen text"})
  first = asyncio.run(routes.start_reviews(1, body, db, owner))
  frozen = first["run"]["options"]
  monkeypatch.setattr(routes, "_snapshot", lambda *a: pytest.fail("retry must not resolve again"))
  second = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert second["run"]["options"] == frozen
  assert routes.get_review(1, second["run"]["id"], db, owner)["run"]["execution_state"] == "stopped"


def test_core_app_less_owner_admission_and_auth(setup, monkeypatch):
  db, _, principal = setup
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: TARGET)
  owner = Principal(owner=principal.owner, app_id=None)
  async def admit(**kwargs):
    assert kwargs["initiated_by_app_id"] is None
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  result = asyncio.run(routes.core_start_reviews(routes.StartReviews(request_id="core-1234", mode="review", items=[ITEM]), db, owner))
  row = db.get(models.ContributionReviewRun, result["run"]["id"])
  assert row.app_id is None and row.app_nonce is None
  assert db.get(models.Chat, row.chat_id).created_by_app_id is None
  assert "/api/github/review-runs/" in result["brief"]
  for bad in [Principal(owner=owner.owner, app_id=1, scope="app"), Principal(owner=owner.owner, app_id=None, scope="media")]:
    with pytest.raises(HTTPException) as error:
      routes.core_list_reviews(db, bad)
    assert error.value.status_code == 403


def test_core_routes_registered_without_install(setup):
  db, row, principal = setup
  row.app_id = None
  row.app_nonce = None
  db.commit()
  application = FastAPI()
  application.include_router(routes.router)
  application.dependency_overrides[routes.get_db] = lambda: db
  application.dependency_overrides[routes.get_principal] = lambda: Principal(owner=principal.owner, app_id=None)
  with TestClient(application) as client:
    assert client.get("/api/github/review-runs").status_code == 200
    assert client.get(f"/api/github/review-runs/{row.id}").status_code == 200
    assert client.get("/api/github/review-presets").status_code == 200


def test_takeover_denies_parent_forged_independent_clear(setup, monkeypatch):
  takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("not independently reviewed"))
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE, independent_receipt_id="invented")
  assert error.value.status_code == 422


def test_independent_review_must_match_frozen_child_prompt(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  child_chat = create_chat(id="independent-child", title="Independent")
  db.add(child_chat)
  db.add(models.ChatRun(id="child-review-run", chat_id=child_chat.id, status="running"))
  child = models.Delegation(id="independent", parent_chat_id=row.chat_id, parent_root_run_id=principal.run_id,
    task_key="independent-review", child_chat_id=child_chat.id, provider="codex", model="gpt-5", effort="xhigh",
    scope="write", cwd="/tmp", prompt_sha256=hashlib.sha256(domain.independent_brief(row, row.targets_json[0]).encode()).hexdigest())
  db.add(child)
  db.commit()
  register_reviewer(db, row, child, row.targets_json[0])
  reviewer = Principal(owner=principal.owner, app_id=None, chat_id=child_chat.id, run_id="child-review-run", delegation_id=child.id)
  body = routes.ReviewOutcome(**ITEM, state="all_clear", summary="Full diff clear", scope=sorted(routes.SCOPE), tests="Passed", tests_passed=True, reviewed_base_sha=BASE)
  result = asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  receipt_id = result["independent_receipt_id"]
  assert asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))["independent_receipt_id"] == receipt_id
  monkeypatch.setattr(domain, "perform_merge", lambda *a: {"merged": True, "sha": "landed"})
  assert report(setup, tests_passed=True, reviewed_base_sha=BASE, independent_receipt_id=receipt_id)["run"]["state"] == "complete"


def configure_repair(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  snapshot = {"checkout": "/fake/server-repair", "initial_head_sha": SHA, "allowed_files": ["owned.py"]}
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: snapshot)
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: {**snapshot, "head_sha": NEW, "files": ["owned.py"], "diff_sha256": "d" * 64})
  asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  return db, row, principal


def test_guarded_push_receipt_is_durable_and_original_grant_immutable(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  original = dict(row.targets_json[0])
  calls = []
  def push(*a, **kwargs):
    db.refresh(row)
    receipt = row.outcomes_json[domain.key(ITEM)]["repair_attempts"][0]
    assert receipt["state"] == "pushing" and receipt["head_sha"] == NEW
    calls.append(1)
  monkeypatch.setattr(repairs, "push_repair", push)
  result = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert row.targets_json == [original]
  assert result["run"]["items"][0]["successor"]["head_sha"] == NEW
  assert domain.effective_target(row, original)["head_sha"] == NEW
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert calls == [1]
  with pytest.raises(HTTPException):
    report(setup, head_sha=NEW, reviewed_base_sha=BASE, tests_passed=True, independent_receipt_id="old-review")


def test_push_confirmed_while_pull_head_projection_lags(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  def lagging_pull(_gh, _cwd, target):
    # Like GitHub right after a push: the pulls API still shows the predecessor.
    if target["head_sha"] != SHA:
      raise HTTPException(409, "This pull request no longer matches the approved selection.")
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", lagging_pull)
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: {"checkout": "/fake/server-repair",
    "initial_head_sha": SHA, "allowed_files": ["owned.py"], "head_repo": "example/project",
    "head_repo_id": 1, "head_ref": "topic", "head_sha": NEW, "files": ["owned.py"], "diff_sha256": "d" * 64})
  pushes = []
  monkeypatch.setattr(repairs, "_read", lambda *a: {"object": {"sha": NEW if pushes else SHA}})
  monkeypatch.setattr(repairs, "_git", lambda *a, **kw: pushes.append(1) or SimpleNamespace(returncode=0))
  result = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  item = result["run"]["items"][0]
  assert item["repair_attempts"][0]["state"] == "pushed"
  assert item["successor"]["head_sha"] == NEW
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert pushes == [1]


def test_lost_push_receipt_reconciles_without_second_public_request(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  calls = []
  def lose(*a, **kwargs):
    calls.append(1)
    raise TimeoutError()
  monkeypatch.setattr(repairs, "push_repair", lose)
  result = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert result["run"]["items"][0]["repair_attempts"][0]["state"] == "push_unknown"
  assert row.targets_json[0]["head_sha"] == SHA
  result = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert result["run"]["items"][0]["repair_attempts"][0]["state"] == "pushed"
  assert calls == [1]


@pytest.mark.parametrize("block", ["stop", "actor", "failed_tests", "model"])
def test_repair_blocks_revoked_context_before_attempt(setup, monkeypatch, block):
  db, row, principal = configure_repair(setup, monkeypatch)
  if block == "stop":
    db.get(models.ChatRun, principal.run_id).status = "stopped"
  elif block == "actor":
    monkeypatch.setattr(domain, "read", lambda *a: {"id": 99})
  elif block == "model":
    db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "other", "effort": "xhigh"}
  db.commit()
  monkeypatch.setattr(repairs, "push_repair", lambda *a: pytest.fail("revoked repair"))
  with pytest.raises(HTTPException):
    asyncio.run(routes.publish_repair(1, row.id, publish_body(tests_passed=block != "failed_tests"), db, principal))
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")


def test_arbitrary_successor_without_receipt_is_rejected(setup, monkeypatch):
  db, row, _ = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "reviewing", "successor": {"head_sha": NEW}})
  with pytest.raises(HTTPException):
    domain.effective_target(row, row.targets_json[0])


def test_round_limit_and_ambiguous_push_deny_new_checkout(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "needs_you", "repair_attempts": [{"from_sha": SHA, "head_sha": NEW, "state": "push_unknown"}]})
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("repeat ambiguous predecessor"))
  with pytest.raises(HTTPException):
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))


def test_observation_reconciles_push_after_stop_without_agent_admission(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  calls = []
  def lose(*a, **kwargs):
    calls.append(1)
    raise TimeoutError()
  monkeypatch.setattr(repairs, "push_repair", lose)
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  async def forbidden(**kwargs):
    pytest.fail("observation cannot restart agents")
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", forbidden)
  result = asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert result["run"]["execution_state"] == "stopped"
  assert result["run"]["state"] == "needs_you"
  assert result["run"]["items"][0]["repair_attempts"][0]["state"] == "pushed"
  assert calls == [1]


def test_model_choice_drift_blocks_merge_not_only_repair(setup, monkeypatch):
  db, row, _ = takeover(setup, monkeypatch)
  db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "other", "effort": "xhigh"}
  db.commit()
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("wrong frozen model"))
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 409


def test_review_frozen_rename_scope_includes_both_paths():
  import json
  def gh(*a):
    return SimpleNamespace(stdout=json.dumps([[{"filename": "new.py", "previous_filename": "old.py", "status": "renamed"}]]))
  assert domain.repair_file_scope(gh, "/tmp", TARGET) == ["new.py", "old.py"]


def test_independent_reviewer_admission_is_durable_same_model_and_never_restarts(setup, monkeypatch):
  from app import delegations
  db, row, principal = takeover(setup, monkeypatch)
  starts = []
  async def fake_start(db, child, prompt, **kwargs):
    assert child.provider == "codex" and child.model == "gpt-5" and child.effort == "xhigh"
    assert child.scope == "write" and child.app_id == 1
    assert "READ-ONLY full-diff review" in prompt
    assert "must not author a repair, mutate public GitHub" in prompt
    assert child.prompt_sha256 == hashlib.sha256(prompt.encode()).hexdigest()
    assert row.outcomes_json[domain.key(ITEM)]["reviewer_steps"][0]["delegation_id"] == child.id
    db.add(models.ChatRun(id="saved-child", chat_id=child.child_chat_id, status="stopped"))
    db.commit()
    starts.append(child.id)
    return True
  monkeypatch.setattr(delegations, "ensure_delegation_started", fake_start)
  first = asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  second = asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  assert first["delegation"]["id"] == second["delegation"]["id"]
  assert len(starts) == 1


def reviewer_context(setup, monkeypatch):
  db, row, parent = takeover(setup, monkeypatch)
  target = row.targets_json[0]
  db.add(create_chat(id="registered-review-child", title="Independent"))
  db.add(models.ChatRun(id="registered-review-run", chat_id="registered-review-child", status="running"))
  child = models.Delegation(id="registered-review", parent_chat_id=row.chat_id,
    parent_root_run_id=parent.run_id, task_key="review-" + hashlib.sha256(
      f"{row.id}:{domain.key(target)}:{target['head_sha']}:{target['base_sha']}".encode()).hexdigest(),
    child_chat_id="registered-review-child", provider="codex", model="gpt-5", effort="xhigh",
    scope="write", cwd="/tmp", prompt_sha256=hashlib.sha256(domain.independent_brief(row, target).encode()).hexdigest())
  db.add(child)
  db.commit()
  register_reviewer(db, row, child, target)
  reviewer = Principal(owner=parent.owner, app_id=None, chat_id=child.child_chat_id,
    run_id="registered-review-run", delegation_id=child.id)
  body = routes.ReviewOutcome(**ITEM, state="all_clear", summary="Full diff reviewed",
    scope=sorted(routes.SCOPE), tests="Passing tests", tests_passed=True, reviewed_base_sha=BASE)
  return db, row, parent, child, reviewer, body


def test_durable_reviewer_reports_after_spawning_turn_completes(setup, monkeypatch):
  db, row, parent, _, reviewer, body = reviewer_context(setup, monkeypatch)
  db.get(models.ChatRun, parent.run_id).status = "completed"
  db.commit()
  assert asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))["independent_receipt_id"]


def test_durable_wait_does_not_revoke_completed_parent_reviewer(setup, monkeypatch):
  from datetime import timedelta
  from app.timeutil import now_naive_utc
  db, row, parent, _, reviewer, body = reviewer_context(setup, monkeypatch)
  db.get(models.ChatRun, parent.run_id).status = "completed"
  db.add(models.ChatWait(id="review-wait", chat_id=row.chat_id,
    created_by_run_id=parent.run_id, root_run_id=parent.run_id,
    description="Await independent review", condition_owner="Independent reviewer",
    kind="timer", deadline_at=now_naive_utc() + timedelta(hours=1),
    next_check_at=now_naive_utc() + timedelta(minutes=5), status="armed"))
  db.commit()
  assert asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))["independent_receipt_id"]


def test_unrelated_live_turn_cannot_revive_stopped_reviewer_root(setup, monkeypatch):
  db, row, parent, _, reviewer, body = reviewer_context(setup, monkeypatch)
  db.get(models.ChatRun, parent.run_id).status = "stopped"
  db.add(models.ChatRun(id="unrelated-parent-run", chat_id=row.chat_id,
    root_run_id="unrelated-parent-run", status="running"))
  db.commit()
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code == 409


def test_stopped_continuation_revokes_its_original_reviewer_root(setup, monkeypatch):
  db, row, parent, _, reviewer, body = reviewer_context(setup, monkeypatch)
  db.get(models.ChatRun, parent.run_id).status = "completed"
  db.add(models.ChatRun(id="stopped-continuation", chat_id=row.chat_id,
    root_run_id=parent.run_id, status="stopped"))
  db.add(models.ChatRun(id="unrelated-parent-run", chat_id=row.chat_id,
    root_run_id="unrelated-parent-run", status="running"))
  db.commit()
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code == 409


@pytest.mark.parametrize("goal_status, admitted", [("open", True), ("stopped", False)])
def test_goal_owned_reviewer_follows_goal_lifecycle(setup, monkeypatch, goal_status, admitted):
  db, row, parent, child, reviewer, body = reviewer_context(setup, monkeypatch)
  db.add(models.ChatGoal(id="review-goal", chat_id=row.chat_id,
    objective="Review", status=goal_status))
  root = db.get(models.ChatRun, parent.run_id)
  root.goal_id = "review-goal"
  root.status = "completed"
  child.parent_root_run_id = "review-goal"
  db.add(models.ChatRun(id="unrelated-parent-run", chat_id=row.chat_id,
    root_run_id="unrelated-parent-run", status="running"))
  db.commit()
  if admitted:
    assert asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))["independent_receipt_id"]
  else:
    with pytest.raises(HTTPException) as error:
      asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
    assert error.value.status_code == 409


def test_spawning_root_stop_during_preflight_blocks_evidence(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, parent, _, reviewer, body = reviewer_context(setup, monkeypatch)
  def preflight(*args):
    with SessionLocal() as other:
      other.get(models.ChatRun, parent.run_id).status = "stopped"
      other.add(models.ChatRun(id="unrelated-parent-run", chat_id=row.chat_id,
        root_run_id="unrelated-parent-run", status="running"))
      other.commit()
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", preflight)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code == 409
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("independent_reviews")


def test_goal_hold_during_preflight_blocks_evidence(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, parent, child, reviewer, body = reviewer_context(setup, monkeypatch)
  db.add(models.ChatGoal(id="review-goal", chat_id=row.chat_id,
    objective="Review", status="open"))
  db.get(models.ChatRun, parent.run_id).goal_id = "review-goal"
  child.parent_root_run_id = "review-goal"
  db.commit()
  def preflight(*args):
    with SessionLocal() as other:
      other.get(models.ChatGoal, "review-goal").status = "stopped"
      other.commit()
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", preflight)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code == 409
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("independent_reviews")


@pytest.mark.parametrize("block", ["legacy_read", "unregistered", "wrong_head", "wrong_base", "step_hash", "step_model", "step_effort"])
def test_independent_evidence_requires_current_server_registered_exact_step(setup, monkeypatch, block):
  db, row, _, child, reviewer, body = reviewer_context(setup, monkeypatch)
  previous = dict(row.outcomes_json[domain.key(ITEM)])
  if block == "legacy_read":
    child.scope = "read"
  elif block == "unregistered":
    previous["reviewer_steps"] = []
  else:
    field, value = {"wrong_head": ("head_sha", NEW), "wrong_base": ("base_sha", NEW),
      "step_hash": ("prompt_sha256", "f" * 64), "step_model": ("model", "different"),
      "step_effort": ("effort", "different")}[block]
    previous["reviewer_steps"] = [{**previous["reviewer_steps"][0], field: value}]
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), previous)
  monkeypatch.setattr(domain, "current_pull", lambda *a: pytest.fail("unregistered/legacy reviewer must be denied before I/O"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code in {403, 409}
  assert not row.outcomes_json[domain.key(ITEM)].get("independent_reviews")
  assert child.scope == ("read" if block == "legacy_read" else "write")


@pytest.mark.parametrize("block", ["legacy_read", "unregistered"])
def test_existing_legacy_or_unregistered_reviewer_is_never_reinterpreted_or_restarted(setup, monkeypatch, block):
  from app import delegations
  db, row, parent, child, _, _ = reviewer_context(setup, monkeypatch)
  if block == "legacy_read":
    child.scope = "read"
    db.commit()
  else:
    domain.save_outcome(db, row, domain.key(ITEM), {"reviewer_steps": []})
  async def start(*a, **kw):
    pytest.fail("saved reviewer must not be restarted")
  monkeypatch.setattr(delegations, "ensure_delegation_started", start)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, parent))
  assert error.value.status_code == 403
  assert child.scope == ("read" if block == "legacy_read" else "write")


@pytest.mark.parametrize("block", ["cancelled", "legacy_read", "model", "prompt", "unregistered", "parent_stop", "child_stop", "parent_binding", "child_binding"])
def test_independent_evidence_rechecks_identity_and_execution_after_io(setup, monkeypatch, block):
  import datetime
  from app.database import SessionLocal
  db, row, parent, child, reviewer, body = reviewer_context(setup, monkeypatch)
  def preflight(*a):
    with SessionLocal() as other:
      saved = other.get(models.Delegation, child.id)
      if block == "cancelled":
        saved.cancelled_at = datetime.datetime.now(datetime.timezone.utc)
      elif block == "legacy_read":
        saved.scope = "read"
      elif block == "model":
        saved.model = "different"
      elif block == "prompt":
        saved.prompt_sha256 = "f" * 64
      elif block == "parent_binding":
        saved.parent_chat_id = saved.child_chat_id
      elif block == "child_binding":
        saved.child_chat_id = row.chat_id
      elif block == "unregistered":
        saved_row = other.get(models.ContributionReviewRun, row.id)
        saved_row.outcomes_json = {domain.key(ITEM): {"reviewer_steps": []}}
      else:
        token = parent.run_id if block == "parent_stop" else reviewer.run_id
        other.get(models.ChatRun, token).status = "stopped"
      other.commit()
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", preflight)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code in {403, 409}
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("independent_reviews")


@pytest.mark.parametrize("action", ["outcome", "checkout", "publish"])
def test_trusted_registered_reviewer_cannot_take_parent_public_actions(setup, monkeypatch, action):
  db, row, _, _, reviewer, body = reviewer_context(setup, monkeypatch)
  endpoint, request = {"outcome": (routes.report_outcome, body),
    "checkout": (routes.repair_checkout, checkout_body()),
    "publish": (routes.publish_repair, publish_body())}[action]
  with pytest.raises(HTTPException) as error:
    asyncio.run(endpoint(1, row.id, request, db, reviewer))
  assert error.value.status_code == 403


def test_stop_during_final_push_preflight_is_rechecked_before_public_io(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  from app.database import SessionLocal
  def preflight(*args, before_push=None):
    with SessionLocal() as other:
      other.get(models.ChatRun, principal.run_id).status = "stopped"
      other.commit()
    before_push()
    pytest.fail("Public push must not happen after Stop")
  monkeypatch.setattr(repairs, "push_repair", preflight)
  result = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert result["run"]["items"][0]["repair_attempts"][0]["state"] == "push_unknown"


def test_cross_grant_uncertain_push_fence_cannot_be_replayed(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  def lose(*a, **kw):
    raise TimeoutError()
  monkeypatch.setattr(repairs, "push_repair", lose)
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  second = models.ContributionReviewRun(id="second-repair", app_id=1, owner_id=row.owner_id,
    request_id="second-repair-request", mode=row.mode, github_actor_id=row.github_actor_id,
    app_nonce=row.app_nonce, options_json=dict(row.options_json), targets_json=list(row.targets_json),
    outcomes_json={domain.key(ITEM): {"state": "repairing", "checkout": row.outcomes_json[domain.key(ITEM)]["checkout"]}},
    chat_id=row.chat_id)
  db.add(second)
  db.commit()
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("overlapping uncertain attempt"))
  result = asyncio.run(routes.publish_repair(1, second.id, publish_body(), db, principal))
  assert "earlier" in result["blocked"].lower()


def test_confirmed_successor_with_fresh_independent_full_diff_can_merge(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: None)
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  target = domain.effective_target(row, row.targets_json[0])
  assert target["head_sha"] == NEW and row.targets_json[0]["head_sha"] == SHA
  db.add(create_chat(id="successor-review-child", title="Full successor review"))
  db.add(models.ChatRun(id="successor-review-run", chat_id="successor-review-child", status="running"))
  child = models.Delegation(id="successor-review", parent_chat_id=row.chat_id, parent_root_run_id=principal.run_id,
    task_key="fresh-successor", child_chat_id="successor-review-child", provider="codex", model="gpt-5", effort="xhigh",
    scope="write", cwd="/tmp", prompt_sha256=hashlib.sha256(domain.independent_brief(row, target).encode()).hexdigest())
  db.add(child)
  db.commit()
  register_reviewer(db, row, child, target)
  child_principal = Principal(owner=principal.owner, app_id=None, chat_id=child.child_chat_id,
    run_id="successor-review-run", delegation_id=child.id)
  body = routes.ReviewOutcome(**{**ITEM, "head_sha": NEW}, state="all_clear", summary="Full diff and combined result reviewed",
    scope=sorted(routes.SCOPE), tests="Fresh tests passed", tests_passed=True, reviewed_base_sha=BASE)
  receipt = asyncio.run(routes.independent_review(1, row.id, body, db, child_principal))["independent_receipt_id"]
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "headRefOid": NEW})
  merged = []
  monkeypatch.setattr(domain, "perform_merge", lambda *a: merged.append(a[2]["head_sha"]) or {"merged": True, "sha": "landed"})
  result = report(setup, head_sha=NEW, tests_passed=True, reviewed_base_sha=BASE, independent_receipt_id=receipt)
  assert result["run"]["state"] == "complete" and merged == [NEW]
  assert result["run"]["items"][0]["approved_head_sha"] == SHA


@pytest.mark.parametrize("change", ["rights", "branch", "repo"])
def test_repair_exact_destination_identity_and_rights_are_required(monkeypatch, change):
  import json
  target = {**TARGET, "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic"}
  head = {"sha": SHA, "ref": "topic", "repo": {"id": 2, "full_name": "owner/fork"}}
  if change == "branch":
    head["ref"] = "unapproved-topic"
  elif change == "repo":
    head["repo"]["id"] = 3
  pull = {**PULL, "head": head}
  def gh(_cwd, _command, endpoint):
    if endpoint == "repos/example/project":
      value = REPO
    elif endpoint == "repos/owner/fork":
      value = {"id": 2, "permissions": {"push": False}}
    else:
      value = pull
    return SimpleNamespace(stdout=json.dumps(value))
  with pytest.raises(HTTPException):
    repo, live = repairs._live_head(gh, "/tmp", target)
    repairs._head_destination(gh, "/tmp", live)


def test_new_takeover_requires_preview_hash_not_merely_mode_scope(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  body = routes.StartReviews(request_id="new-takeover", mode="review_fix_merge", items=[ITEM],
    confirmation_scope="named_pr_repairs_and_reviewed_successors")
  monkeypatch.setattr(routes, "_snapshot", lambda *a: pytest.fail("no unpreviewed admission"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, owner))
  assert error.value.status_code == 422


def test_optional_source_chat_can_only_bind_current_authenticated_chat(setup):
  db, _, principal = setup
  body = routes.StartReviews(request_id="source-123", mode="review", items=[ITEM],
    source_chat_id="another-owner-chat", chat_approval={"context": "Owner approved private review"})
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, principal))
  assert error.value.status_code == 403


def test_failed_tests_never_become_all_clear_even_review_only(setup):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=False, tests="Tests failed")
  assert error.value.status_code == 422


def test_app_can_read_owned_core_history_without_borrowing_mutation_authority(setup, monkeypatch):
  from app.github_contributions import _validate_submit_app
  db, row, principal = setup
  row.app_id = None
  row.app_nonce = None
  db.commit()
  app_principal = Principal(owner=principal.owner, app_id=1, scope="app", app_instance_id="nonce")
  monkeypatch.setattr(routes, "_validate_submit_app", _validate_submit_app)
  runs = routes.list_reviews(1, db, app_principal)["runs"]
  assert len(runs) == 1 and runs[0]["id"] == row.id
  assert runs[0]["app_id"] is None and runs[0]["source_chat_id"] == row.chat_id
  assert runs[0]["can_stop"] is False
  detail = routes.get_review(1, row.id, db, app_principal)
  assert detail["run"]["can_stop"] is False
  assert "/api/github/review-runs/" in detail["brief"]
  owner = Principal(owner=principal.owner, app_id=None)
  assert routes.core_get_review(row.id, db, owner)["run"]["can_stop"] is True
  for action in [lambda: routes._row(db, 1, row.id, app_principal),
                 lambda: asyncio.run(routes.stop_review(1, row.id, db, app_principal)),
                 lambda: asyncio.run(routes.observe_review(1, row.id, db, app_principal))]:
    with pytest.raises(HTTPException) as error:
      action()
    assert error.value.status_code == 404


def test_review_stop_from_its_own_chat_refuses_instead_of_ending_the_callers_turn(setup, monkeypatch):
  import app.chat
  db, row, principal = setup  # the fixture's principal is the owning chat's agent run
  assert principal.chat_id == row.chat_id
  monkeypatch.setattr(app.chat, "stop_chat_for", lambda *a, **k: pytest.fail("stopped the calling chat"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.stop_review(1, row.id, db, principal))
  assert error.value.status_code == 409
  assert "your current turn" in error.value.detail


@pytest.mark.parametrize("caller, expected", [("owner", "owner"), ("other_chat_agent", "agent")])
def test_review_stop_records_a_goal_hold_naming_who_stopped_it(setup, monkeypatch, caller, expected):
  import app.chat
  from app.goals import goal_hold, stage_goal_hold
  db, row, principal = setup
  stopper = {
    "owner": Principal(owner=principal.owner, app_id=None),
    "other_chat_agent": Principal(owner=principal.owner, app_id=None, chat_id="coordinator", run_id="coordinator-run"),
  }[caller]
  calls = []

  async def stop_chat_for(chat_id, **kwargs):
    calls.append((chat_id, kwargs))
    return True, []

  monkeypatch.setattr(app.chat, "stop_chat_for", stop_chat_for)
  assert asyncio.run(routes.stop_review(1, row.id, db, stopper))["stopped"] is True
  (chat_id, kwargs), = calls
  assert chat_id == row.chat_id
  # An actor the hold model rejects presents the Goal as an unexplained interruption.
  goal = SimpleNamespace(status="open", hold_json=None, revision=0)
  stage_goal_hold(goal, cause="stop", actor=kwargs["actor"], source_id="stop", actor_id=kwargs["actor_id"])
  assert (goal_hold(goal) or {}).get("actor") == expected


@pytest.mark.parametrize("goal_owned", [False, True])
def test_review_stop_cascades_to_running_reviewer(setup, monkeypatch, goal_owned):
  import app.chat
  from app.database import SessionLocal
  from app.routes import delegations as delegation_routes
  from datetime import UTC, datetime
  db, row, parent, child, _, _ = reviewer_context(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "state": "reviewing"})
  if goal_owned:
    db.add(models.ChatGoal(id="review-goal", chat_id=row.chat_id,
      objective="Review", status="open"))
    db.get(models.ChatRun, parent.run_id).goal_id = "review-goal"
    child.parent_root_run_id = "review-goal"
    db.commit()
  async def stopped(chat_id, **kwargs):
    assert chat_id == row.chat_id
    return True, []
  cancelled = []
  async def cancel_child(delegation_id):
    cancelled.append(delegation_id)
    with SessionLocal() as other:
      other.get(models.Delegation, delegation_id).cancelled_at = datetime.now(UTC)
      other.commit()
    return True
  monkeypatch.setattr(app.chat, "stop_chat_for", stopped)
  monkeypatch.setattr(delegation_routes, "cancel_delegation_execution", cancel_child)
  result = asyncio.run(routes.stop_review(1, row.id, db,
    Principal(owner=parent.owner, app_id=None)))
  assert result["stopped"] is True
  assert cancelled == [child.id]
  db.refresh(child)
  assert child.cancelled_at is not None


def test_review_stop_fences_completed_parent_before_child_cancel_wait(setup, monkeypatch):
  from app import chat_queue
  from app.database import SessionLocal
  from app.routes import delegations as delegation_routes
  db, row, parent, child, reviewer, body = reviewer_context(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "state": "reviewing"})
  db.get(models.ChatRun, parent.run_id).status = "completed"
  db.commit()
  real_cascade = delegation_routes.cancel_active_for_parent
  cascade_started = asyncio.Event()

  async def cascade(*args):
    cascade_started.set()
    return await real_cascade(*args)

  monkeypatch.setattr(delegation_routes, "cancel_active_for_parent", cascade)

  async def exercise():
    async with chat_queue.get_transition_lock(child.child_chat_id):
      stopping = asyncio.create_task(routes.stop_review(1, row.id, db,
        Principal(owner=parent.owner, app_id=None)))
      await asyncio.wait_for(cascade_started.wait(), 10)
      assert not stopping.done()  # The child cancellation still awaits its lock.
      with SessionLocal() as evidence_db:
        with pytest.raises(HTTPException) as error:
          await routes.independent_review(1, row.id, body, evidence_db, reviewer)
        assert error.value.status_code == 409
    assert (await asyncio.wait_for(stopping, 10))["stopped"] is True

  asyncio.run(exercise())


def test_review_stop_reports_child_timeout_and_keeps_evidence_revoked(setup, monkeypatch):
  import app.chat
  from app.database import SessionLocal
  db, row, parent, child, reviewer, body = reviewer_context(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "state": "reviewing"})
  db.get(models.ChatRun, parent.run_id).status = "completed"
  db.commit()
  real_stop_locked = app.chat._stop_chat_for_locked

  async def stop_locked(chat_id, *args, **kwargs):
    if chat_id == child.child_chat_id:
      return False, []  # Child provider is still draining.
    return await real_stop_locked(chat_id, *args, **kwargs)

  monkeypatch.setattr(app.chat, "_stop_chat_for_locked", stop_locked)
  monkeypatch.setattr(app.chat, "is_chat_running", lambda chat_id: chat_id == child.child_chat_id)

  async def exercise():
    result = await routes.stop_review(1, row.id, db,
      Principal(owner=parent.owner, app_id=None))
    assert result["stopped"] is False
    db.refresh(child)
    assert child.cancelled_at is None
    assert db.get(models.ChatRun, reviewer.run_id).status == "running"
    with SessionLocal() as evidence_db:
      with pytest.raises(HTTPException) as error:
        await routes.independent_review(1, row.id, body, evidence_db, reviewer)
      assert error.value.status_code == 409

  asyncio.run(exercise())


def test_review_stop_revokes_later_parent_public_actions(setup, monkeypatch):
  import app.chat
  db, row, parent = takeover(setup, monkeypatch)
  async def stopped(*args, **kwargs):
    return True, []
  monkeypatch.setattr(app.chat, "stop_chat_for", stopped)
  assert asyncio.run(routes.stop_review(1, row.id, db,
    Principal(owner=parent.owner, app_id=None)))["stopped"] is True
  db.add(models.ChatRun(id="later-owner-run", chat_id=row.chat_id,
    root_run_id="later-owner-run", status="running"))
  db.commit()
  later = Principal(owner=parent.owner, app_id=None, chat_id=row.chat_id,
    run_id="later-owner-run")
  monkeypatch.setattr(domain, "current_pull", lambda *args: pytest.fail("stopped grant reached GitHub"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.report_outcome(1, row.id, routes.ReviewOutcome(**ITEM,
      state="all_clear", summary="Clear", scope=sorted(routes.SCOPE),
      tests="Passing", tests_passed=True), db, later))
  assert error.value.status_code == 409


@pytest.mark.parametrize("pause_at", ["current_pull", "pull_checks"])
def test_stop_fences_unadmitted_merge_during_remote_preflight(setup, monkeypatch, pause_at):
  from threading import Event
  from app.database import SessionLocal
  from tests.test_contribution_review_runs import REPO, PULL, CHECKS
  db, row, parent = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "reviewing",
    "independent_reviews": [{"id": "clear", "head_sha": SHA, "base_sha": BASE,
      "state": "all_clear", "tests_passed": True}]})
  entered, release = Event(), Event()
  public_calls = []

  def remote(*args):
    entered.set()
    assert release.wait(10)
    return (REPO, PULL) if pause_at == "current_pull" else CHECKS

  monkeypatch.setattr(domain, pause_at, remote)
  monkeypatch.setattr(domain, "perform_merge", lambda *args:
    public_calls.append("merge") or {"merged": True, "sha": "landed"})
  real_save = domain.save_outcome

  async def exercise():
    fenced = asyncio.Event()
    def save(db_arg, review_row, item_key, outcome):
      result = real_save(db_arg, review_row, item_key, outcome)
      if item_key == routes.REVIEW_STOP_KEY:
        fenced.set()
      return result
    monkeypatch.setattr(domain, "save_outcome", save)
    with SessionLocal() as report_db, SessionLocal() as stop_db:
      body = routes.ReviewOutcome(**ITEM, state="all_clear", summary="Clear",
        scope=sorted(routes.SCOPE), tests="Passing", tests_passed=True,
        reviewed_base_sha=BASE, independent_receipt_id="clear")
      reporting = asyncio.create_task(routes.report_outcome(1, row.id, body, report_db, parent))
      assert await asyncio.to_thread(entered.wait, 10)
      stopping = asyncio.create_task(routes.stop_review(1, row.id, stop_db,
        Principal(owner=parent.owner, app_id=None)))
      try:
        await asyncio.wait_for(fenced.wait(), 2)
        with SessionLocal() as check_db:
          saved = check_db.get(models.ContributionReviewRun, row.id)
          assert routes._review_stop_requested(saved)
          assert not saved.outcomes_json[domain.key(ITEM)].get("merge_attempted")
        assert not public_calls
      finally:
        release.set()
        report_result, stop_result = await asyncio.wait_for(asyncio.gather(
          reporting, stopping, return_exceptions=True), 10)
      assert not public_calls
      assert isinstance(report_result, HTTPException) and report_result.status_code == 409
      assert not isinstance(stop_result, Exception) and stop_result["stopped"] is True

  asyncio.run(exercise())


def test_stop_after_durable_merge_admission_only_reconciles_that_attempt(setup, monkeypatch):
  from threading import Event
  from app.database import SessionLocal
  from tests.test_contribution_review_runs import REPO, PULL
  db, row, parent = setup
  entered, release = Event(), Event()
  public_calls = []

  def admitted_merge(*args):
    public_calls.append("merge")
    entered.set()
    assert release.wait(10)
    return {"merged": True, "sha": "landed"}

  monkeypatch.setattr(domain, "perform_merge", admitted_merge)
  real_save = domain.save_outcome

  async def exercise():
    fenced = asyncio.Event()
    def save(db_arg, review_row, item_key, outcome):
      result = real_save(db_arg, review_row, item_key, outcome)
      if item_key == routes.REVIEW_STOP_KEY:
        fenced.set()
      return result
    monkeypatch.setattr(domain, "save_outcome", save)
    with SessionLocal() as report_db, SessionLocal() as stop_db:
      body = routes.ReviewOutcome(**ITEM, state="all_clear", summary="Clear",
        scope=sorted(routes.SCOPE), tests="Passing")
      reporting = asyncio.create_task(routes.report_outcome(1, row.id, body, report_db, parent))
      assert await asyncio.to_thread(entered.wait, 10)
      stopping = asyncio.create_task(routes.stop_review(1, row.id, stop_db,
        Principal(owner=parent.owner, app_id=None)))
      try:
        await asyncio.wait_for(fenced.wait(), 2)
        with SessionLocal() as check_db:
          saved = check_db.get(models.ContributionReviewRun, row.id)
          assert saved.outcomes_json[domain.key(ITEM)]["merge_attempted"] is True
          assert routes._review_stop_requested(saved)
      finally:
        release.set()
        report_result, stop_result = await asyncio.wait_for(asyncio.gather(
          reporting, stopping, return_exceptions=True), 10)
    assert public_calls == ["merge"]
    assert not isinstance(report_result, Exception)
    assert report_result["run"]["items"][0]["merge_attempted"] is True
    assert not isinstance(stop_result, Exception) and stop_result["stopped"] is True
    with SessionLocal() as observed_db:
      saved = observed_db.get(models.ContributionReviewRun, row.id)
      assert saved.outcomes_json[domain.key(ITEM)]["merge_attempted"] is True
      monkeypatch.setattr(domain, "current_pull", lambda *args:
        (REPO, {**PULL, "merged": True, "merge_commit_sha": "landed"}))
      viewed = await routes.observe_review(1, row.id, observed_db,
        Principal(owner=parent.owner, app_id=None))
      assert viewed["run"]["items"][0]["state"] == "merged"
      assert public_calls == ["merge"]

  asyncio.run(exercise())


def test_stop_allows_read_only_observation_of_prior_merge_attempt(setup, monkeypatch):
  from app.database import SessionLocal
  from tests.test_contribution_review_runs import REPO, PULL
  db, row, parent = setup
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "merge_unknown",
    "head_sha": SHA, "merge_attempted": True})
  owner = Principal(owner=parent.owner, app_id=None)
  assert asyncio.run(routes.stop_review(1, row.id, db, owner))["stopped"] is True
  monkeypatch.setattr(domain, "perform_merge", lambda *args: pytest.fail("observation retried merge"))
  monkeypatch.setattr(domain, "current_pull", lambda *args:
    (REPO, {**PULL, "merged": True, "merge_commit_sha": "landed"}))
  with SessionLocal() as observed_db:
    view = asyncio.run(routes.observe_review(1, row.id, observed_db, owner))["run"]
    assert view["state"] == "complete"
    assert view["items"][0]["merge_sha"] == "landed"
    saved = observed_db.get(models.ContributionReviewRun, row.id)
    assert routes._review_stop_requested(saved)
    assert routes.get_review(1, row.id, observed_db, owner)["run"]["state"] == "complete"
    observed_db.add(models.ChatRun(id="later-observation-run", chat_id=row.chat_id,
      root_run_id="later-observation-run", status="running"))
    observed_db.commit()
    with pytest.raises(HTTPException) as error:
      routes._parent(observed_db, saved, Principal(owner=parent.owner, app_id=None,
        chat_id=row.chat_id, run_id="later-observation-run"))
    assert error.value.status_code == 409


@pytest.mark.parametrize("terminal", [True, False])
def test_model_drift_preserves_terminal_history_but_gates_unfinished_run(setup, monkeypatch, terminal):
  db, row, _ = takeover(setup, monkeypatch)
  target = row.targets_json[0]
  if terminal:
    domain.save_outcome(db, row, domain.key(target), {
      "state": "merged", "head_sha": target["head_sha"], "merge_sha": "landed"})
  chat = db.get(models.Chat, row.chat_id)
  chat.agent_settings_json = {"model": "different", "effort": "high"}
  db.commit()
  view = routes._run_view(db, row)
  assert view["state"] == ("complete" if terminal else "needs_you")
  if terminal:
    assert view["items"][0]["state"] == "merged"
  else:
    assert "model choice" in view["summary"]


def test_app_read_projection_stays_owner_scoped_and_excludes_other_app_grants(setup):
  db, row, principal = setup
  core = models.ContributionReviewRun(id="other-owner-core", app_id=None, owner_id=principal.owner.id + 1,
    request_id="other-owner", mode="review", github_actor_id="42", app_nonce=None,
    targets_json=[TARGET], outcomes_json={}, chat_id=row.chat_id)
  peer_app = models.ContributionReviewRun(id="other-app-grant", app_id=2, owner_id=principal.owner.id,
    request_id="peer-app", mode="review", github_actor_id="42", app_nonce="peer",
    targets_json=[TARGET], outcomes_json={}, chat_id=row.chat_id)
  db.add_all([core, peer_app])
  db.commit()
  assert [r["id"] for r in routes.list_reviews(1, db, principal)["runs"]] == [row.id]
  for hidden in [core, peer_app]:
    with pytest.raises(HTTPException) as error:
      routes.get_review(1, hidden.id, db, principal)
    assert error.value.status_code == 404


@pytest.mark.parametrize("mode", ["review_merge", "review_fix_merge"])
def test_public_review_option_is_refused_outside_review_only(setup, mode):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  body = routes.StartReviews(request_id=f"public-{mode}", mode=mode, items=[ITEM],
    confirmation_scope="named_pr_repairs_and_reviewed_successors" if mode == "review_fix_merge" else None,
    options=routes.ReviewOptions(post_review=True))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, body, db, owner))
  assert error.value.status_code == 422


def overlapping_grant(db, row, *, number=None):
  target = {**row.targets_json[0]}
  if number is not None:
    target.update(number=number, pr_id=f"PR_{number}")
  peer = models.ContributionReviewRun(id="overlapping-grant", app_id=row.app_id,
    owner_id=row.owner_id, request_id="overlapping-request", mode=row.mode,
    github_actor_id=row.github_actor_id, app_nonce=row.app_nonce,
    options_json=dict(row.options_json), targets_json=[target], outcomes_json={}, chat_id=row.chat_id)
  db.add(peer)
  db.commit()
  return peer


@pytest.mark.parametrize("state", ["merge_unknown", "queued"])
@pytest.mark.parametrize("overlap", [False, True])
def test_prepared_repair_cannot_publish_behind_pending_merge(setup, monkeypatch, state, overlap):
  db, row, principal = configure_repair(setup, monkeypatch)
  grant = overlapping_grant(db, row) if overlap else row
  prior = grant.outcomes_json.get(domain.key(ITEM), {})
  domain.save_outcome(db, grant, domain.key(ITEM), {**prior, "state": state,
    "head_sha": SHA, "merge_attempted": True})
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("PUBLIC_PUSH"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")


@pytest.mark.parametrize("state", ["pushing", "push_unknown"])
@pytest.mark.parametrize("overlap", [False, True])
def test_lagging_predecessor_merge_blocked_by_pending_repair(setup, monkeypatch, state, overlap):
  db, row, principal = takeover(setup, monkeypatch)
  grant = overlapping_grant(db, row) if overlap else row
  domain.save_outcome(db, grant, domain.key(ITEM), {"state": "needs_you", "repair_attempts": [
    {"state": state, "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
  prior = row.outcomes_json.get(domain.key(ITEM), {})
  domain.save_outcome(db, row, domain.key(ITEM), {**prior, "independent_reviews": [
    {"id": "clear", "state": "all_clear", "head_sha": SHA, "base_sha": BASE, "tests_passed": True}]})
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("PUBLIC_MERGE"))
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE, independent_receipt_id="clear")
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("merge_attempted")


@pytest.mark.parametrize("state", ["pushing", "push_unknown"])
@pytest.mark.parametrize("confirmed_count", [1, 3])
def test_pending_later_repair_with_new_base_keeps_detail_list_and_observation_readable(setup, monkeypatch, state, confirmed_count):
  db, row, principal = takeover(setup, monkeypatch)
  head = completed_repairs(db, row, confirmed_count)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior, "state": "needs_you",
    "repair_attempts": [*prior["repair_attempts"], {"state": state, "from_sha": head,
      "head_sha": NEW, "base_sha": "d" * 40}]})
  target = domain.effective_target(row, row.targets_json[0])
  assert target["head_sha"] == head and target["base_sha"] == BASE
  owner = Principal(owner=principal.owner, app_id=None)
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "projection lags")))
  detail = routes.get_review(1, row.id, db, owner)["run"]
  listed = routes.list_reviews(1, db, owner)["runs"][0]
  observed = asyncio.run(routes.observe_review(1, row.id, db, owner))["run"]
  for view in (detail, listed, observed):
    assert view["items"][0]["repair_attempts"][-1]["state"] == state
    assert view["items"][0]["successor"] == {"head_sha": head, "base_sha": BASE}
    assert view["execution_state"] == "stopped"


def test_reviewer_registration_failure_leaves_no_recoverable_unauthorized_child(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, principal = takeover(setup, monkeypatch)
  def fail_registration(*args, **kwargs):
    raise RuntimeError("registration storage failed")
  with monkeypatch.context() as patch:
    patch.setattr(domain, "write_outcome", fail_registration, raising=False)
    patch.setattr(domain, "save_outcome", fail_registration)
    with pytest.raises(RuntimeError):
      asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  db.rollback()
  with SessionLocal() as restarted:
    assert restarted.query(models.Delegation).count() == 0
    assert restarted.query(models.Chat).count() == 1
  starts = []
  async def start(**kwargs):
    with SessionLocal() as writer:
      child = writer.query(models.Delegation).filter_by(child_chat_id=kwargs["chat_id"]).one()
      registered = writer.get(models.ContributionReviewRun, row.id)
      assert registered.outcomes_json[domain.key(ITEM)]["reviewer_steps"][0]["delegation_id"] == child.id
      writer.add(models.ChatRun(id="retry-reviewer", chat_id=child.child_chat_id, status="stopped"))
      writer.commit()
    starts.append(kwargs["chat_id"])
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", start)
  asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  assert len(starts) == 1


@pytest.mark.parametrize("same_item", [False, True])
def test_cross_action_concurrent_claims_admit_only_one_pending_attempt(setup, monkeypatch, same_item):
  from concurrent.futures import ThreadPoolExecutor
  from threading import Barrier
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = takeover(setup, monkeypatch)
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
      current = session.get(models.ContributionReviewRun, row.id if action == "repair" else peer.id)
      try:
        if action == "repair":
          result = domain.arm_repair(session, current, target, {}, {"id": "push", "state": "pushing",
            "head_sha": NEW, "base_sha": BASE, "from_sha": SHA}, principal)
        else:
          result = domain.arm_merge(session, current, peer_target, {"head_sha": SHA}, principal)
        return result
      except HTTPException as exc:
        session.rollback()
        assert exc.status_code == 409
        return "blocked"
  with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(arm, ["repair", "merge"]))
  assert results.count(None) == 1 and results.count("blocked") == 1
  with SessionLocal() as restarted:
    receipts = [r.outcomes_json for r in restarted.query(models.ContributionReviewRun).all()]
    assert sum(bool(i.get("repair_attempts") or i.get("merge_attempted"))
      for outcomes in receipts for i in outcomes.values()) == 1


@pytest.mark.parametrize("action", ["merge", "repair"])
def test_cross_action_is_revalidated_after_claim_commit(setup, monkeypatch, action):
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  target = dict(row.targets_json[0])
  real_claim = agent_work_claims.claim_work
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as other:
      current = other.get(models.ContributionReviewRun, peer.id)
      pending = ({"state": "merge_unknown", "head_sha": SHA, "merge_attempted": True}
        if action == "repair" else {"state": "needs_you", "repair_attempts": [
          {"state": "push_unknown", "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
      domain.save_outcome(other, current, domain.key(ITEM), pending)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  with pytest.raises(HTTPException) as error:
    if action == "merge":
      domain.arm_merge(db, row, target, {"head_sha": SHA}, principal)
    else:
      domain.arm_repair(db, row, target, {}, {"state": "pushing", "from_sha": SHA,
        "head_sha": NEW, "base_sha": BASE}, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(row)
  assert not row.outcomes_json


def test_registered_reviewer_survives_restart_before_first_run_and_stopped_reattachment(setup, monkeypatch):
  from app import delegations
  from app.database import SessionLocal
  db, row, principal = takeover(setup, monkeypatch)
  async def crash(*args, **kwargs):
    raise RuntimeError("process lost before child run")
  with monkeypatch.context() as patch:
    patch.setattr(delegations, "ensure_delegation_started", crash)
    with pytest.raises(RuntimeError):
      asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  with SessionLocal() as restarted:
    saved = restarted.query(models.Delegation).one()
    registered = restarted.get(models.ContributionReviewRun, row.id)
    assert registered.outcomes_json[domain.key(ITEM)]["reviewer_steps"][0]["delegation_id"] == saved.id
    assert saved.startup_prompt and not restarted.query(models.ChatRun).filter_by(chat_id=saved.child_chat_id).first()
    saved_id, child_id = saved.id, saved.child_chat_id
  starts = []
  async def start(**kwargs):
    with SessionLocal() as writer:
      writer.add(models.ChatRun(id="recovered-reviewer", chat_id=kwargs["chat_id"], status="stopped"))
      writer.commit()
    starts.append(kwargs["chat_id"])
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", start)
  recovered = asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  assert recovered["delegation"]["id"] == saved_id and starts == [child_id]
  again = asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  assert again["delegation"]["id"] == saved_id and starts == [child_id]


def test_process_loss_after_reviewer_registration_before_commit_rolls_back_both(setup, monkeypatch):
  from app.database import SessionLocal
  db, row, principal = takeover(setup, monkeypatch)
  class ProcessLoss(BaseException):
    pass
  def lose_commit():
    assert db.query(models.Delegation).count() == 1
    # Registration was written, but another process cannot recover either row.
    db.refresh(row)
    assert row.outcomes_json[domain.key(ITEM)]["reviewer_steps"]
    with SessionLocal() as other:
      assert other.query(models.Delegation).count() == 0
      assert not other.get(models.ContributionReviewRun, row.id).outcomes_json
    raise ProcessLoss()
  with monkeypatch.context() as patch:
    patch.setattr(db, "commit", lose_commit)
    with pytest.raises(ProcessLoss):
      asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  with SessionLocal() as other:
    assert other.query(models.Delegation).count() == 0
    assert not other.get(models.ContributionReviewRun, row.id).outcomes_json


@pytest.mark.parametrize("corruption", ["head", "base", "chain"])
def test_pending_receipt_does_not_hide_corrupted_confirmed_successor(setup, monkeypatch, corruption):
  db, row, _ = takeover(setup, monkeypatch)
  head = completed_repairs(db, row, 1)
  previous = row.outcomes_json[domain.key(ITEM)]
  successor = dict(previous["successor"])
  attempts = [dict(previous["repair_attempts"][0]), {"state": "push_unknown", "from_sha": head,
    "head_sha": NEW, "base_sha": "d" * 40}]
  if corruption == "chain":
    attempts[0]["from_sha"] = "e" * 40
  else:
    successor[corruption + "_sha"] = "e" * 40
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "successor": successor, "repair_attempts": attempts})
  with pytest.raises(HTTPException) as error:
    domain.effective_target(row, row.targets_json[0])
  assert error.value.status_code == 409


def test_merge_keeps_new_server_receipts_admitted_during_claim_commit(setup, monkeypatch):
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = setup
  real_claim = agent_work_claims.claim_work
  saved_steps = [{"delegation_id": "newly-registered", "head_sha": SHA, "base_sha": BASE}]
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as writer:
      current = writer.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(writer, current, domain.key(ITEM), {"state": "reviewing",
        "head_sha": SHA, "reviewer_steps": saved_steps})
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: {"merged": True, "sha": "landed"})
  result = report(setup)
  assert result["run"]["items"][0]["reviewer_steps"] == saved_steps
  assert result["run"]["items"][0]["merge_attempted"] is True


@pytest.mark.parametrize("state", ["pushing", "push_unknown"])
def test_stop_observation_and_parent_resume_do_not_clear_pending_repair_admission(setup, monkeypatch, state):
  db, row, principal = takeover(setup, monkeypatch)
  attempt = {"state": state, "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "needs_you", "repair_attempts": [attempt],
    "independent_reviews": [{"id": "clear", "head_sha": SHA, "base_sha": BASE,
      "state": "all_clear", "tests_passed": True}]})
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  def lagging_pull(_gh, _cwd, target):
    if target["head_sha"] != SHA:
      raise HTTPException(409, "PR projection still shows the predecessor")
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", lagging_pull)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("PUBLIC_MERGE"))
  observed = asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert observed["run"]["execution_state"] == "stopped"
  assert observed["run"]["items"][0]["repair_attempts"] == [attempt]
  db.add(models.ChatRun(id="resumed-parent", chat_id=row.chat_id, status="running"))
  db.commit()
  principal.run_id = "resumed-parent"
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE, independent_receipt_id="clear")
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)]["repair_attempts"] == [attempt]
  assert not row.outcomes_json[domain.key(ITEM)].get("merge_attempted")
