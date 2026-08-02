"""Run reviewed platform contributions through GitHub before opening a PR.

Preparation stays private.  The explicit Contribute action handled here is a
separate public boundary: it may create/update the owner's fork, push the exact
reviewed branch, and dispatch the allowlisted test workflow, but it never opens
or edits a pull request.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from app import github_auth
from app.contribution_errors import ContributionSubmitError
from app.github_contribution_contract import (
  GIT_SHA as _GIT_SHA,
  PRE_PR_CHECK_ACTIVE_STATES as _ACTIVE_STATES,
)
from app import github_contribution_git as _git_ops
from app import github_contributions as _contrib


_SUPPORTED_WORKFLOWS = {
  "mobius-os/mobius": "test.yml",
}
_API_VERSION = "2026-03-10"
def supports_pre_pr_checks(record: dict) -> bool:
  """True when one prepared record may use the pre-pr-check action."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repo = str(plan.get("repo") or record.get("repo") or "")
  return bool(
    record.get("status") == "prepared"
    and record.get("type") == "pr"
    and plan.get("action") == "pr"
    and not isinstance(plan.get("stack"), dict)
    and repo in _SUPPORTED_WORKFLOWS
  )


def pre_pr_checks_active(check: object) -> bool:
  return isinstance(check, dict) and check.get("state") in _ACTIVE_STATES


def _now() -> datetime:
  return datetime.now(UTC)


def _now_iso() -> str:
  return _now().isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
  try:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
  except (TypeError, ValueError):
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=UTC)
  return parsed.astimezone(UTC)


def _json_output(proc: subprocess.CompletedProcess, message: str) -> dict:
  try:
    payload = json.loads(proc.stdout or "{}")
  except ValueError:
    payload = None
  if not isinstance(payload, dict):
    raise ContributionSubmitError(message)
  return payload


def _api(
  repo: Path,
  *args: str,
  check: bool = True,
) -> subprocess.CompletedProcess:
  return _git_ops._gh(
    repo,
    "api",
    "-H", "Accept: application/vnd.github+json",
    "-H", f"X-GitHub-Api-Version: {_API_VERSION}",
    *args,
    check=check,
  )


def _enable_workflow(repo: Path, fork_slug: str, workflow: str) -> None:
  enabled = _api(
    repo,
    "--method", "PUT",
    f"repos/{fork_slug}/actions/workflows/{quote(workflow, safe='')}/enable",
    check=False,
  )
  if enabled.returncode != 0:
    detail = (enabled.stderr or enabled.stdout or "").strip()
    raise ContributionSubmitError(
      "GitHub could not enable the Tests workflow on the personal fork. "
      "Enable Actions there or reconnect GitHub, then try Run checks again.",
      detail=detail[:600] or None,
      code="pre_pr_checks_disabled",
    )


def _assert_upstream_workflow_dispatchable(
  repo: Path, *, upstream_sha: str, workflow: str,
) -> None:
  source = _git_ops._git(
    repo,
    "show",
    f"{upstream_sha}:.github/workflows/{workflow}",
    check=False,
  )
  if source.returncode != 0 or "workflow_dispatch:" not in source.stdout:
    raise ContributionSubmitError(
      "This is the one-time bootstrap for pre-PR checks: the manual Tests "
      "trigger must reach upstream main before GitHub can run it from forks. "
      "Send this contribution normally; later platform contributions can run "
      "checks first.",
      code="pre_pr_checks_unavailable",
    )


def _dispatch_workflow(
  repo: Path,
  *,
  fork_slug: str,
  workflow: str,
  branch: str,
) -> dict:
  try:
    result = _api(
      repo,
      "--method", "POST",
      f"repos/{fork_slug}/actions/workflows/{quote(workflow, safe='')}/dispatches",
      "-f", f"ref={branch}",
      "-F", "return_run_details=true",
      check=False,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise ContributionSubmitError(
      "The reviewed branch was pushed, but Contribute could not confirm "
      "whether GitHub started the checks. Reopen Contribute to reconcile the "
      "run before trying again.",
      code="pre_pr_checks_uncertain",
    ) from exc
  if result.returncode != 0:
    detail = (result.stderr or result.stdout or "").strip()
    raise ContributionSubmitError(
      "The reviewed branch was pushed, but GitHub could not start its Tests "
      "workflow.",
      detail=detail[:600] or None,
      code="pre_pr_checks_dispatch_failed",
    )
  try:
    payload = _json_output(
      result,
      "GitHub started the checks but did not return their run details.",
    )
  except ContributionSubmitError as exc:
    raise ContributionSubmitError(
      exc.message,
      detail=exc.detail,
      code="pre_pr_checks_uncertain",
    ) from exc
  try:
    run_id = int(payload.get("workflow_run_id"))
  except (TypeError, ValueError):
    run_id = 0
  url = str(payload.get("html_url") or "")
  if run_id <= 0 or not url.startswith("https://github.com/"):
    raise ContributionSubmitError(
      "GitHub started the checks but did not return their run details.",
      code="pre_pr_checks_uncertain",
    )
  return {"run_id": run_id, "url": url}


def dispatch_pre_pr_checks(
  record: dict,
  diff_path: Path,
) -> tuple[dict, dict]:
  """Push the exact reviewed branch to the owner fork and start Tests.

  Returns ``(pre_pr_checks, record_patch)``.  ``record_patch`` may contain a
  normalized reviewed head SHA and fork/push evidence; callers must persist it
  with the run atomically.
  """
  if not shutil.which("git") or not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs git and gh installed before it can run GitHub checks."
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  login = str(state.get("login") or "")
  scopes = set(state.get("scopes") or [])
  if not token or not login:
    raise ContributionSubmitError("Connect GitHub before running checks.", 401)
  if "workflow" not in scopes:
    raise ContributionSubmitError(
      "Reconnect GitHub with full PR access before running checks on a fork.",
      status_code=409,
      code="pre_pr_checks_scope",
    )
  if not supports_pre_pr_checks(record):
    raise ContributionSubmitError(
      "Pre-PR GitHub checks are currently available for standalone Möbius "
      "platform contributions only.",
      status_code=409,
      code="pre_pr_checks_unsupported",
    )

  plan = record.get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(
    plan.get("repo") or record.get("repo")
  )
  workflow = _SUPPORTED_WORKFLOWS[upstream_repo]
  branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
  repo = _contrib._safe_repo_path(plan.get("repo_path"))
  if not (repo / ".git").exists():
    raise ContributionSubmitError("The staged repo is not a git checkout.")
  author_name, author_email = _git_ops._connected_git_identity(state, login)

  checkout_back = None
  pushed: dict = {}
  record_patch: dict = {}
  try:
    current_branch = _git_ops._git(
      repo, "rev-parse", "--abbrev-ref", "HEAD",
    ).stdout.strip()
    checkout_back = (
      _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
      if current_branch == "HEAD" else current_branch
    )
    _git_ops._git(repo, "check-ref-format", "--branch", branch)
    _git_ops._assert_clean_worktree(repo)
    _git_ops._git(repo, "checkout", "-q", branch)
    expected_base, _, expected_diff = _git_ops._assert_fresh(
      record, diff_path, repo, branch,
    )
    _git_ops._assert_coauthor_trailer(repo, branch)
    record_patch = _git_ops._normalize_head_attribution(
      repo,
      branch,
      author_name=author_name,
      author_email=author_email,
      base_sha=expected_base,
      expected_diff=expected_diff,
      record=record,
    )
    merge_patch = _git_ops._assert_merges_with_upstream(
      repo, upstream_repo, branch,
    )
    record_patch = _git_ops._record_patch_with(record_patch, merge_patch)
    _assert_upstream_workflow_dispatchable(
      repo,
      upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
      workflow=workflow,
    )

    conflict = _contrib._existing_branch_pr(
      repo, upstream_repo, login, branch, same_repo=False,
    )
    if conflict is not None:
      url, state_label = conflict
      raise ContributionSubmitError(
        f"This branch already has a {state_label} pull request: {url}. "
        "Use that pull request's checks instead of starting a private run.",
        code="pre_pr_checks_existing_pr",
      )

    fork_slug = _contrib._ensure_owner_fork_remote(repo, upstream_repo, login)
    fork_patch = _contrib._inspect_owner_fork_default_branch(
      repo,
      fork_slug,
      upstream_branch=str(merge_patch["last_submit_upstream_branch"]),
      upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
    )
    if (
      fork_patch.get("last_submit_fork_sync") == "strictly-behind"
      or fork_patch.get("last_submit_fork_carrier_branch")
    ):
      fork_patch = _contrib._sync_owner_fork_with_workflow_scope(
        repo,
        fork_slug,
        upstream_branch=str(merge_patch["last_submit_upstream_branch"]),
        upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
      )
    record_patch = _git_ops._record_patch_with(record_patch, fork_patch)

    push_source, record_patch = _contrib._push_reviewed_topic(
      repo,
      branch=branch,
      fork_slug=fork_slug,
      merge_patch=merge_patch,
      record_patch=record_patch,
      diff_path=diff_path,
      expected_diff=expected_diff,
      author_name=author_name,
      author_email=author_email,
      workflow_scope=True,
    )
    pushed_sha = _git_ops._git(repo, "rev-parse", push_source).stdout.strip()
    if not _GIT_SHA.fullmatch(pushed_sha):
      raise ContributionSubmitError(
        "Could not verify the exact reviewed commit after pushing this branch."
      )
    pushed = {
      "fork_repo": fork_slug,
      "branch": branch,
      "head_sha": pushed_sha,
      "workflow": workflow,
    }
    branch_url = f"https://github.com/{fork_slug}/tree/{quote(branch, safe='/')}"
    record_patch = _git_ops._record_patch_with(record_patch, {
      "head_repository": fork_slug,
      "last_pushed_branch": f"{login}:{branch}",
      "last_pushed_branch_url": branch_url,
      "last_submit_push_sha": pushed_sha,
    })

    _enable_workflow(repo, fork_slug, workflow)
    pushed["requested_at"] = _now_iso()
    dispatched = _dispatch_workflow(
      repo,
      fork_slug=fork_slug,
      workflow=workflow,
      branch=branch,
    )
    return ({
      **pushed,
      **dispatched,
      "state": "queued",
      "conclusion": None,
      "observed_at": _now_iso(),
    }, record_patch)
  except ContributionSubmitError as exc:
    combined_patch = _git_ops._record_patch_with(
      record_patch, exc.record_patch,
    )
    if pushed and not isinstance(combined_patch.get("pre_pr_checks"), dict):
      check_state = "uncertain" if exc.code == "pre_pr_checks_uncertain" else "error"
      patch = {
        **pushed,
        "state": check_state,
        "conclusion": None,
        "message": exc.message,
        "observed_at": _now_iso(),
      }
      raise ContributionSubmitError(
        exc.message,
        exc.status_code,
        detail=exc.detail,
        record_patch={**combined_patch, "pre_pr_checks": patch},
        code=exc.code,
      ) from exc
    if combined_patch:
      raise ContributionSubmitError(
        exc.message,
        exc.status_code,
        detail=exc.detail,
        record_patch=combined_patch,
        code=exc.code,
      ) from exc
    raise
  finally:
    if checkout_back:
      _git_ops._git(repo, "checkout", "-q", checkout_back, check=False)


def _run_snapshot(payload: dict, check: dict) -> dict:
  event = str(payload.get("event") or "")
  head_sha = str(payload.get("head_sha") or "")
  head_branch = str(payload.get("head_branch") or "")
  html_url = str(payload.get("html_url") or "")
  if (
    event != "workflow_dispatch"
    or head_sha != str(check.get("head_sha") or "")
    or head_branch != str(check.get("branch") or "")
    or not html_url.startswith("https://github.com/")
  ):
    raise ContributionSubmitError(
      "GitHub returned a workflow run that does not match the reviewed branch."
    )
  status = str(payload.get("status") or "")
  if status not in {"queued", "in_progress", "completed"}:
    status = "in_progress"
  next_check = {
    **check,
    "state": status,
    "conclusion": payload.get("conclusion") if status == "completed" else None,
    "url": html_url,
  }
  if payload.get("created_at"):
    next_check["started_at"] = payload["created_at"]
  if status == "completed" and payload.get("updated_at"):
    next_check["completed_at"] = payload["updated_at"]
  next_check.pop("message", None)
  prior = {key: value for key, value in check.items() if key != "observed_at"}
  current = {
    key: value for key, value in next_check.items() if key != "observed_at"
  }
  if current == prior:
    return check
  next_check["observed_at"] = _now_iso()
  return next_check


def _find_uncertain_run(repo: Path, check: dict) -> dict | None:
  fork_slug = _git_ops._validate_repo_slug(check.get("fork_repo"))
  branch = _git_ops._validate_branch(check.get("branch"))
  head_sha = str(check.get("head_sha") or "")
  if not _GIT_SHA.fullmatch(head_sha):
    return None
  path = (
    f"repos/{fork_slug}/actions/workflows/"
    f"{quote(str(check.get('workflow') or ''), safe='')}/runs"
    f"?event=workflow_dispatch&branch={quote(branch, safe='')}"
    f"&head_sha={quote(head_sha, safe='')}&per_page=10"
  )
  result = _api(repo, path, check=False)
  if result.returncode != 0:
    return None
  payload = _json_output(result, "Could not reconcile the GitHub checks.")
  runs = payload.get("workflow_runs")
  requested = _parse_iso(check.get("requested_at"))
  if not isinstance(runs, list) or requested is None:
    return None
  matches = []
  for run in runs:
    if not isinstance(run, dict):
      continue
    created = _parse_iso(run.get("created_at"))
    if created is not None and created >= requested - timedelta(seconds=15):
      matches.append(run)
  return matches[0] if len(matches) == 1 else None


def refresh_pre_pr_check(record: dict) -> dict | None:
  """Return a fresh pre-pr-check snapshot, or None when no refresh is due."""
  check = record.get("pre_pr_checks")
  if not supports_pre_pr_checks(record) or not pre_pr_checks_active(check):
    return None
  plan = record.get("plan") or {}
  repo = _contrib._safe_repo_path(plan.get("repo_path"))
  if not (repo / ".git").exists():
    return {
      **check,
      "state": "error",
      "message": "The prepared review checkout is no longer available.",
      "observed_at": _now_iso(),
    }

  run_id = check.get("run_id")
  if not run_id and check.get("state") == "uncertain":
    matched = _find_uncertain_run(repo, check)
    if matched is not None:
      run_id = matched.get("id")
      check = {
        **check,
        "run_id": run_id,
        "url": matched.get("html_url") or check.get("url"),
      }
    elif (
      (requested := _parse_iso(check.get("requested_at"))) is not None
      and _now() - requested > timedelta(minutes=3)
    ):
      return {
        **check,
        "state": "error",
        "message": (
          "GitHub did not expose a matching workflow run. The reviewed branch "
          "is still on the personal fork; Run checks can be tried again."
        ),
        "observed_at": _now_iso(),
      }
    else:
      return check

  try:
    run_id = int(run_id)
  except (TypeError, ValueError):
    return check
  fork_slug = _git_ops._validate_repo_slug(check.get("fork_repo"))
  result = _api(
    repo, f"repos/{fork_slug}/actions/runs/{run_id}", check=False,
  )
  if result.returncode != 0:
    return check
  payload = _json_output(result, "Could not inspect the GitHub checks.")
  return _run_snapshot(payload, check)
