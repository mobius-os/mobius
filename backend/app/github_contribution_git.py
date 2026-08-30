"""Low-level Git/GitHub validation for reviewed contribution actions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from app import github_auth
from app.contribution_errors import ContributionSubmitError
from app.github_contribution_contract import (
  BRANCH_NAME as _BRANCH_NAME,
  COAUTHOR_TRAILER as _COAUTHOR_TRAILER,
  GITHUB_LOGIN as _GITHUB_LOGIN,
  GITHUB_REPO as _GITHUB_REPO,
  GIT_SHA as _GIT_SHA,
  SUBMIT_TIMEOUT_SECONDS as _SUBMIT_TIMEOUT,
)
from app.terminal_output import readable_output


_FETCH_ATTEMPTS = 2


def _is_transient_transport_error(message: str) -> bool:
  """Classify failures worth one safe retry before any public mutation.

  Rate limiting (429) is deliberately absent. The preflight retry below is
  sleepless, and GitHub answers a throttled caller with Retry-After, so an
  immediate second attempt would spend the only retry on a near-certain
  second rejection and add one more request to the throttle.
  """
  detail = str(message or "").lower()
  transient_markers = (
    "could not resolve host",
    "failed to connect",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "remote end hung up unexpectedly",
    "remote hung up unexpectedly",
    "temporarily unavailable",
    "empty reply from server",
    "early eof",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "the requested url returned error: 500",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
  )
  return any(marker in detail for marker in transient_markers)


def _validate_repo_slug(value: object) -> str:
  repo = str(value or "")
  if not _GITHUB_REPO.match(repo):
    raise ContributionSubmitError("The staged GitHub repo is invalid.")
  return repo


def _validate_branch(value: object) -> str:
  branch = str(value or "")
  if (
    not _BRANCH_NAME.match(branch)
    or branch.startswith("-")
    or ".." in branch
    or "//" in branch
    or branch.endswith(("/", ".", ".lock"))
  ):
    raise ContributionSubmitError("The staged branch name is invalid.")
  return branch


def _git_env(repo: Path) -> dict:
  env = dict(os.environ)
  for var in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
  ):
    env.pop(var, None)
  env["GIT_CEILING_DIRECTORIES"] = str(repo.resolve().parent)
  env["GIT_TERMINAL_PROMPT"] = "0"
  env["GH_PROMPT_DISABLED"] = "1"
  token = github_auth.get_token()
  if token:
    env["GH_TOKEN"] = token
  if github_auth.GH_AUTH_DIR.exists():
    env["GH_CONFIG_DIR"] = str(github_auth.GH_AUTH_DIR)
  return env


def _run_cmd(
  argv: list[str], *,
  cwd: Path,
  check: bool = True,
  timeout: int = _SUBMIT_TIMEOUT,
  env: dict | None = None,
) -> subprocess.CompletedProcess:
  proc = subprocess.run(
    argv,
    cwd=str(cwd),
    capture_output=True,
    text=True,
    timeout=timeout,
    check=False,
    env=env or _git_env(cwd),
  )
  if check and proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or "command failed").strip()
    raise ContributionSubmitError(detail[:600] or "GitHub command failed.")
  return proc


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return _run_cmd(["git", "-C", str(repo), *args], cwd=repo, check=check)


def _gh(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return _run_cmd(["gh", *args], cwd=repo, check=check)


def _assert_clean_worktree(repo: Path) -> None:
  status = _git(repo, "status", "--porcelain").stdout.strip()
  if status:
    raise ContributionSubmitError(
      "This staged branch has uncommitted source changes. Ask your agent "
      "to prepare the PR again before submitting.",
      code="working_changes",
    )


def _assert_coauthor_trailer(repo: Path, branch: str) -> None:
  body = _git(repo, "log", "-1", "--format=%B", branch).stdout
  if _COAUTHOR_TRAILER not in body:
    raise ContributionSubmitError(
      "This staged commit is missing the Möbius Agent co-author trailer. "
      "Leave feedback so your agent can prepare it again.",
      code="missing_coauthor",
    )


def _connected_git_identity(state: dict, login: str) -> tuple[str, str]:
  if not _GITHUB_LOGIN.match(login):
    raise ContributionSubmitError("Reconnect GitHub before approving this PR.", 401)
  user_id = str(state.get("user_id") or "").strip()
  if user_id and not user_id.isdigit():
    raise ContributionSubmitError("Reconnect GitHub before approving this PR.", 401)
  return login, github_auth.noreply_email(login, user_id)


def _head_commit_metadata(repo: Path, branch: str) -> dict:
  out = _git(
    repo,
    "show", "-s",
    "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI",
    branch,
  ).stdout.rstrip("\n")
  parts = out.split("\x00")
  if len(parts) != 7:
    raise ContributionSubmitError(
      "Could not inspect the staged commit attribution. Ask your agent "
      "to prepare this PR again."
    )
  return {
    "sha": parts[0],
    "tree": parts[1],
    "author_name": parts[2],
    "author_email": parts[3],
    "committer_name": parts[4],
    "committer_email": parts[5],
    "author_date": parts[6],
  }


def _head_sha_patch(record: dict, old_head: str, new_head: str) -> dict:
  if old_head == new_head:
    return {}
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  return {
    "head_sha": new_head,
    "plan": {
      **plan,
      "head_sha": new_head,
      "attribution_normalized_from": old_head,
    },
  }


def _merge_error_patch(exc: ContributionSubmitError, patch: dict) -> ContributionSubmitError:
  if not patch:
    return exc
  return ContributionSubmitError(
    exc.message,
    exc.status_code,
    record_patch={**patch, **exc.record_patch},
    code=exc.code,
    detail=exc.detail,
  )


def _record_patch_with(base: dict, extra: dict) -> dict:
  if not extra:
    return base
  if not base:
    return extra
  return {**base, **extra}


def _normalize_head_attribution(
  repo: Path,
  branch: str,
  *,
  author_name: str,
  author_email: str,
  base_sha: str,
  expected_diff: str,
  record: dict,
) -> dict:
  before = _head_commit_metadata(repo, branch)
  if (
    before["author_name"] == author_name
    and before["author_email"] == author_email
    and before["committer_name"] == author_name
    and before["committer_email"] == author_email
  ):
    return {}

  _git(
    repo,
    "-c", f"user.name={author_name}",
    "-c", f"user.email={author_email}",
    "commit", "--amend", "--no-edit", "--no-gpg-sign",
    "--author", f"{author_name} <{author_email}>",
    "--date", before["author_date"],
  )

  after = _head_commit_metadata(repo, branch)
  if after["tree"] != before["tree"]:
    raise ContributionSubmitError(
      "Normalizing commit attribution changed the staged source tree. "
      "Ask your agent to prepare this PR again."
    )
  if (
    after["author_name"] != author_name
    or after["author_email"] != author_email
    or after["committer_name"] != author_name
    or after["committer_email"] != author_email
  ):
    raise ContributionSubmitError(
      "Could not normalize the staged commit attribution. Ask your agent "
      "to prepare this PR again."
    )
  branch_hash = hashlib.sha256(
    _reviewed_branch_diff(repo, base_sha, after["sha"])
  ).hexdigest()
  if branch_hash != expected_diff:
    raise ContributionSubmitError(
      "Normalizing commit attribution changed the reviewed diff. Ask your "
      "agent to prepare this PR again."
    )
  return _head_sha_patch(record, before["sha"], after["sha"])


def _assert_head_attribution(
  repo: Path,
  branch: str,
  *,
  author_name: str,
  author_email: str,
) -> None:
  """Require a stack commit to already carry the connected owner identity.

  Standalone submissions may safely amend their one reviewed commit before
  push.  A stack cannot: rewriting a parent commit would invalidate every
  child's reviewed base SHA and ancestry.  Stack preparation therefore pins
  the identity up front and submission only verifies it.
  """
  metadata = _head_commit_metadata(repo, branch)
  if (
    metadata["author_name"] != author_name
    or metadata["author_email"] != author_email
    or metadata["committer_name"] != author_name
    or metadata["committer_email"] != author_email
  ):
    raise ContributionSubmitError(
      "This PR stack was prepared with a different commit identity. Leave "
      "feedback so your agent can rebuild the stack without rewriting its "
      "reviewed parent links."
    )


def _upstream_default_branch(repo: Path, upstream_repo: str) -> str:
  proc = _gh(
    repo,
    "repo", "view", upstream_repo,
    "--json", "defaultBranchRef",
    "--jq", ".defaultBranchRef.name",
    check=False,
  )
  branch = (proc.stdout or "").strip() if proc.returncode == 0 else ""
  if not branch:
    branch = "main"
  return _validate_branch(branch)


def _assert_upstream_push_permission(repo: Path, upstream_repo: str) -> None:
  """True GitHub stacks need their base branches in the upstream repository."""
  proc = _gh(
    repo,
    "api", f"repos/{upstream_repo}",
    "--jq", ".permissions.push",
    check=False,
  )
  if proc.returncode != 0 or (proc.stdout or "").strip().lower() != "true":
    raise ContributionSubmitError(
      "GitHub only allows a PR to target a branch in its base repository. "
      "This account cannot publish the upstream stack branches, so nothing "
      "was sent. Submit these as independent PRs or use an account with "
      "upstream push access."
    )


def _upstream_branch_sha(
  repo: Path, upstream_repo: str, branch: str,
) -> str | None:
  """Read one upstream branch tip, returning ``None`` on an invalid response."""
  branch = _validate_branch(branch)
  proc = _gh(
    repo,
    "api",
    f"repos/{upstream_repo}/git/ref/heads/{quote(branch, safe='')}",
    "--jq", ".object.sha",
    check=False,
  )
  actual_sha = (proc.stdout or "").strip() if proc.returncode == 0 else ""
  return actual_sha if _GIT_SHA.match(actual_sha) else None


def _assert_upstream_branch_at(
  repo: Path,
  upstream_repo: str,
  branch: str,
  expected_sha: str,
) -> None:
  """Require an already-public stack parent to remain at its reviewed tip."""
  branch = _validate_branch(branch)
  if not _GIT_SHA.match(str(expected_sha or "")):
    raise ContributionSubmitError(
      "An existing PR stack parent has no valid reviewed commit. Leave "
      "feedback so your agent can prepare the remaining layers again."
    )
  actual_sha = _upstream_branch_sha(repo, upstream_repo, branch)
  if actual_sha is None:
    raise ContributionSubmitError(
      f"The existing stack base {branch} is no longer available upstream. "
      "Nothing was sent; leave feedback so your agent can refresh the "
      "remaining layers."
    )
  if actual_sha != expected_sha:
    raise ContributionSubmitError(
      f"The existing stack base {branch} changed after review. Nothing was "
      "sent; leave feedback so your agent can refresh the remaining layers."
    )


def _assert_unprotected_landing_target(
  repo: Path, upstream_repo: str, branch: str,
) -> None:
  """Atomic stack landing is deliberately limited to unprotected app refs.

  The platform repository and any app that has opted into branch protection
  keep GitHub's ordinary merge/queue path.  Admin bypass is not treated as
  permission to skip those repository-owned invariants.
  """
  encoded = quote(_validate_branch(branch), safe="")
  protection = _gh(
    repo,
    "api", f"repos/{upstream_repo}/branches/{encoded}/protection",
    check=False,
  )
  protection_detail = (protection.stderr or protection.stdout or "").lower()
  if protection.returncode == 0:
    raise ContributionSubmitError(
      f"{branch} is protected, so Contribute will not bypass its merge rules. "
      "Use GitHub's normal merge or merge queue for this stack."
    )
  if "404" not in protection_detail and "not protected" not in protection_detail:
    raise ContributionSubmitError(
      f"Contribute could not verify that {branch} is safe for atomic landing. "
      "Nothing was changed; try again after GitHub is reachable."
    )

  rules = _gh(
    repo,
    "api", f"repos/{upstream_repo}/rules/branches/{encoded}",
    check=False,
  )
  if rules.returncode != 0:
    raise ContributionSubmitError(
      f"Contribute could not inspect the active rules for {branch}. Nothing "
      "was changed; use GitHub's normal merge flow."
    )
  try:
    active_rules = json.loads(rules.stdout or "[]")
  except ValueError:
    active_rules = None
  if not isinstance(active_rules, list):
    raise ContributionSubmitError(
      f"Contribute could not understand the active rules for {branch}. "
      "Nothing was changed; use GitHub's normal merge flow."
    )
  if active_rules:
    raise ContributionSubmitError(
      f"{branch} has repository rules, so Contribute will not bypass them. "
      "Use GitHub's normal merge or merge queue for this stack."
    )


def _assert_pr_checks_green(
  repo: Path,
  *,
  upstream_repo: str,
  record: dict,
  base_branch: str,
  head_branch: str,
) -> None:
  number = record.get("number")
  if not isinstance(number, int) or number <= 0:
    raise ContributionSubmitError(
      "A pull request in this stack has no verified GitHub number. Refresh "
      "Contribute before landing it."
    )
  proc = _gh(
    repo,
    "pr", "view", str(number),
    "-R", upstream_repo,
    "--json",
    "state,isDraft,baseRefName,headRefName,headRepositoryOwner,statusCheckRollup,url",
    check=False,
  )
  if proc.returncode != 0:
    raise ContributionSubmitError(
      f"Contribute could not verify pull request #{number}. Nothing was "
      "changed; refresh and try again."
    )
  try:
    data = json.loads(proc.stdout or "{}")
  except ValueError:
    data = None
  if not isinstance(data, dict):
    raise ContributionSubmitError(
      f"GitHub returned an invalid result for pull request #{number}."
    )
  owner = data.get("headRepositoryOwner")
  owner_login = owner.get("login") if isinstance(owner, dict) else ""
  upstream_owner = upstream_repo.split("/", 1)[0]
  if (
    data.get("state") != "OPEN"
    or bool(data.get("isDraft"))
    or data.get("baseRefName") != base_branch
    or data.get("headRefName") != head_branch
    or str(owner_login).lower() != upstream_owner.lower()
  ):
    raise ContributionSubmitError(
      f"Pull request #{number} no longer matches the reviewed stack. Nothing "
      "was changed; refresh Contribute before landing it."
    )
  expected_url = str(record.get("url") or "")
  if expected_url and data.get("url") != expected_url:
    raise ContributionSubmitError(
      f"Pull request #{number} no longer matches its Contribute record."
    )

  checks = data.get("statusCheckRollup")
  if not isinstance(checks, list) or not checks:
    raise ContributionSubmitError(
      f"Pull request #{number} has no completed CI checks yet. Nothing was "
      "changed; wait for CI and try again."
    )
  unfinished = []
  failed = []
  for check in checks:
    if not isinstance(check, dict):
      failed.append("unknown check")
      continue
    name = str(check.get("name") or check.get("context") or "check")
    if check.get("__typename") == "StatusContext":
      state = str(check.get("state") or "").upper()
      if state == "SUCCESS":
        continue
      if state in {"PENDING", "EXPECTED"}:
        unfinished.append(name)
      else:
        failed.append(name)
      continue
    status = str(check.get("status") or "").upper()
    conclusion = str(check.get("conclusion") or "").upper()
    if status != "COMPLETED" or not conclusion:
      unfinished.append(name)
    elif conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
      failed.append(name)
  if failed:
    raise ContributionSubmitError(
      f"Pull request #{number} has failing CI ({', '.join(failed[:3])}). "
      "Nothing was changed."
    )
  if unfinished:
    raise ContributionSubmitError(
      f"Pull request #{number} still has CI running ({', '.join(unfinished[:3])}). "
      "Nothing was changed; try again when it is green."
    )


def _assert_merges_with_upstream(
  repo: Path, upstream_repo: str, branch: str,
) -> dict:
  upstream_branch = _upstream_default_branch(repo, upstream_repo)
  remote_url = f"https://github.com/{upstream_repo}.git"
  ref_key = hashlib.sha256(
    f"{upstream_repo}\0{branch}\0{time.time_ns()}".encode("utf-8")
  ).hexdigest()[:24]
  upstream_ref = f"refs/mobius-submit/upstream-{ref_key}"
  preflight_patch = {"last_submit_upstream_branch": upstream_branch}
  try:
    fetch_detail = ""
    fetched = None
    fetch_was_transient = False
    for attempt in range(_FETCH_ATTEMPTS):
      fetched = None
      try:
        fetched = _git(
          repo,
          "fetch", "--no-tags", "--force",
          remote_url,
          f"+refs/heads/{upstream_branch}:{upstream_ref}",
          check=False,
        )
      except subprocess.TimeoutExpired as exc:
        fetch_detail = readable_output(
          exc.stderr or exc.stdout or
          f"Git fetch timed out after {exc.timeout} seconds."
        )
        fetch_was_transient = True
        if attempt + 1 < _FETCH_ATTEMPTS:
          continue
        break
      if fetched.returncode == 0:
        break
      observed_detail = readable_output(fetched.stderr or fetched.stdout or "")
      if observed_detail:
        fetch_detail = observed_detail
      fetch_was_transient = _is_transient_transport_error(fetch_detail)
      if not fetch_was_transient:
        break
      # Starting a fresh git process is the recovery boundary. There is no
      # sleep here: Send remains bounded, and deterministic rejections never
      # consume a second attempt.
      if attempt + 1 >= _FETCH_ATTEMPTS:
        break
    if fetched is None or fetched.returncode != 0:
      transient = fetch_was_transient
      raise ContributionSubmitError(
        (
          "GitHub was temporarily unreachable while Contribute checked "
          f"upstream {upstream_branch}. Nothing was published. Try Send "
          "again; leave feedback only if it keeps failing."
          if transient else
          f"GitHub could not provide upstream {upstream_branch} while "
          "Contribute checked mergeability. Nothing was published. Leave "
          "feedback so your agent can inspect it."
        ),
        record_patch=preflight_patch,
        code=(
          "upstream_fetch_unavailable" if transient
          else "upstream_fetch_failed"
        ),
        detail=fetch_detail or "Git fetch failed without diagnostic output.",
      ) from None
    upstream_sha = _git(
      repo, "rev-parse", "--verify", f"{upstream_ref}^{{commit}}",
    ).stdout.strip()
    if not _GIT_SHA.match(upstream_sha):
      raise ContributionSubmitError(
        "Could not resolve the upstream branch for this PR. Leave feedback "
        "so your agent can refresh it.",
        record_patch=preflight_patch,
      )
    preflight_patch["last_submit_upstream_sha"] = upstream_sha
    merged = _git(
      repo, "merge-tree", "--write-tree", upstream_sha, branch, check=False,
    )
    if merged.returncode != 0:
      raise ContributionSubmitError(
        (
          f"This PR no longer merges cleanly with upstream {upstream_branch}. "
          "Leave feedback so your agent can refresh the branch before it is "
          "pushed."
        ),
        record_patch=preflight_patch,
      )
    return preflight_patch
  finally:
    _git(repo, "update-ref", "-d", upstream_ref, check=False)

def _conflicts_with_recorded_upstream(record: dict, repo, branch: str) -> bool:
  """Whether this branch still fails to merge the upstream it last saw.

  A conflict is a fact about two commits, so DERIVE it from the branch as it
  stands rather than trusting a verdict a past attempt left behind. A refreshed
  branch stops reporting one the moment it genuinely merges, and nothing has to
  remember to clear a flag — the failure mode of every stored classification.

  The upstream commit is already in this checkout's object store, fetched by the
  submit that recorded its sha, so this stays local and never reaches the
  network. It can only go stale in the safe direction: upstream moving on is
  invisible here, and the authoritative check against live upstream still runs
  at submit before anything is pushed.

  ``merge-tree --write-tree`` writes the merged tree it computes, so this leaves
  unreferenced objects behind for gc — the same objects the submit-time check
  already writes. It touches no working tree, index, branch or ref, which is
  what the read-only review endpoint calling it promises.
  """
  upstream_sha = str(record.get("last_submit_upstream_sha") or "")
  if not _GIT_SHA.match(upstream_sha):
    return False
  present = _git(
    repo, "cat-file", "-e", f"{upstream_sha}^{{commit}}", check=False,
  )
  if present.returncode != 0:
    return False
  merged = _git(
    repo, "merge-tree", "--write-tree", upstream_sha, branch, check=False,
  )
  return merged.returncode != 0



def _resolve_reviewed_commit(repo: Path, value: object, label: str) -> str:
  raw = str(value or "").strip()
  if not raw:
    raise ContributionSubmitError(
      f"This record needs to be prepared again: it has no reviewed {label}."
    )
  if not _GIT_SHA.match(raw):
    raise ContributionSubmitError(f"The reviewed {label} is invalid.")
  try:
    resolved = _git(
      repo, "rev-parse", "--verify", f"{raw}^{{commit}}"
    ).stdout.strip()
  except ContributionSubmitError:
    raise ContributionSubmitError(
      f"The reviewed {label} is not present in the staged repo."
    )
  if not _GIT_SHA.match(resolved):
    raise ContributionSubmitError(f"The reviewed {label} resolved incorrectly.")
  return resolved


def _reviewed_branch_diff(repo: Path, base_sha: str, head_sha: str) -> bytes:
  proc = _git(
    repo,
    "-c", "core.quotePath=false",
    "diff",
    "--no-ext-diff",
    "--no-color",
    "--binary",
    "--full-index",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    f"{base_sha}..{head_sha}",
  )
  return proc.stdout.encode("utf-8")


def _assert_fresh(
  record: dict, diff_path: Path, repo: Path, branch: str,
) -> tuple[str, str, str]:
  plan = record.get("plan") or {}
  expected_base = _resolve_reviewed_commit(repo, plan.get("base_sha"), "base sha")
  expected_head = _resolve_reviewed_commit(
    repo, plan.get("head_sha") or record.get("head_sha"), "head sha"
  )
  ancestry = _git(
    repo,
    "merge-base",
    "--is-ancestor",
    expected_base,
    expected_head,
    check=False,
  )
  if ancestry.returncode != 0:
    raise ContributionSubmitError(
      "The reviewed branch is no longer based on its recorded parent. Ask "
      "your agent to prepare this contribution again.",
      code="invalid_ancestry",
    )
  actual_head = _git(repo, "rev-parse", branch).stdout.strip()
  if actual_head != expected_head:
    raise ContributionSubmitError(
      "This branch changed after review. Ask your agent to refresh the "
      "Contribute card before submitting.",
      code="branch_moved",
    )
  expected_diff = str(plan.get("diff_sha256") or "").strip()
  if not expected_diff:
    raise ContributionSubmitError(
      "This record needs to be prepared again: it has no reviewed diff hash.",
      code="missing_diff_hash",
    )
  try:
    diff_bytes = diff_path.read_bytes()
  except OSError:
    raise ContributionSubmitError(
      "The reviewed diff is missing. Ask your agent to prepare this again.",
      code="missing_diff",
    )
  stored_hash = hashlib.sha256(diff_bytes).hexdigest()
  if stored_hash != expected_diff:
    raise ContributionSubmitError(
      "The reviewed diff changed. Ask your agent to refresh the "
      "Contribute card before submitting.",
      code="review_changed",
    )
  branch_hash = hashlib.sha256(
    _reviewed_branch_diff(repo, expected_base, expected_head)
  ).hexdigest()
  if branch_hash != expected_diff:
    raise ContributionSubmitError(
      "The reviewed diff does not match the branch that would be pushed. "
      "Ask your agent to prepare this PR again.",
      code="diff_mismatch",
    )
  return expected_base, expected_head, expected_diff
