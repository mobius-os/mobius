"""Focused contract tests for pre-PR GitHub workflow runs."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from app.contribution_errors import ContributionSubmitError
from app import github_auth, github_pre_pr_checks as checks


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_REPOSITORY_GUARD = "github.repository == 'mobius-os/mobius'"


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


def test_only_allowlisted_workflow_jobs_may_run_in_forks():
  """Enabling Tests on a fork must not arm production or scheduled jobs."""
  workflow_dir = ROOT / ".github" / "workflows"
  paths = sorted([
    *workflow_dir.glob("*.yml"),
    *workflow_dir.glob("*.yaml"),
  ])
  allowlisted = set(checks._SUPPORTED_WORKFLOWS.values())
  names = {path.name for path in paths}
  assert allowlisted <= names

  for path in paths:
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    assert isinstance(jobs, dict) and jobs, f"{path.name} must define jobs"
    if path.name in allowlisted:
      assert payload.get("permissions") == {"contents": "read"}, (
        f"{path.name} is fork-runnable and must remain read-only"
      )
      assert all("permissions" not in job for job in jobs.values()), (
        f"{path.name} jobs must not widen the fork workflow token"
      )
      continue
    for job_name, job in jobs.items():
      assert isinstance(job, dict), f"{path.name}:{job_name} must be a job"
      condition = str(job.get("if") or "")
      assert condition == CANONICAL_REPOSITORY_GUARD, (
        f"{path.name}:{job_name} can run in a fork; add the canonical "
        "repository guard or explicitly allowlist the workflow"
      )


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


def test_dispatch_rejects_partial_connection_before_git(monkeypatch, tmp_path):
  monkeypatch.setattr(github_auth, "get_token", lambda: "gh-partial")
  monkeypatch.setattr(
    github_auth,
    "read_state",
    lambda: {"login": "octocat", "scopes": ["public_repo"]},
  )
  monkeypatch.setattr(checks.shutil, "which", lambda name: f"/bin/{name}")

  with pytest.raises(ContributionSubmitError) as failure:
    checks.dispatch_pre_pr_checks(_record(), tmp_path / "reviewed.diff")

  assert failure.value.code == "pre_pr_checks_scope"
  assert "full PR access" in failure.value.message


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
