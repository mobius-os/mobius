import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import models, reviewer_automation
from app.config import get_settings


GUIDE = hashlib.sha256(b"guide").hexdigest()


@pytest.fixture(autouse=True)
def _app_owned_reviewer_policy(monkeypatch):
  """Route tests exercise the trust adapter; app tests own policy details."""
  async def validate(_db, _app_id, body, _head_sha):
    return str(body).strip()

  async def manual(_app_id, body, _nonce, _db):
    return body.model_dump()

  async def grant(_db, _app_id, body):
    return body.model_dump()

  monkeypatch.setattr("app.routes.reviewer._reviewer_validate_comment", validate)
  monkeypatch.setattr("app.routes.reviewer._reviewer_manual_plan", manual)
  monkeypatch.setattr("app.routes.reviewer._reviewer_prepare_grant", grant)


def test_public_authority_changes_require_owner_scope():
  from app.routes import reviewer as reviewer_routes

  reviewer_routes._require_reviewer_owner_action(SimpleNamespace(
    scope="owner", app_id=None, delegation_id=None,
  ))

  with pytest.raises(HTTPException) as app_denied:
    reviewer_routes._require_reviewer_owner_action(SimpleNamespace(
      scope="app", app_id=12, delegation_id=None,
    ))
  assert app_denied.value.status_code == 403

  with pytest.raises(HTTPException) as child_denied:
    reviewer_routes._require_reviewer_owner_action(SimpleNamespace(
      scope="owner", app_id=None, delegation_id="child-1",
    ))
  assert child_denied.value.status_code == 403


def _app(db, source_dir="/data/apps/pr-review"):
  row = models.App(
    name="Reviewer", description="test", source_dir=str(source_dir),
    slug="pr-review", github_access=True, github_connect=True,
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


def _json_digest(value):
  raw = json.dumps(
    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  ).encode()
  return hashlib.sha256(raw).hexdigest()


def _manual_case(db, tmp_path):
  source = tmp_path / "reviewer-source"
  source.mkdir()
  guide = "# Reviewer QA guide\n\nBe concrete."
  (source / "reviewing.md").write_text(guide)
  app = _app(db, source)
  storage = Path(get_settings().data_dir) / "apps" / str(app.id)
  (storage / "job-state").mkdir(parents=True)
  settings = {
    "selectedRepos": ["mobius-os/app-memory"],
    "customGuidance": "Prefer the smallest durable correction.",
    "repoGuidance": {"mobius-os/app-memory": "Protect saved owner settings."},
  }
  (storage / "settings.json").write_text(json.dumps(settings))
  effective = (
    guide + "\n\n---\n\n# Workspace guidance\n\n"
    + settings["customGuidance"]
    + "\n\n---\n\n# Repository guidance\n\n"
    + settings["repoGuidance"]["mobius-os/app-memory"]
  )
  guide_hash = hashlib.sha256(effective.encode()).hexdigest()
  head_sha = "a" * 40
  base_sha = "b" * 40
  bundle_hash = "c" * 64
  identity = _json_digest({
    "repository": "mobius-os/app-memory", "number": 54,
    "head_sha": head_sha, "base_sha": base_sha,
    "guide_hash": guide_hash, "bundle_hash": bundle_hash,
  })
  comment = (
    "### Reviewer: QA second look\n\nOne concrete issue.\n\n"
    "_Reviewed revision `aaaaaaaaaaaa`._"
  )
  record = {
    "identity": identity, "repository": "mobius-os/app-memory", "number": 54,
    "head_sha": head_sha, "base_sha": base_sha, "guide_hash": guide_hash,
    "bundle_hash": bundle_hash, "status": "complete", "private": True,
    "draft_comment": comment,
  }
  ledger_path = storage / "job-state" / "ledger.json"
  ledger_path.write_text(json.dumps({
    "schema": 1, "pulls": {"mobius-os/app-memory#54": record}, "events": [],
  }))
  return app, {
    "identity": identity, "repository": record["repository"], "pr_number": 54,
    "head_sha": head_sha, "base_sha": base_sha,
    "guide_hash": guide_hash, "body": comment,
  }, source, storage, ledger_path


def _claim(db, app_id, identity="1" * 64, number=7, repo="mobius-os/mobius"):
  return reviewer_automation.claim_post(
    db, app_id, identity=identity, repository=repo, pr_number=number,
    head_sha="a" * 40, base_sha="b" * 40, guide_hash=GUIDE,
  )


def test_grant_is_exact_visible_scope(db):
  app = _app(db)
  row = reviewer_automation.stamp_grant(
    db, app.id,
    repositories=["mobius-os/mobius", "mobius-os/app-memory", "mobius-os/mobius"],
    guide_hash=GUIDE, max_rounds_per_pr=3, daily_post_ceiling=9,
  )
  public = reviewer_automation.public_grant(row)
  assert public["enabled"] is True
  assert public["repositories"] == ["mobius-os/app-memory", "mobius-os/mobius"]
  assert public["guide_hash"] == GUIDE
  assert public["max_rounds_per_pr"] == 3
  assert public["daily_post_ceiling"] == 9


def test_claim_is_exactly_once_and_fail_closed(db):
  app = _app(db)
  reviewer_automation.stamp_grant(
    db, app.id, repositories=["mobius-os/mobius"], guide_hash=GUIDE,
    max_rounds_per_pr=3, daily_post_ceiling=9,
  )
  claim = _claim(db, app.id)
  assert claim.status == "posting"
  with pytest.raises(HTTPException) as duplicate:
    _claim(db, app.id)
  assert duplicate.value.status_code == 409
  reviewer_automation.fail_post(db, claim, "ambiguous")
  db.refresh(claim)
  assert claim.status == "uncertain"
  with pytest.raises(HTTPException):
    _claim(db, app.id)


def test_scope_guide_daily_and_round_ceilings_are_enforced(db):
  app = _app(db)
  reviewer_automation.stamp_grant(
    db, app.id, repositories=["mobius-os/mobius"], guide_hash=GUIDE,
    max_rounds_per_pr=1, daily_post_ceiling=2,
  )
  with pytest.raises(HTTPException) as scope:
    _claim(db, app.id, repo="someone/else")
  assert scope.value.status_code == 403
  with pytest.raises(HTTPException) as guide:
    reviewer_automation.claim_post(
      db, app.id, identity="2" * 64, repository="mobius-os/mobius",
      pr_number=8, head_sha="a" * 40, base_sha="b" * 40,
      guide_hash="3" * 64,
    )
  assert guide.value.status_code == 409
  _claim(db, app.id, identity="4" * 64, number=7)
  with pytest.raises(HTTPException) as rounds:
    _claim(db, app.id, identity="5" * 64, number=7)
  assert rounds.value.status_code == 409
  _claim(db, app.id, identity="6" * 64, number=8)
  with pytest.raises(HTTPException) as daily:
    _claim(db, app.id, identity="7" * 64, number=9)
  assert daily.value.status_code == 429


def test_pause_revokes_next_claim(db):
  app = _app(db)
  reviewer_automation.stamp_grant(
    db, app.id, repositories=["mobius-os/mobius"], guide_hash=GUIDE,
    max_rounds_per_pr=5, daily_post_ceiling=5,
  )
  reviewer_automation.set_enabled(db, app.id, False)
  with pytest.raises(HTTPException) as paused:
    _claim(db, app.id)
  assert paused.value.status_code == 403


def test_guarded_comment_route_posts_comment_only_once(
  client, auth, db, monkeypatch,
):
  app = _app(db)
  grant = client.post(
    f"/api/github/reviewer/{app.id}/grant", headers=auth, json={
      "repositories": ["mobius-os/mobius"], "guide_hash": GUIDE,
      "max_rounds_per_pr": 2, "daily_post_ceiling": 4,
    },
  )
  assert grant.status_code == 200, grant.text
  live_checks = []
  monkeypatch.setattr(
    "app.routes.reviewer._reviewer_assert_live_revision",
    lambda repo, number, head, base: live_checks.append((repo, number, head, base)) or {},
  )
  calls = []
  monkeypatch.setattr(
    "app.routes.reviewer._gh",
    lambda *args: calls.append(args) or SimpleNamespace(
      stdout='{"html_url":"https://github.test/review/1"}',
    ),
  )
  payload = {
    "identity": "9" * 64, "repository": "mobius-os/mobius",
    "pr_number": 77, "head_sha": "a" * 40, "base_sha": "b" * 40,
    "guide_hash": GUIDE,
    "body": (
      "### Reviewer: QA second look\n\nOne concrete issue.\n\n"
      "_Reviewed revision `aaaaaaaaaaaa`._"
    ),
  }
  posted = client.post(
    f"/api/github/reviewer/{app.id}/comment", headers=auth, json=payload,
  )
  assert posted.status_code == 200, posted.text
  assert posted.json()["url"] == "https://github.test/review/1"
  assert len(live_checks) == 2
  assert len(calls) == 1
  command = calls[0]
  assert "event=COMMENT" in command
  assert all("APPROVE" not in str(value) for value in command)
  duplicate = client.post(
    f"/api/github/reviewer/{app.id}/comment", headers=auth, json=payload,
  )
  assert duplicate.status_code == 409
  assert len(calls) == 1


def test_second_freshness_failure_closes_identity_without_charging_slot(
  client, auth, db, monkeypatch,
):
  app = _app(db)
  client.post(
    f"/api/github/reviewer/{app.id}/grant", headers=auth, json={
      "repositories": ["mobius-os/mobius"], "guide_hash": GUIDE,
      "max_rounds_per_pr": 1, "daily_post_ceiling": 1,
    },
  )
  checks = []
  def freshness(*_args):
    checks.append(True)
    if len(checks) == 2:
      raise HTTPException(409, "revision moved")
    return {}
  monkeypatch.setattr("app.routes.reviewer._reviewer_assert_live_revision", freshness)
  calls = []
  monkeypatch.setattr("app.routes.reviewer._gh", lambda *args: calls.append(args))
  payload = {
    "identity": "7" * 64, "repository": "mobius-os/mobius",
    "pr_number": 77, "head_sha": "a" * 40, "base_sha": "b" * 40,
    "guide_hash": GUIDE,
    "body": (
      "### Reviewer: all clear\n\nClean.\n\n"
      "_Reviewed revision `aaaaaaaaaaaa`._"
    ),
  }
  response = client.post(
    f"/api/github/reviewer/{app.id}/comment", headers=auth, json=payload,
  )
  assert response.status_code == 409
  assert calls == []
  claim = db.query(models.ReviewerAutomationPost).filter_by(
    app_id=app.id, identity="7" * 64,
  ).one()
  grant = db.query(models.ReviewerAutomationGrant).filter_by(app_id=app.id).one()
  assert claim.status == "superseded"
  assert grant.daily_posts_used == 0
  assert grant.rounds_json == {}


def test_manual_comment_posts_once_without_grant_and_exposes_audit(
  client, auth, db, monkeypatch, tmp_path,
):
  app, payload, _source, _storage, _ledger = _manual_case(db, tmp_path)
  live_checks = []
  monkeypatch.setattr(
    "app.routes.reviewer._reviewer_assert_live_revision",
    lambda *args: live_checks.append(args) or {},
  )
  calls = []
  monkeypatch.setattr(
    "app.routes.reviewer._gh",
    lambda *args: calls.append(args) or SimpleNamespace(
      stdout='{"html_url":"https://github.test/review/manual"}',
    ),
  )

  response = client.post(
    f"/api/github/reviewer/{app.id}/comment/manual", headers=auth, json=payload,
  )
  assert response.status_code == 200, response.text
  assert response.json()["url"] == "https://github.test/review/manual"
  assert len(live_checks) == 2
  assert len(calls) == 1
  assert "event=COMMENT" in calls[0]
  assert db.query(models.ReviewerAutomationGrant).filter_by(app_id=app.id).count() == 0

  # A repeated browser click reconciles the confirmed result; it never writes
  # the same public comment again.
  duplicate = client.post(
    f"/api/github/reviewer/{app.id}/comment/manual", headers=auth, json=payload,
  )
  assert duplicate.status_code == 200
  assert duplicate.json()["url"] == "https://github.test/review/manual"
  assert len(calls) == 1

  audit = client.get(f"/api/github/reviewer/{app.id}/comments", headers=auth)
  assert audit.status_code == 200
  assert audit.json()["comments"] == [{
    "identity": payload["identity"],
    "repository": "mobius-os/app-memory", "pr_number": 54,
    "head_sha": "a" * 40, "base_sha": "b" * 40,
    "guide_hash": payload["guide_hash"], "status": "posted",
    "url": "https://github.test/review/manual", "error": None,
    "claimed_at": audit.json()["comments"][0]["claimed_at"],
    "posted_at": audit.json()["comments"][0]["posted_at"],
  }]


def test_manual_comment_stops_when_app_policy_rejects_the_draft(
  client, auth, db, monkeypatch, tmp_path,
):
  app, payload, _source, _storage, _ledger = _manual_case(db, tmp_path)
  async def reject(*_args, **_kwargs):
    raise HTTPException(409, "The draft or guidance changed; refresh the review before sending.")
  monkeypatch.setattr("app.routes.reviewer._reviewer_manual_plan", reject)
  calls = []
  monkeypatch.setattr("app.routes.reviewer._gh", lambda *args: calls.append(args))

  response = client.post(
    f"/api/github/reviewer/{app.id}/comment/manual", headers=auth, json=payload,
  )
  assert response.status_code == 409
  assert calls == []
  assert db.query(models.ReviewerAutomationPost).count() == 0


def test_manual_second_local_check_supersedes_without_refunding_grant(
  client, auth, db, monkeypatch, tmp_path,
):
  app, payload, _source, _storage, _ledger = _manual_case(db, tmp_path)
  grant = reviewer_automation.stamp_grant(
    db, app.id, repositories=["mobius-os/app-memory"], guide_hash=GUIDE,
    max_rounds_per_pr=5, daily_post_ceiling=9,
  )
  grant.daily_posts_used = 3
  grant.rounds_json = {"mobius-os/app-memory#54": 2}
  db.commit()
  from app.routes import reviewer as reviewer_routes
  original = reviewer_routes._reviewer_manual_plan
  checks = []

  async def changed_after_claim(*args, **kwargs):
    checks.append(True)
    if len(checks) == 2:
      raise HTTPException(409, "stored draft moved")
    return await original(*args, **kwargs)

  monkeypatch.setattr(reviewer_routes, "_reviewer_manual_plan", changed_after_claim)
  monkeypatch.setattr(
    reviewer_routes, "_reviewer_assert_live_revision", lambda *_args: {},
  )
  calls = []
  monkeypatch.setattr(reviewer_routes, "_gh", lambda *args: calls.append(args))

  response = client.post(
    f"/api/github/reviewer/{app.id}/comment/manual", headers=auth, json=payload,
  )
  assert response.status_code == 409
  assert calls == []
  claim = db.query(models.ReviewerAutomationPost).filter_by(
    app_id=app.id, identity=payload["identity"],
  ).one()
  db.refresh(grant)
  assert claim.status == "superseded"
  assert grant.daily_posts_used == 3
  assert grant.rounds_json == {"mobius-os/app-memory#54": 2}


def test_manual_ambiguous_github_result_is_not_retried(
  client, auth, db, monkeypatch, tmp_path,
):
  app, payload, _source, _storage, _ledger = _manual_case(db, tmp_path)
  monkeypatch.setattr(
    "app.routes.reviewer._reviewer_assert_live_revision", lambda *_args: {},
  )
  calls = []

  def fail(*args):
    calls.append(args)
    raise RuntimeError("ambiguous")

  monkeypatch.setattr("app.routes.reviewer._gh", fail)
  response = client.post(
    f"/api/github/reviewer/{app.id}/comment/manual", headers=auth, json=payload,
  )
  assert response.status_code == 502
  retry = client.post(
    f"/api/github/reviewer/{app.id}/comment/manual", headers=auth, json=payload,
  )
  assert retry.status_code == 409
  assert len(calls) == 1
  claim = db.query(models.ReviewerAutomationPost).filter_by(
    app_id=app.id, identity=payload["identity"],
  ).one()
  assert claim.status == "uncertain"


@pytest.mark.parametrize("repository,number", [
  ("not a repo", 1),
  ("mobius-os/mobius", 0),
  ("a/b/c", 3),
])
def test_reviewer_live_pr_rejects_invalid_target_before_any_github_call(
  monkeypatch, repository, number,
):
  """The validation branch must run (and reject) before any GitHub call —
  it is the code path CI lint once caught as an undefined name."""
  from app.routes import reviewer

  monkeypatch.setattr(
    reviewer, "_gh", lambda *_: pytest.fail("invalid target reached GitHub"),
  )
  with pytest.raises(HTTPException) as error:
    reviewer._reviewer_live_pr(repository, number)
  assert error.value.status_code == 422


@pytest.mark.parametrize("state,head,base,expected", [
  ("open", "a" * 40, "b" * 40, None),
  ("closed", "a" * 40, "b" * 40, 409),
  ("open", "c" * 40, "b" * 40, 409),
  ("open", "a" * 40, "c" * 40, 409),
])
def test_live_revision_revalidates_actual_github_response(
  monkeypatch, state, head, base, expected,
):
  from app.routes import reviewer

  calls = []
  pull = {"state": state, "head": {"sha": head}, "base": {"sha": base}}

  def gh(*args):
    calls.append(args[1:])
    return SimpleNamespace(stdout=json.dumps(pull))

  monkeypatch.setattr(reviewer, "_gh", gh)
  if expected:
    with pytest.raises(HTTPException) as error:
      reviewer._reviewer_assert_live_revision(
        "owner/repo", 17, "a" * 40, "b" * 40,
      )
    assert error.value.status_code == expected
  else:
    assert reviewer._reviewer_assert_live_revision(
      "owner/repo", 17, "a" * 40, "b" * 40,
    ) == pull
  assert calls == [("api", "repos/owner/repo/pulls/17")]


@pytest.mark.parametrize("response", [
  "not json",
  "[]",
  '{"state":"open","head":"not-an-object","base":{"sha":"bbbb"}}',
  '{"state":"open","head":{"sha":"aaaa"},"base":"not-an-object"}',
])
def test_live_revision_rejects_invalid_github_response(monkeypatch, response):
  from app.routes import reviewer

  monkeypatch.setattr(
    reviewer, "_gh", lambda *_: SimpleNamespace(stdout=response),
  )
  with pytest.raises(HTTPException) as error:
    reviewer._reviewer_assert_live_revision(
      "owner/repo", 17, "a" * 40, "b" * 40,
    )
  assert error.value.status_code == 502


def test_live_revision_handles_github_read_failure(monkeypatch):
  from app.routes import reviewer

  def unavailable(*_):
    raise RuntimeError("read unavailable")

  monkeypatch.setattr(reviewer, "_gh", unavailable)
  with pytest.raises(HTTPException) as error:
    reviewer._reviewer_live_pr("owner/repo", 17)
  assert error.value.status_code == 502
