"""App-less private review and truthful existing merge settlement."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text

from app import contribution_review_runs as domain, models
from app import schema_migrations
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, CHECKS, REPO, PULL, TARGET, setup, report, snapshot_owning_chat
from scripts import github_review as cli
from scripts.github_review_selection import review_path, start_body, verify_selection
from app.contribution_review_effects import NotDispatched, classify_dispatch_error, graphql_mutation_result
from app.contribution_errors import ContributionSubmitError


CAPABILITIES = {"post_review": True, "post_review_merge": True, "propose_verdict": True,
                "review_fix_merge": True, "draft_takeover": True}

def test_app_less_routes_advertise_only_available_modes():
  paths = {route.path for route in routes.router.routes}
  assert "/api/github/review-preview" in paths
  assert "/api/github/review-runs/{run_id}/outcomes" in paths
  assert "/api/github/review-runs/{run_id}/repairs" in paths
  assert routes.REVIEW_CAPABILITIES == CAPABILITIES
  assert "/api/github/review-runs/{run_id}/ready" in paths


def test_cli_app_adapter_identity_and_body_use_canonical_helpers(monkeypatch):
  from inspect import signature
  for name in ("api", "prepare_selection", "freeze_preview", "verify_selection", "start_body", "review_path"):
    assert callable(getattr(cli, name))
  assert tuple(signature(cli.prepare_selection).parameters) == ("references", "mode", "chat_id", "app_id")
  assert tuple(signature(cli.freeze_preview).parameters) == ("selection", "options", "agent", "allow_mark_ready")
  assert signature(cli.freeze_preview).parameters["allow_mark_ready"].default is False
  monkeypatch.setattr(cli, "inspect_pr", lambda ref: {**ITEM, "title": "Safe change", "url": "https://github.com/example/project/pull/7"})
  snapshot = {"review_prompt": "Review", "merge_prompt": "Merge",
    "provider": "codex", "model": "test-model",
    "reasoning_effort": "high"}
  monkeypatch.setattr(cli, "api", lambda path, **kw: {"options": snapshot, "preview_sha256": routes._fingerprint(snapshot)})
  core = cli.prepare_selection(["example/project#7"], "review", "chat-1")
  app = cli.prepare_selection(["example/project#7"], "review", "chat-1", 9)
  assert core["request_id"] != app["request_id"]
  assert review_path(None, "/review-runs") == "/api/github/review-runs"
  assert review_path(9, "/review-runs") == "/api/github/contributions/9/review-runs"
  frozen = cli.freeze_preview(app)
  verify_selection(frozen)
  assert "app_id" not in start_body(frozen)
  tampered = {**frozen, "app_id": 10}
  with pytest.raises(ValueError):
    verify_selection(tampered)


def test_core_admission_is_null_app_and_app_context_is_read_only(setup, monkeypatch):
  db, existing, principal = setup
  snapshot_owning_chat(db, principal)
  principal.scope = "owner"
  monkeypatch.setattr(domain, "inspect_target", lambda *a: existing.targets_json[0])
  body = routes.StartReviews(request_id="core-request-123", mode="review",
    source_chat_id=principal.chat_id, items=[ITEM],
    chat_approval={"context": "The owner requested this named private review."})
  created = asyncio.run(routes.core_start_reviews(body, db, principal))["run"]
  from app import models
  row = db.get(models.ContributionReviewRun, created["id"])
  assert row.app_id is None and row.app_nonce is None
  assert created["app_id"] is None
  row.options_json = {**row.options_json, "post_review": True}
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "all_clear", "summary": "private finding",
    "tests": "secret tests", "public_review": {"state": "posted", "url": "https://github.com/review/1", "marker": "private marker"}})
  assert any(item["id"] == row.id for item in routes.list_reviews(1, db, principal)["runs"])
  app = Principal(owner=principal.owner, app_id=1)
  projected = next(item for item in routes.list_reviews(1, db, app)["runs"] if item["id"] == row.id)
  assert set(projected) <= {"id", "chat_id", "state", "execution_state", "app_id", "can_stop", "items",
                            "mode", "created_at", "post_review", "details_in_owning_chat"}
  assert projected["details_in_owning_chat"] is True and projected["post_review"] is True
  assert projected["mode"] == "review" and projected["created_at"]
  assert all(set(item) <= {"repo", "number", "head_sha", "base_ref", "base_sha", "state", "public_review"} for item in projected["items"])
  assert projected["items"][0]["base_ref"] == ITEM["base_ref"]
  assert projected["items"][0]["public_review"] == {"state": "posted", "url": "https://github.com/review/1"}
  assert "private finding" not in str(projected) and "secret tests" not in str(projected)
  assert routes.get_review(1, row.id, db, app)["brief"] is None
  with pytest.raises(HTTPException) as observe_denied:
    asyncio.run(routes.observe_review(1, row.id, db, app))
  assert observe_denied.value.status_code == 403
  assert observe_denied.value.detail["code"] == "details_in_owning_chat"
  with pytest.raises(HTTPException) as denied_core:
    routes.core_list_reviews(db, app)
  assert denied_core.value.status_code == 403
  with pytest.raises(HTTPException) as denied:
    routes._row(db, 1, row.id, principal)
  assert denied.value.status_code == 404


def test_owning_chat_without_actual_model_refuses_preview_before_grant(setup):
  db, _row, principal = setup
  body = routes.ReviewPreview()
  with pytest.raises(HTTPException, match="no snapshotted model") as refused:
    routes.review_preview(1, body, db, principal)
  assert refused.value.status_code == 409


@pytest.mark.parametrize("options", [
  {"fix_prompt": "Please fix"}, {"max_rounds": 3}, {"autopilot": True},
])
def test_unenforced_options_refused_before_snapshot(setup, options):
  db, _row, principal = setup
  snapshot_owning_chat(db, principal)
  with pytest.raises(HTTPException, match="not supported") as refused:
    routes.review_preview(1, routes.ReviewPreview(options=options), db, principal)
  assert refused.value.status_code == 422


def test_live_read_failure_after_stop_does_not_admit_new_evidence(setup, monkeypatch):
  db, row, principal = setup
  def stopped_read(*_args):
    domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
    raise HTTPException(409, "PR moved")
  monkeypatch.setattr(domain, "current_pull", stopped_read)
  body = routes.ReviewOutcome(**ITEM, state="needs_you", summary="new private finding",
                              scope=sorted(routes.SCOPE), tests="focused tests")
  with pytest.raises(HTTPException, match="stopped"):
    asyncio.run(routes.report_outcome(1, row.id, body, db, principal))
  db.refresh(row)
  assert domain.key(ITEM) not in row.outcomes_json


def test_private_all_clear_survives_moved_base(setup, monkeypatch):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  monkeypatch.setattr(domain, "assert_current_base", lambda *a: pytest.fail("private history must not check live base"))
  assert report(setup)["run"]["items"][0]["state"] == "all_clear"
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "PR moved")))
  assert report(setup)["run"]["items"][0]["state"] == "all_clear"
  assert asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=row_owner(db, row), app_id=None)))["run"]["items"][0]["state"] == "all_clear"


def test_first_private_all_clear_does_not_depend_on_live_pr_read(setup, monkeypatch):
  """Historical verdict on the selected head survives a move before reporting.

  Removing the review-only shortcut would send this through the live-read
  failure path, which rewrites an all-clear as needs-you.
  """
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(
    HTTPException(409, "PR head moved before the first verdict")))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "all_clear"
  assert item["summary"] == "Full diff reviewed"
  assert item["head_sha"] == ITEM["head_sha"]
  assert item["reviewed_base_sha"] is None


def test_observe_preserves_private_needs_you_evidence_when_live_read_fails(setup, monkeypatch):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  report(setup, state="needs_you", summary="Private finding: race in X")
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "PR moved")))
  asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=row_owner(db, row), app_id=None)))
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)]["summary"] == "Private finding: race in X"


def test_private_finding_records_even_after_head_moved(setup, monkeypatch):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(HTTPException(409, "PR moved")))
  assert report(setup, state="needs_you", summary="Private finding: race in X")["run"]["items"][0]["summary"] == "Private finding: race in X"


def test_private_report_waiting_for_outcome_lock_cannot_pass_stop(setup):
  from app import chat_queue
  db, row, principal = setup
  row.mode = "review"
  db.commit()
  body = routes.ReviewOutcome(**ITEM, state="needs_you", summary="Private finding",
    scope=["tests"], tests="Reviewed")

  async def race():
    async with chat_queue.get_transition_lock(f"review-outcome:{row.id}"):
      pending = asyncio.create_task(routes.report_outcome(1, row.id, body, db, principal))
      await asyncio.sleep(0)
      domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested"})
    with pytest.raises(HTTPException) as refused:
      await pending
    assert refused.value.status_code == 409

  asyncio.run(race())
  db.refresh(row)
  assert domain.key(ITEM) not in row.outcomes_json


def test_completed_run_view_keeps_fact_when_chat_model_later_changes(setup):
  db, row, _ = setup
  row.mode = "review"
  row.options_json = {"provider": "codex", "model": "old-model", "reasoning_effort": "high"}
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "all_clear", "head_sha": ITEM["head_sha"]})
  assert routes._run_view(db, row)["state"] == "complete"
  from app import models
  chat = db.get(models.Chat, row.chat_id)
  chat.agent_settings_json = {"model": "new-model", "effort": "high"}
  db.commit()
  assert routes._run_view(db, row)["state"] == "complete"


def test_null_rollup_does_not_invent_failing_checks():
  checks = {**CHECKS, "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}
  assert domain.merge_blocker(TARGET, REPO, PULL, checks) is None


def row_owner(db, row):
  from app import models
  return db.get(models.Owner, row.owner_id)


def test_proved_predispatch_stop_releases_exact_merge_claim(setup, monkeypatch):
  db, row, _ = setup
  calls = 0
  original = routes._parent

  def stop_after_arm(*args):
    nonlocal calls
    calls += 1
    if calls >= 3:
      raise HTTPException(409, "Stopped before public dispatch")
    return original(*args)

  monkeypatch.setattr(routes, "_parent", stop_after_arm)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("must not dispatch"))
  result = report(setup)["run"]["items"][0]
  assert result["state"] == "needs_you"
  assert result["merge_attempted"] is False
  from app import models
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.merge_work_key(ITEM)).one()
  assert claim.released_at is not None


@pytest.mark.parametrize("failure", [
  HTTPException(409, "The target branch changed."),
  OSError("Final provider read failed"),
])
def test_base_drift_after_reservation_never_dispatches_merge(setup, monkeypatch, failure):
  db, row, _ = setup
  reads = 0

  def moving_base(*args):
    nonlocal reads
    reads += 1
    if reads == 2:
      raise failure

  monkeypatch.setattr(domain, "assert_current_base", moving_base)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("drift must not dispatch"))
  result = report(setup)["run"]["items"][0]
  assert reads == 2
  assert result["state"] == "needs_you" and result["merge_attempted"] is False
  from app import models
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.merge_work_key(ITEM)).one()
  assert claim.released_at is not None


def test_stop_after_dispatch_cannot_erase_confirmed_merge(setup, monkeypatch):
  db, row, _ = setup

  def merge_then_stop(*args):
    from app import models
    from app.database import SessionLocal
    other = SessionLocal()
    try:
      live = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, live, routes.REVIEW_STOP_KEY, {"state": "requested"})
    finally:
      other.close()
    return {"merged": True, "sha": "confirmed-merge-sha"}

  monkeypatch.setattr(domain, "perform_merge", merge_then_stop)
  result = report(setup)["run"]["items"][0]
  assert result["state"] == "merged"
  assert result["merge_sha"] == "confirmed-merge-sha"
  db.refresh(row)
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"


def test_definite_refusal_racing_stop_disarms_exact_reservation(setup, monkeypatch):
  db, row, _ = setup
  reads = 0

  def drift_while_stop_lands(*args):
    nonlocal reads
    reads += 1
    if reads == 2:
      from app.database import SessionLocal
      other = SessionLocal()
      try:
        live = other.get(models.ContributionReviewRun, row.id)
        domain.save_outcome(other, live, routes.REVIEW_STOP_KEY, {"state": "requested"})
      finally:
        other.close()
      raise HTTPException(409, "The target branch changed.")

  monkeypatch.setattr(domain, "assert_current_base", drift_while_stop_lands)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("must not dispatch"))
  try:
    report(setup)
  except HTTPException:
    pass
  db.refresh(row)
  item = row.outcomes_json[domain.key(ITEM)]
  assert item.get("merge_attempted") is False and item.get("state") != "merging"
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"


@pytest.mark.parametrize("message,status,expected", [
  ("Pull Request is not mergeable", 405, "not_dispatched"),
  ("gh: HTTP 422: rejected", 409, "not_dispatched"),
  ("gh: HTTP 408: timeout", 409, "unknown"),
  ("gh: HTTP 429: rate limited", 409, "unknown"),
  ("gh: HTTP 503: server error", 409, "unknown"),
  ("gh: Pull Request is not mergeable (HTTP 405)", 409, "not_dispatched"),
  ("gh: rejected user body says HTTP 405: but response was not parsed", 409, "unknown"),
  ("Untrusted quote: (HTTP 405)", 409, "unknown"),
  ("transport failed", 409, "unknown"),
])
def test_dispatch_classifier_requires_proved_rejection(message, status, expected):
  assert classify_dispatch_error(ContributionSubmitError(message, status)).state == expected


def test_definite_github_merge_rejection_is_not_unknown(setup, monkeypatch):
  monkeypatch.setattr(domain, "perform_merge", lambda *a: (_ for _ in ()).throw(
    ContributionSubmitError("Pull Request is not mergeable", 405)))
  item = report(setup)["run"]["items"][0]
  assert item["state"] == "needs_you" and item["merge_attempted"] is False


def test_real_gh_graphql_nullable_mutation_refusal_is_definite(tmp_path, monkeypatch):
  from app import github_contribution_git as transport
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  fake = fake_bin / "gh"
  fake.write_text("#!/bin/sh\necho '{\"errors\":[{\"type\":\"UNPROCESSABLE\",\"path\":[\"enqueuePullRequest\"],\"message\":\"not queueable\"}],\"data\":{\"enqueuePullRequest\":null}}'\nexit 1\n")
  fake.chmod(0o755)
  monkeypatch.setattr(transport, "_git_env", lambda _cwd: {"PATH": str(fake_bin) + ":/usr/bin:/bin"})
  with pytest.raises(NotDispatched):
    domain.enqueue(transport._gh, tmp_path, TARGET)


@pytest.mark.parametrize("payload", [
  {"errors": "garbled", "data": None},
  {"errors": [], "data": None},
  {"errors": [{}], "data": None},
  {"errors": [{"message": ""}], "data": None},
  {"errors": [{"message": "rejected"}], "data": []},
])
def test_malformed_graphql_response_never_certifies_no_effect(payload):
  from types import SimpleNamespace
  import json
  response = SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=1)
  with pytest.raises(ContributionSubmitError):
    graphql_mutation_result(response)


def test_valid_graphql_no_data_error_certifies_refusal():
  from types import SimpleNamespace
  response = SimpleNamespace(stdout='{"errors":[{"type":"UNPROCESSABLE","message":"rejected"}],"data":null}', stderr="", returncode=1)
  with pytest.raises(NotDispatched):
    graphql_mutation_result(response)


@pytest.mark.parametrize("data,paths,expected", [
  ({"enqueuePullRequest": None}, [["enqueuePullRequest"]], "not_dispatched"),
  ({"enqueuePullRequest": None}, [["otherField"]], "unknown"),
  ({"enqueuePullRequest": None}, [None], "unknown"),
  ({"enqueuePullRequest": {"mergeQueueEntry": {"id": "q"}}}, [["enqueuePullRequest"]], "unknown"),
  ({"enqueuePullRequest": None, "otherField": {"ok": True}}, [["enqueuePullRequest"]], "unknown"),
  ({"enqueuePullRequest": None, "otherField": None}, [["enqueuePullRequest"], None], "unknown"),
])
def test_nullable_mutation_field_requires_matching_typed_root_error(data, paths, expected):
  from types import SimpleNamespace
  import json
  errors = [{"type": "UNPROCESSABLE", "message": "rejected", **({"path": path} if path else {})}
            for path in paths]
  response = SimpleNamespace(stdout=json.dumps({"data": data, "errors": errors}), stderr="", returncode=1)
  try:
    graphql_mutation_result(response)
  except Exception as exc:
    assert classify_dispatch_error(exc).state == expected
  else:
    pytest.fail("An error response must not be treated as success")


@pytest.mark.parametrize("kind,stderr,expected", [
  ("UNPROCESSABLE", "gh: HTTP 422: refused", "not_dispatched"),
  ("NOT_FOUND", "", "not_dispatched"),
  ("FORBIDDEN", "gh: HTTP 503: unavailable", "unknown"),
  ("TIMEOUT", "gh: HTTP 422: timed out", "unknown"),
  ("UNPROCESSABLE", "gh: HTTP 429: retry later", "unknown"),
])
def test_nullable_mutation_field_requires_refusal_type_and_safe_status(kind, stderr, expected):
  from types import SimpleNamespace
  import json
  payload = {"data": {"enqueuePullRequest": None},
             "errors": [{"type": kind, "path": ["enqueuePullRequest"], "message": "not applied"}]}
  response = SimpleNamespace(stdout=json.dumps(payload), stderr=stderr, returncode=1)
  with pytest.raises((NotDispatched, ContributionSubmitError)) as exc:
    graphql_mutation_result(response)
  assert classify_dispatch_error(exc.value).state == expected


@pytest.mark.parametrize("types,stderr,expected", [
  (["FORBIDDEN"], "", "not_dispatched"),
  (["NOT_FOUND"], "gh: HTTP 404: missing", "not_dispatched"),
  (["UNPROCESSABLE"], "gh: request rejected (HTTP 422)", "not_dispatched"),
  (["FORBIDDEN"], "gh: HTTP 200: GraphQL error", "not_dispatched"),
  (["FORBIDDEN"], "gh: HTTP 302: redirected", "unknown"),
  ([None], "", "unknown"),
  (["TIMEOUT"], "", "unknown"),
  (["INTERNAL"], "gh: HTTP 503: upstream failed", "unknown"),
  (["FORBIDDEN"], "gh: HTTP 503: upstream failed", "unknown"),
  (["FORBIDDEN"], "gh: HTTP 408: timed out", "unknown"),
  (["FORBIDDEN"], "gh: HTTP 429: rate limited", "unknown"),
  (["FORBIDDEN", "INTERNAL"], "", "unknown"),
])
def test_graphql_no_data_requires_known_refusal_and_safe_status(types, stderr, expected):
  from types import SimpleNamespace
  import json
  errors = [{"message": "GitHub message", **({"type": kind} if kind else {})} for kind in types]
  response = SimpleNamespace(stdout=json.dumps({"errors": errors, "data": None}), stderr=stderr, returncode=1)
  try:
    graphql_mutation_result(response)
  except Exception as exc:
    assert classify_dispatch_error(exc).state == expected
  else:
    pytest.fail("An error response must not be treated as success")


def test_real_gh_graphql_5xx_no_data_remains_unknown(tmp_path, monkeypatch):
  from app import github_contribution_git as transport
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  fake = fake_bin / "gh"
  fake.write_text("#!/bin/sh\necho '{\"errors\":[{\"type\":\"INTERNAL\",\"message\":\"may be timeout\"}],\"data\":null}'\necho 'gh: HTTP 503: unavailable' >&2\nexit 1\n")
  fake.chmod(0o755)
  monkeypatch.setattr(transport, "_git_env", lambda _cwd: {"PATH": str(fake_bin) + ":/usr/bin:/bin"})
  with pytest.raises(ContributionSubmitError) as uncertain:
    domain.enqueue(transport._gh, tmp_path, TARGET)
  assert classify_dispatch_error(uncertain.value).state == "unknown"


@pytest.mark.parametrize("nonce_nullable", [False, True])
def test_core_migration_upgrades_legacy_strict_table_shape(tmp_path, nonce_nullable):
  eng = create_engine(f"sqlite:///{tmp_path / 'old-review.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE schema_migrations (version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP)"))
    nullable = "" if nonce_nullable else " NOT NULL"
    conn.execute(text("CREATE TABLE contribution_review_runs (id VARCHAR(64) PRIMARY KEY, app_id INTEGER NOT NULL, app_nonce VARCHAR(64)" + nullable + ", request_id VARCHAR(64), owner_id INTEGER)"))
    conn.execute(text("INSERT INTO contribution_review_runs VALUES ('old-run', 2, 'old-nonce', 'old-request', 1)"))
  schema_migrations._allow_core_contribution_review_runs(eng)
  schema_migrations._allow_core_contribution_review_runs(eng)
  cols = {col["name"]: col for col in inspect(eng).get_columns("contribution_review_runs")}
  assert cols["app_id"]["nullable"] and cols["app_nonce"]["nullable"]
  assert "options_json" in cols
  with eng.connect() as conn:
    assert conn.execute(text("SELECT id, app_id, app_nonce FROM contribution_review_runs")).one() == ("old-run", 2, "old-nonce")
    assert conn.execute(text("SELECT count(*) FROM schema_migrations")).scalar_one() == 0


def test_canonical_cli_freezes_takeover_scope_in_request_identity(monkeypatch):
  monkeypatch.setattr(cli, "inspect_pr", lambda ref: {**ITEM, "title": "Safe change", "url": "https://github.com/example/project/pull/7"})
  snapshot = {"review_prompt": "Review", "fix_prompt": "Fix", "merge_prompt": "Merge",
    "max_rounds": None, "autopilot": False, "provider": "codex", "model": "test-model",
    "reasoning_effort": "high"}
  monkeypatch.setattr(cli, "api", lambda path, **kw: {"options": snapshot, "preview_sha256": routes._fingerprint(snapshot)})
  frozen = cli.freeze_preview(cli.prepare_selection(["example/project#7"], "review_fix_merge", "chat-1"))
  verify_selection(frozen)
  assert start_body(frozen)["confirmation_scope"] == "named_pr_repairs_and_reviewed_successors"
  with pytest.raises(ValueError):
    verify_selection({**frozen, "confirmation_scope": "broader-scope"})


def test_core_migration_preserves_exact_post_0079_nullable_shape(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'post-0079.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE schema_migrations (version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP)"))
    conn.execute(text("INSERT INTO schema_migrations VALUES ('0079_core_contribution_review_runs', CURRENT_TIMESTAMP)"))
    conn.execute(text("CREATE TABLE contribution_review_runs (id VARCHAR(64) PRIMARY KEY, app_id INTEGER, app_nonce VARCHAR(64), owner_id INTEGER NOT NULL, request_id VARCHAR(64) NOT NULL, options_json JSON)"))
    conn.execute(text("CREATE UNIQUE INDEX uq_contribution_review_runs_core_request ON contribution_review_runs (owner_id, request_id) WHERE app_id IS NULL"))
    conn.execute(text("INSERT INTO contribution_review_runs VALUES ('core-run', NULL, NULL, 1, 'same-request', '{}')"))
    before = conn.execute(text("SELECT type, name, sql FROM sqlite_master WHERE tbl_name='contribution_review_runs' ORDER BY type, name")).all()
  schema_migrations._allow_core_contribution_review_runs(eng)
  schema_migrations._allow_core_contribution_review_runs(eng)
  with eng.begin() as conn:
    assert conn.execute(text("SELECT type, name, sql FROM sqlite_master WHERE tbl_name='contribution_review_runs' ORDER BY type, name")).all() == before
    assert conn.execute(text("SELECT id, options_json FROM contribution_review_runs")).one() == ("core-run", "{}")
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
      conn.execute(text("INSERT INTO contribution_review_runs VALUES ('duplicate', NULL, NULL, 1, 'same-request', '{}')"))


def test_canonical_cli_allow_mark_ready_is_exact_frozen_scope(monkeypatch):
  monkeypatch.setattr(cli, "inspect_pr", lambda ref: {**ITEM, "title": "Safe change",
    "url": "https://github.com/example/project/pull/7"})
  snapshot = {"review_prompt": "Review", "fix_prompt": "Fix", "merge_prompt": "Merge",
    "max_rounds": None, "autopilot": False, "provider": "codex", "model": "test-model",
    "reasoning_effort": "high"}
  monkeypatch.setattr(cli, "api", lambda path, **kw: {"options": snapshot,
    "preview_sha256": routes._fingerprint(snapshot)})
  selection = cli.prepare_selection(["example/project#7"], "review_fix_merge", "chat-1")
  ordinary = cli.freeze_preview(selection)
  ready = cli.freeze_preview(selection, allow_mark_ready=True)
  assert start_body(ready)["confirmation_scope"] == "named_pr_repairs_ready_and_reviewed_successors"
  assert ready["request_id"] != ordinary["request_id"]
  with pytest.raises(ValueError):
    cli.freeze_preview(cli.prepare_selection(["example/project#7"], "review_merge", "chat-1"),
      allow_mark_ready=True)


def test_cli_preview_flag_reaches_frozen_ready_scope(monkeypatch, tmp_path, capsys):
  seen = []
  monkeypatch.setenv("CHAT_ID", "owning-chat")
  monkeypatch.setattr(cli, "prepare_selection", lambda *a: {"mode": "review_fix_merge"})
  def freeze(selection, options=None, agent=None, *, allow_mark_ready=False):
    seen.append(allow_mark_ready)
    return {"confirmation_scope": "named_pr_repairs_ready_and_reviewed_successors"}
  monkeypatch.setattr(cli, "freeze_preview", freeze)
  monkeypatch.setattr(cli, "save_preview", lambda path, selection: None)
  cli.main(["preview", "--mode", "review_fix_merge", "--allow-mark-ready",
    "--output", str(tmp_path / "frozen.json"), "example/project#7"])
  assert seen == [True]
  assert "named_pr_repairs_ready_and_reviewed_successors" in capsys.readouterr().out
