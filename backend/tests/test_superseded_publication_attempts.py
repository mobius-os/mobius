"""A newer review supersedes a pre-PR attempt that GitHub shows left nothing.

A signed attempt receipt lets the exact claimed action replay and reconcile.
Once the record is restaged or re-reviewed that action can never replay, so
its receipt must not refuse every later Send. A push's only public effect is
the branch tip, which GitHub reports directly.
"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import contribution_runtime
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.routes import github as github_routes
from test_app_fixtures import create_local_app

APP_ID = 41
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
OLD_TIP = "c" * 40
ACTION = {"action": "submit", "publication_stage": "draft", "autopilot": False}


def write_record(record_id, head=NEW_HEAD, status="prepared", app_id=APP_ID, **extra):
  record = {**extra,
    "id": record_id, "type": "pr", "status": status, "repo": "mobius-os/app-demo",
    "branch": "fix/demo", "title": "Polish demo",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo", "branch": "fix/demo",
             "head_sha": head, "title": "Polish demo", "body_draft": "Polishes."},
  }
  path = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  path.mkdir(parents=True, exist_ok=True)
  (path / f"{record_id}.json").write_text(json.dumps(record))
  return record


def sign(record_id, record, phase, *, claimed_head=OLD_HEAD, expected_remote=None,
         app_id=APP_ID):
  claim = {**record, "status": "submitting", "submitter": "contribute-button",
           "plan": {**record["plan"], "head_sha": claimed_head}}
  envelope = github_routes._signed_personal_attempt({
    "version": 1, "app_id": app_id, "record_id": record_id,
    "claim_sha256": github_routes._personal_publication_input_sha256(claim),
    "claim_input": github_routes._personal_publication_input(claim),
    "preclaim_input": None, "action_input": ACTION, "phase": phase,
    "effective_request": {
      "action": "push", "repo": "mobius-os/app-demo",
      "head_repository": "octocat/app-demo", "branch": "fix/demo",
      "head_sha": claimed_head, "base_branch": "main",
      "expected_remote_sha": expected_remote,
    },
    "record_patch": {}, "updated_at": "2026-10-12T00:00:00Z",
  })
  path = contribution_runtime.personal_attempt_path(app_id, record_id, create_parent=True)
  path.write_text(json.dumps(envelope))
  return path


def remote_tip(monkeypatch, answer):
  reads = []
  def read(_cwd, head_repository, branch):
    reads.append((head_repository, branch))
    if isinstance(answer, Exception):
      raise answer
    return answer
  monkeypatch.setattr(github_routes, "_authoritative_upstream_branch_sha", read)
  return reads


def retire(record_id):
  asyncio.run(github_routes._retire_superseded_attempt(APP_ID, record_id))


@pytest.mark.parametrize(("expected_remote", "tip"), [
  (None, None),          # a new branch GitHub still does not have
  (OLD_TIP, OLD_TIP),    # an existing branch the push never moved
  (None, OLD_HEAD),      # the old push landed exactly; the new Send leases on it
])
def test_a_restaged_record_retires_a_push_github_resolves(monkeypatch, expected_remote, tip):
  record = write_record("resolved")
  receipt = sign("resolved", record, "push_ambiguous", expected_remote=expected_remote)
  reads = remote_tip(monkeypatch, tip)
  retire("resolved")
  assert reads == [("octocat/app-demo", "fix/demo")]
  assert not receipt.exists()


@pytest.mark.parametrize("tip", [
  "d" * 40, ContributionSubmitError("GitHub is unreachable."), OSError("timeout"),
])
def test_a_moved_or_unreadable_branch_keeps_the_receipt(monkeypatch, tip):
  record = write_record("unresolved")
  receipt = sign("unresolved", record, "push_pending", expected_remote=OLD_TIP)
  remote_tip(monkeypatch, tip)
  retire("unresolved")
  assert receipt.exists()


def test_the_exact_claimed_proposal_keeps_its_replay_receipt(monkeypatch):
  # A failed Send returns the claim to prepared and keeps its submitter.
  record = write_record("replayable", head=OLD_HEAD, submitter="contribute-button")
  receipt = sign("replayable", record, "push_ambiguous")
  reads = remote_tip(monkeypatch, None)
  retire("replayable")
  assert reads == [] and receipt.exists()


@pytest.mark.parametrize("phase", ["branch_published", "pr_ambiguous", "complete"])
def test_a_phase_where_a_pr_may_exist_keeps_the_receipt(monkeypatch, phase):
  record = write_record("maybe-public")
  receipt = sign("maybe-public", record, phase)
  reads = remote_tip(monkeypatch, None)
  retire("maybe-public")
  assert reads == [] and receipt.exists()


def test_an_in_flight_claim_keeps_the_receipt(monkeypatch):
  record = write_record("in-flight", status="submitting")
  receipt = sign("in-flight", record, "push_pending")
  reads = remote_tip(monkeypatch, None)
  retire("in-flight")
  assert reads == [] and receipt.exists()


def test_a_superseded_attempt_that_never_pushed_needs_no_github_read(monkeypatch):
  record = write_record("never-pushed")
  receipt = sign("never-pushed", record, "armed")
  reads = remote_tip(monkeypatch, AssertionError("no read needed"))
  retire("never-pushed")
  assert reads == [] and not receipt.exists()


def test_a_record_that_changes_during_the_read_keeps_the_receipt(monkeypatch):
  record = write_record("racing")
  receipt = sign("racing", record, "push_ambiguous")
  def read(*_args):
    write_record("racing", head="e" * 40)
    return None
  monkeypatch.setattr(github_routes, "_authoritative_upstream_branch_sha", read)
  retire("racing")
  assert receipt.exists()


def app_token(client, owner_token):
  """An app with GitHub access and its scoped token."""
  from app import models
  from app.database import SessionLocal

  headers = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, headers, name="contribute-test")["id"]
  session = SessionLocal()
  try:
    session.get(models.App, app_id).github_access = True
    session.commit()
  finally:
    session.close()
  response = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=headers,
  )
  assert response.status_code == 200, response.text
  return app_id, response.json()["token"]


ROUTES = {
  "submit": ("{app_id}/one/submit", False),
  "update": ("{app_id}/one/update-existing", False),
  "submit-stack": ("{app_id}/submit-stack", True),
  "update-stack": ("{app_id}/update-stack", True),
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("stale", [True, False])
def test_publish_routes_confirm_before_they_retire_an_attempt(
  client, owner_token, monkeypatch, route, stale,
):
  monkeypatch.setattr(github_routes._limiter, "enabled", False)
  app_id, token = app_token(client, owner_token)
  path, stack = ROUTES[route]
  records = [write_record(value, app_id=app_id) for value in ("one", "two")]
  receipt = sign("one", records[0], "push_ambiguous", app_id=app_id)
  reads = remote_tip(monkeypatch, None)
  confirmed = [
    {**record, "title": "Changed after confirming"} if stale else record
    for record in records
  ]
  checks = []
  original = github_routes._assert_confirmed_publication
  def check(*args, **kwargs):
    checks.append(receipt.exists())
    original(*args, **kwargs)
    if len(checks) == 2:  # the claim's own recheck: stop before any claim
      raise HTTPException(status_code=418)
  monkeypatch.setattr(github_routes, "_assert_confirmed_publication", check)
  body = (
    {"record_ids": ["one", "two"], "expected_records": confirmed}
    if stack else {"expected_record": confirmed[0]}
  )
  response = client.post(
    f"/api/github/contributions/{path.format(app_id=app_id)}",
    headers={"Authorization": f"Bearer {token}"}, json=body,
  )
  if stale:
    # A refused confirmation wrote nothing: the receipt stays and GitHub is
    # never read, so its not-attempted answer is still truthful.
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["not_attempted"] is False
    assert checks == [True] and reads == [] and receipt.exists()
  else:
    assert response.status_code == 418, response.text
    assert checks == [True, False]
    assert reads == [("octocat/app-demo", "fix/demo")] and not receipt.exists()


@pytest.mark.parametrize(("stage", "pushed", "kept"), [
  ("push_ambiguous", OLD_HEAD, False),  # an older head's push: superseded
  ("push_pending", None, False),
  ("push_ambiguous", NEW_HEAD, True),   # the observer can still settle it
  ("pushed", OLD_HEAD, True),           # settled journals are evidence
])
def test_restaging_drops_only_an_unsettled_push_of_an_older_head(stage, pushed, kept):
  record = {"plan": {"head_sha": NEW_HEAD}, "last_submit_stage": stage,
            "last_submit_push_sha": pushed}
  github_routes._clear_superseded_push_stage(record)
  assert ("last_submit_stage" in record) is kept
  assert record["last_submit_push_sha"] == pushed
