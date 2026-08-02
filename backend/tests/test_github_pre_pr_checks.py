"""Focused contract tests for pre-PR GitHub workflow runs."""

import json
import subprocess

import pytest

from app.contribution_errors import ContributionSubmitError
from app import github_pre_pr_checks as checks


def _record(*, repo="mobius-os/mobius", status="prepared"):
  return {
    "type": "pr",
    "status": status,
    "repo": repo,
    "plan": {
      "action": "pr",
      "repo": repo,
      "branch": "fix/reviewed-change",
    },
  }


def _cp(payload, returncode=0):
  stdout = payload if isinstance(payload, str) else json.dumps(payload)
  return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_support_is_narrow_and_active_states_are_explicit():
  assert checks.supports_pre_pr_checks(_record())
  assert not checks.supports_pre_pr_checks(_record(repo="mobius-os/app-demo"))
  assert not checks.supports_pre_pr_checks(_record(status="open"))
  stacked = _record()
  stacked["plan"]["stack"] = {"id": "stack-1"}
  assert not checks.supports_pre_pr_checks(stacked)

  assert checks.pre_pr_checks_active({"state": "dispatching"})
  assert checks.pre_pr_checks_active({"state": "uncertain"})
  assert checks.pre_pr_checks_active({"state": "queued"})
  assert checks.pre_pr_checks_active({"state": "in_progress"})
  assert not checks.pre_pr_checks_active({"state": "completed"})


def test_bootstrap_requires_manual_trigger_on_upstream(monkeypatch, tmp_path):
  calls = []

  def fake_git(repo, *args, check=True):
    calls.append((repo, args, check))
    return _cp("on:\n  pull_request:\n")

  monkeypatch.setattr(checks._git_ops, "_git", fake_git)
  with pytest.raises(ContributionSubmitError) as failure:
    checks._assert_upstream_workflow_dispatchable(
      tmp_path, upstream_sha="a" * 40, workflow="test.yml",
    )
  assert failure.value.code == "pre_pr_checks_unavailable"
  assert "one-time bootstrap" in failure.value.message
  assert calls[0][1] == (
    "show", f"{'a' * 40}:.github/workflows/test.yml",
  )


def test_dispatch_response_without_run_identity_is_uncertain(
  monkeypatch, tmp_path,
):
  monkeypatch.setattr(
    checks, "_api", lambda *args, **kwargs: _cp({"message": "Accepted"}),
  )
  with pytest.raises(ContributionSubmitError) as failure:
    checks._dispatch_workflow(
      tmp_path,
      fork_slug="octocat/mobius",
      workflow="test.yml",
      branch="fix/reviewed-change",
    )
  assert failure.value.code == "pre_pr_checks_uncertain"


def test_exact_workflow_run_snapshot_rejects_the_wrong_head():
  check = {
    "head_sha": "a" * 40,
    "branch": "fix/reviewed-change",
  }
  payload = {
    "event": "workflow_dispatch",
    "head_sha": "b" * 40,
    "head_branch": "fix/reviewed-change",
    "html_url": "https://github.com/octocat/mobius/actions/runs/4",
    "status": "completed",
    "conclusion": "success",
  }
  with pytest.raises(ContributionSubmitError):
    checks._run_snapshot(payload, check)


def test_exact_workflow_run_snapshot_preserves_terminal_result():
  check = {
    "head_sha": "a" * 40,
    "branch": "fix/reviewed-change",
    "message": "old transport warning",
  }
  payload = {
    "event": "workflow_dispatch",
    "head_sha": "a" * 40,
    "head_branch": "fix/reviewed-change",
    "html_url": "https://github.com/octocat/mobius/actions/runs/4",
    "status": "completed",
    "conclusion": "failure",
    "created_at": "2026-08-02T14:00:00Z",
    "updated_at": "2026-08-02T14:03:00Z",
  }
  snapshot = checks._run_snapshot(payload, check)
  assert snapshot["state"] == "completed"
  assert snapshot["conclusion"] == "failure"
  assert snapshot["completed_at"] == payload["updated_at"]
  assert "message" not in snapshot


def test_unchanged_workflow_snapshot_reuses_the_stored_state():
  check = {
    "head_sha": "a" * 40,
    "branch": "fix/reviewed-change",
    "state": "in_progress",
    "conclusion": None,
    "url": "https://github.com/octocat/mobius/actions/runs/4",
    "started_at": "2026-08-02T14:00:00Z",
    "observed_at": "2026-08-02T14:01:00Z",
  }
  payload = {
    "event": "workflow_dispatch",
    "head_sha": check["head_sha"],
    "head_branch": check["branch"],
    "html_url": check["url"],
    "status": "in_progress",
    "conclusion": None,
    "created_at": check["started_at"],
  }
  assert checks._run_snapshot(payload, check) is check
