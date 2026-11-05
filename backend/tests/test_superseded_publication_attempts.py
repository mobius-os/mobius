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

from app import contribution_runtime
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.routes import github as github_routes
from tests.test_github_routes import _app_token  # noqa: F401

APP_ID = 41
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
OLD_TIP = "c" * 40
ACTION = {"action": "submit", "publication_stage": "draft", "autopilot": False}


def write_record(record_id, head=NEW_HEAD, status="prepared", **extra):
  record = {**extra,
    "id": record_id, "type": "pr", "status": status, "repo": "mobius-os/app-demo",
    "branch": "fix/demo", "title": "Polish demo",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo", "branch": "fix/demo",
             "head_sha": head, "title": "Polish demo", "body_draft": "Polishes."},
  }
  path = Path(get_settings().data_dir) / "apps" / str(APP_ID) / "contributions"
  path.mkdir(parents=True, exist_ok=True)
  (path / f"{record_id}.json").write_text(json.dumps(record))
  return record


def sign(record_id, record, phase, *, claimed_head=OLD_HEAD, expected_remote=None):
  claim = {**record, "status": "submitting", "submitter": "contribute-button",
           "plan": {**record["plan"], "head_sha": claimed_head}}
  envelope = github_routes._signed_personal_attempt({
    "version": 1, "app_id": APP_ID, "record_id": record_id,
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
  path = contribution_runtime.personal_attempt_path(APP_ID, record_id, create_parent=True)
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


def test_send_retires_a_superseded_attempt_before_its_claim(client, owner_token, monkeypatch):
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  calls = []
  async def record_call(app, record_id):
    calls.append((app, record_id))
  monkeypatch.setattr(github_routes, "_retire_superseded_attempt", record_call)
  client.post(f"/api/github/contributions/{app_id}/missing-record/submit",
              headers={"Authorization": f"Bearer {app_token}"}, json={})
  assert calls == [(app_id, "missing-record")]
