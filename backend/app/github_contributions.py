"""Reviewed contribution preparation, publication, and landing operations.

These functions own Git/repository invariants and record state transitions. The
HTTP router remains responsible for authenticating the caller and obtaining the
owner's explicit action; this module receives only an already-authorized call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import (
  app_git,
  fs_locks,
  github_auth,
  github_contribution_git as _git_ops,
  models,
)
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError, push_rejected
from app.terminal_output import readable_output
from app.github_contribution_contract import (
  BRANCH_NAME as _BRANCH_NAME,
  COAUTHOR_TRAILER as _COAUTHOR_TRAILER,
  GITHUB_LOGIN as _GITHUB_LOGIN,
  GITHUB_REPO as _GITHUB_REPO,
  GIT_SHA as _GIT_SHA,
  PRE_PR_CHECK_ACTIVE_STATES as _PRE_PR_CHECK_ACTIVE_STATES,
  SUBMIT_TIMEOUT_SECONDS as _SUBMIT_TIMEOUT,
)
from app.contribution_records import (
  now_iso as _now_iso,
  read_record as _read_record,
  record_paths as _record_paths,
  write_record as _write_record,
)
from app.deps import Principal


_CONTRIBUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PUSH_RETRIES = 3
_PUSH_RETRY_BASE_SECONDS = 0.5

def _require_github_access_principal(
  principal: Principal, db: Session
) -> models.Owner:
  if principal.app_id is None:
    return principal.owner
  app = (
    db.query(models.App)
    .filter(models.App.id == principal.app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.github_access):
    return principal.owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.github_access=true in its manifest "
      "to manage and read the GitHub connection on your behalf."
    ),
  )


def _validate_submit_app(
  app_id: int, principal: Principal, db: Session
) -> str | None:
  """Authorize a direct contribution submit and return the app token nonce."""
  _require_github_access_principal(principal, db)
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="An app can only submit contributions from its own storage.",
    )
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  return app.token_nonce


def _recheck_submit_app(db: Session, app_id: int, expected_nonce: str | None) -> None:
  row = (
    db.query(models.App)
    .populate_existing()
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if row is None or row.token_nonce != expected_nonce:
    raise HTTPException(status_code=404, detail="App not found.")


def _safe_repo_path(raw: object) -> Path:
  if not isinstance(raw, str) or not raw:
    raise ContributionSubmitError(
      "This record needs to be prepared again: it has no durable repo_path."
    )
  try:
    repo = Path(raw).resolve()
  except (OSError, RuntimeError):
    raise ContributionSubmitError("The staged repo path is invalid.")
  data_dir = Path(get_settings().data_dir).resolve()
  # A durable repo must live under one of these roots so a restart can find it
  # again. "contrib" is the de-facto staging root the agent prepares work in
  # (often nested, e.g. contrib/<audit>/<slug>); the plural "contributions" is
  # kept alongside it for back-compat with older docs that named that form.
  allowed_roots = (
    data_dir / "contrib",
    data_dir / "apps",
    data_dir / "platform",
    data_dir / "contributions",
  )
  for root in allowed_roots:
    try:
      repo.relative_to(root)
      return repo
    except ValueError:
      continue
  raise ContributionSubmitError(
    "This prepared PR was staged outside Mobius' durable contribution folders. "
    "Ask the agent to prepare it again from /data/contrib, /data/apps, or "
    "/data/platform; nothing was sent to GitHub."
  )


def _safe_equivalence_source_path(raw: object) -> Path:
  """Installed app/platform repo allowed to own durable provenance refs."""
  repo = _safe_repo_path(raw)
  data_dir = Path(get_settings().data_dir).resolve()
  platform = data_dir / "platform"
  apps = data_dir / "apps"
  if repo != platform and not repo.is_relative_to(apps):
    raise ContributionSubmitError(
      "The contribution source must be an installed app or the live platform."
    )
  if not app_git.is_repo(repo):
    raise ContributionSubmitError(
      "The contribution source is no longer a Git-backed app or platform."
    )
  return repo


def _equivalence_source_repo(record: dict) -> tuple[Path, Path] | None:
  """Return ``(installed source, review checkout)`` for one contribution."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  review_repo = _safe_repo_path(plan.get("repo_path"))
  raw_source_repo = plan.get("source_repo_path")
  if raw_source_repo:
    return _safe_equivalence_source_path(raw_source_repo), review_repo
  primary = app_git.primary_worktree_path(review_repo)
  if primary is not None:
    return _safe_equivalence_source_path(str(primary)), review_repo
  # Legacy prepared records sometimes used the installed source checkout
  # directly rather than a linked worktree. It is already under the stricter
  # apps/platform allowlist, so it can safely own the witness itself.
  try:
    return _safe_equivalence_source_path(str(review_repo)), review_repo
  except ContributionSubmitError:
    return None


def _record_pending_equivalence(record: dict) -> str | None:
  """Persist the reviewed local-history witness after the owner sends a PR.

  Linked review worktrees derive their primary live checkout automatically.
  Standalone app review clones carry an explicit ``plan.source_repo_path``;
  :mod:`app_git` copies only the verified reviewed commits into that installed
  repo before recording the witness. A prepared record should pin
  ``plan.source_sha``; old linked records safely use the primary HEAD observed
  at send time.
  """
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repos = _equivalence_source_repo(record)
  if repos is None:
    return None
  source_repo, review_repo = repos
  plan_base_sha = str(plan.get("base_sha") or "")
  plan_head_sha = str(plan.get("head_sha") or "")
  # Older cleanup could remove a linked review worktree before the merged-state
  # poll created its durable witness. The reviewed commits remain in the live
  # repository, but only reuse it when both immutable commits still resolve.
  if (
    not app_git.ref_exists(review_repo, f"{plan_base_sha}^{{commit}}")
    or not app_git.ref_exists(review_repo, f"{plan_head_sha}^{{commit}}")
  ) and (
    app_git.ref_exists(source_repo, f"{plan_base_sha}^{{commit}}")
    and app_git.ref_exists(source_repo, f"{plan_head_sha}^{{commit}}")
  ):
    review_repo = source_repo
  source_sha = str(plan.get("source_sha") or "").strip()
  current_source = app_git.head_sha(source_repo, "HEAD")
  if not source_sha:
    source_sha = current_source
  elif (
    current_source
    and source_sha != current_source
    and app_git.ref_is_ancestor(source_repo, source_sha, current_source) is not True
  ):
    # The app model may have replayed the accepted tree onto a new upstream
    # parent before Send, so the captured commit is no longer causal history.
    # Use the stable current tip; the exact diff/tree proof below still decides.
    source_sha = current_source
  if not source_sha:
    return None
  kwargs = {
    "base_sha": plan_base_sha,
    "head_sha": plan_head_sha,
    "diff_sha256": str(plan.get("diff_sha256") or ""),
    "contribution_id": str(record.get("id") or ""),
    "review_source_dir": review_repo,
  }
  recorded = app_git.record_pending_equivalent_change(
    source_repo, source_sha=source_sha, **kwargs,
  )
  if recorded is not None:
    return recorded
  # An App Store update can intentionally replay the accepted source tree onto
  # a new upstream parent between preparation and Send. Under the source lock,
  # retry the *current* immutable tip: record_pending repeats the exact diff-hash
  # and tree-subsumption proofs, so this carries a preserved change across that
  # pre-witness rewrite without blessing a source that removed the change.
  if current_source and current_source != source_sha:
    return app_git.record_pending_equivalent_change(
      source_repo, source_sha=current_source, **kwargs,
    )
  return None


async def _record_pending_equivalence_locked(
  record: dict,
  *,
  already_locked: frozenset[str] = frozenset(),
) -> str | None:
  """Serialize witness creation with an App Store source-history replay."""
  repos = await asyncio.to_thread(_equivalence_source_repo, record)
  if repos is None:
    return None
  source_repo, _review_repo = repos
  if str(source_repo) in already_locked:
    return await asyncio.to_thread(_record_pending_equivalence, record)
  async with fs_locks.source_dir_lock(str(source_repo)):
    return await asyncio.to_thread(_record_pending_equivalence, record)


def _merged_upstream_sha(record: dict, repo: Path) -> str | None:
  """Best available immutable upstream commit for a terminal merged record."""
  for value in (
    record.get("last_land_head_sha"),
    record.get("merge_commit_sha"),
    (record.get("checks") or {}).get("merge_commit_sha")
    if isinstance(record.get("checks"), dict) else None,
  ):
    candidate = str(value or "").strip().lower()
    if _GIT_SHA.fullmatch(candidate):
      return candidate

  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repo_slug = plan.get("repo") or record.get("repo")
  number = record.get("number")
  if not isinstance(repo_slug, str) or not _GITHUB_REPO.fullmatch(repo_slug):
    return None
  if not isinstance(number, int) or number <= 0:
    return None
  try:
    proc = _git_ops._gh(
      repo,
      "pr", "view", str(number),
      "-R", repo_slug,
      "--json", "state,mergeCommit",
      check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if proc.returncode != 0:
    return None
  try:
    payload = json.loads(proc.stdout or "{}")
  except ValueError:
    return None
  merge_commit = payload.get("mergeCommit") if isinstance(payload, dict) else None
  candidate = (
    str(merge_commit.get("oid") or "").strip().lower()
    if isinstance(merge_commit, dict) and payload.get("state") == "MERGED"
    else ""
  )
  return candidate if _GIT_SHA.fullmatch(candidate) else None


def _settle_equivalence(record: dict, upstream_sha: str | None = None) -> str | None:
  """Promote or discard the pending witness when GitHub settles the PR."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repos = _equivalence_source_repo(record)
  if repos is None:
    return None
  repo, _review_repo = repos
  digest = str(plan.get("diff_sha256") or "")
  if record.get("status") == "merged":
    equivalent = app_git.mark_equivalent_change_landed(
      repo, digest, upstream_sha=upstream_sha,
    )
    if equivalent is None and _record_pending_equivalence(record) is not None:
      equivalent = app_git.mark_equivalent_change_landed(
        repo, digest, upstream_sha=upstream_sha,
      )
    return equivalent
  if record.get("status") == "closed":
    app_git.discard_pending_equivalent_change(repo, digest)
  return None


def _cleanup_terminal_staging_checkout(record: dict) -> bool:
  """Remove a terminal contribution checkout through its owning Git shape."""
  if record.get("status") not in {
    "merged", "closed", "superseded", "commented", "abandoned",
  }:
    return False
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repo = _safe_repo_path(plan.get("repo_path"))
  data_dir = Path(get_settings().data_dir).resolve()
  roots = (data_dir / "contrib", data_dir / "contributions")
  if not any(repo.is_relative_to(root) for root in roots):
    return False
  marker = repo / ".git"
  if not marker.exists() or marker.is_symlink():
    return False

  # A linked worktree has a .git *file*. Deleting its directory directly
  # strands the registration and branch lock in the source repository. Ask Git
  # to remove it instead, using the common directory as the stable owner even
  # while the checkout itself disappears.
  if marker.is_file():
    env = dict(os.environ)
    for name in (
      "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
      "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    ):
      env.pop(name, None)
    probe = subprocess.run(
      [
        "git", "-C", str(repo), "rev-parse", "--path-format=absolute",
        "--git-dir", "--git-common-dir",
      ],
      cwd=str(repo.parent),
      capture_output=True,
      text=True,
      check=False,
      env=env,
    )
    paths = [Path(line).resolve() for line in probe.stdout.splitlines() if line]
    if probe.returncode != 0 or len(paths) != 2:
      return False
    git_dir, common_dir = paths
    if not common_dir.is_relative_to(data_dir):
      return False

    if git_dir != common_dir:
      removed = subprocess.run(
        [
          "git", f"--git-dir={common_dir}",
          "worktree", "remove", "--force", str(repo),
        ],
        cwd=str(data_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
      )
      if removed.returncode != 0:
        raise ContributionSubmitError(
          "Could not clear the old review checkout for this contribution.",
          detail=readable_output(
            removed.stderr or removed.stdout or "git worktree remove failed",
          ),
        )
      subprocess.run(
        ["git", f"--git-dir={common_dir}", "worktree", "prune"],
        cwd=str(data_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
      )
      return True

    # Manifest-installed apps use `--separate-git-dir=<record-root>/git`.
    # That is a main checkout rather than a linked worktree, so remove both
    # halves only when the git directory is the documented sibling.
    shutil.rmtree(repo)
    if common_dir.name == "git" and common_dir.parent == repo.parent:
      shutil.rmtree(common_dir, ignore_errors=True)
    return True

  # Ordinary standalone clones keep a real .git directory inside the checkout.
  shutil.rmtree(repo)
  return True


def _claim_record(
  *, app_id: int, record_id: str, db: Session, expected_nonce: str | None,
  submitter: str = "contribute-button",
) -> tuple[dict, Path, Path]:
  record_path, diff_path = _record_paths(app_id, record_id)
  _recheck_submit_app(db, app_id, expected_nonce)
  record = _read_record(record_path)
  if record.get("status") != "prepared":
    raise HTTPException(
      status_code=409,
      detail="This contribution is no longer waiting for approval.",
    )
  pre_pr_checks = record.get("pre_pr_checks")
  if (
    isinstance(pre_pr_checks, dict)
    and pre_pr_checks.get("state") in _PRE_PR_CHECK_ACTIVE_STATES
  ):
    raise HTTPException(
      status_code=409,
      detail=(
        "GitHub checks are still starting or running for this review. Wait "
        "for them to finish before opening the pull request."
      ),
    )
  plan = record.get("plan")
  if not isinstance(plan, dict):
    raise HTTPException(
      status_code=409,
      detail="This older contribution needs agent review before it can submit.",
    )
  if plan.get("action") != "pr" or record.get("type") != "pr":
    raise HTTPException(
      status_code=400,
      detail="Direct approval currently supports pull requests.",
    )
  if isinstance(plan.get("stack"), dict):
    raise HTTPException(
      status_code=409,
      detail=(
        "This contribution belongs to a PR stack. Review and send the complete "
        "chain together."
      ),
    )
  now = _now_iso()
  claimed = {
    **record,
    "status": "submitting",
    "submitter": submitter,
    "submit_started_at": now,
    "updated_at": now,
  }
  _write_record(record_path, claimed)
  return claimed, record_path, diff_path


def _stack_meta(record: dict) -> dict:
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  stack = plan.get("stack") if isinstance(plan.get("stack"), dict) else {}
  stack_id = str(stack.get("id") or "").strip()
  if not _CONTRIBUTION_ID.match(stack_id):
    raise ContributionSubmitError(
      "This PR stack has an invalid stack id. Leave feedback so your agent "
      "can prepare it again."
    )
  try:
    position = int(stack.get("position"))
    total = int(stack.get("total"))
  except (TypeError, ValueError):
    raise ContributionSubmitError(
      "This PR stack is missing its layer positions. Leave feedback so your "
      "agent can prepare it again."
    ) from None
  if total < 2 or total > 12 or position < 1 or position > total:
    raise ContributionSubmitError(
      "A PR stack must contain between 2 and 12 ordered layers."
    )
  base_branch = _git_ops._validate_branch(stack.get("base_branch"))
  parent_record_id = str(stack.get("parent_record_id") or "").strip()
  if parent_record_id and not _CONTRIBUTION_ID.match(parent_record_id):
    raise ContributionSubmitError("This PR stack has an invalid parent record.")
  return {
    **stack,
    "id": stack_id,
    "position": position,
    "total": total,
    "base_branch": base_branch,
    "parent_record_id": parent_record_id,
  }


def _validate_stack_records(records: list[dict]) -> list[dict]:
  """Validate one complete, immutable parent-to-child contribution chain."""
  if not records:
    raise ContributionSubmitError("This PR stack has no reviewed records.")
  decorated = [(record, _stack_meta(record)) for record in records]
  decorated.sort(key=lambda item: item[1]["position"])
  first_stack = decorated[0][1]
  total = first_stack["total"]
  stack_id = first_stack["id"]
  if len(decorated) != total:
    raise ContributionSubmitError(
      "This PR stack is incomplete. Review every layer together before "
      "sending it."
    )
  if [meta["position"] for _, meta in decorated] != list(range(1, total + 1)):
    raise ContributionSubmitError("This PR stack has duplicate or missing layers.")

  repo = None
  branches = set()
  previous_record = None
  previous_plan = None
  # A draft PR is already public and owner-approved; it is a valid durable
  # parent for a later private layer just like an open PR. `prepared` remains
  # the only private state this request is allowed to claim.
  allowed_statuses = {"prepared", "submitting", "draft", "open", "landing", "merged"}
  for record, meta in decorated:
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
    record_id = str(record.get("id") or "")
    record_repo = _git_ops._validate_repo_slug(plan.get("repo") or record.get("repo"))
    branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
    prefix = f"stack/{stack_id}/"
    if not branch.startswith(prefix):
      raise ContributionSubmitError(
        f"Every branch in this stack must start with {prefix}."
      )
    if branch in branches:
      raise ContributionSubmitError("Every PR stack layer needs a unique branch.")
    branches.add(branch)
    if meta["id"] != stack_id or meta["total"] != total:
      raise ContributionSubmitError("These records do not describe one PR stack.")
    if record.get("type") != "pr" or plan.get("action") != "pr":
      raise ContributionSubmitError("PR stacks can contain pull requests only.")
    if record.get("status") not in allowed_statuses:
      raise ContributionSubmitError(
        "Every stack layer must be ready, draft, open, or already merged."
      )
    if repo is None:
      repo = record_repo
    elif record_repo != repo:
      raise ContributionSubmitError("Every layer in a PR stack must target one repository.")

    if previous_record is None:
      if meta["parent_record_id"]:
        raise ContributionSubmitError("The first stack layer cannot have a parent PR.")
    else:
      if meta["parent_record_id"] != str(previous_record.get("id") or ""):
        raise ContributionSubmitError("A PR stack layer points at the wrong parent record.")
      previous_branch = _git_ops._validate_branch(
        previous_plan.get("branch") or previous_record.get("branch")
      )
      if meta["base_branch"] != previous_branch:
        raise ContributionSubmitError("A PR stack layer points at the wrong base branch.")
      # GitHub may retarget/rebase an already-public child after its parent
      # merges. Preserve that settled history in the stack, but require exact
      # reviewed ancestry at every still-private edge.
      if (
        record.get("status") == "prepared" and
        str(plan.get("base_sha") or "") != str(previous_plan.get("head_sha") or "")
      ):
        raise ContributionSubmitError(
          "A PR stack layer is not based on its reviewed parent commit."
        )
    previous_record = record
    previous_plan = plan

  # Keep the validated metadata beside each record for callers without
  # changing the stored ledger shape.
  return [{"record": record, "stack": meta} for record, meta in decorated]


def _claim_stack_records(
  *,
  app_id: int,
  record_ids: list[str],
  db: Session,
  expected_nonce: str | None,
) -> list[dict]:
  if not 2 <= len(record_ids) <= 12 or len(set(record_ids)) != len(record_ids):
    raise HTTPException(
      status_code=400,
      detail="Choose one complete PR stack of 2 to 12 unique records.",
    )
  _recheck_submit_app(db, app_id, expected_nonce)
  rows = []
  for record_id in record_ids:
    record_path, diff_path = _record_paths(app_id, record_id)
    record = _read_record(record_path)
    if str(record.get("id") or "") != record_id:
      raise HTTPException(status_code=409, detail="A stack record id changed.")
    rows.append({
      "record": record,
      "record_path": record_path,
      "diff_path": diff_path,
    })
  try:
    validated = _validate_stack_records([row["record"] for row in rows])
  except ContributionSubmitError as exc:
    raise HTTPException(status_code=409, detail=exc.message) from exc
  by_id = {row["record"]["id"]: row for row in rows}
  ordered = []
  now = _now_iso()
  for item in validated:
    row = by_id[item["record"]["id"]]
    record = row["record"]
    if record.get("status") == "prepared":
      record = {
        **record,
        "status": "submitting",
        "submitter": "contribute-stack-button",
        "submit_started_at": now,
        "updated_at": now,
      }
      _write_record(row["record_path"], record)
    ordered.append({**row, "record": record, "stack": item["stack"]})
  if not any(row["record"].get("status") == "submitting" for row in ordered):
    raise HTTPException(
      status_code=409,
      detail="Every PR in this stack has already been submitted.",
    )
  return ordered


def _claim_stack_landing(
  *,
  app_id: int,
  record_ids: list[str],
  db: Session,
  expected_nonce: str | None,
) -> tuple[list[dict], str]:
  """Claim a new landing or reopen its durable journal for reconciliation."""
  if not 2 <= len(record_ids) <= 12 or len(set(record_ids)) != len(record_ids):
    raise HTTPException(
      status_code=400,
      detail="Choose one complete PR stack of 2 to 12 unique records.",
    )
  _recheck_submit_app(db, app_id, expected_nonce)
  rows = []
  for record_id in record_ids:
    record_path, diff_path = _record_paths(app_id, record_id)
    record = _read_record(record_path)
    if str(record.get("id") or "") != record_id:
      raise HTTPException(status_code=409, detail="A stack record id changed.")
    rows.append({
      "record": record,
      "record_path": record_path,
      "diff_path": diff_path,
    })
  try:
    validated = _validate_stack_records([row["record"] for row in rows])
  except ContributionSubmitError as exc:
    raise HTTPException(status_code=409, detail=exc.message) from exc
  statuses = {item["record"].get("status") for item in validated}
  by_id = {row["record"]["id"]: row for row in rows}
  ordered = [
    {**by_id[item["record"]["id"]], "stack": item["stack"]}
    for item in validated
  ]
  if statuses == {"merged"}:
    targets = {
      (
        item["record"].get("last_land_target_branch"),
        item["record"].get("last_land_head_sha"),
      )
      for item in validated
    }
    if (
      any(
        item["record"].get("last_land_mode") != "atomic-fast-forward"
        for item in validated
      )
      or len(targets) != 1
    ):
      raise HTTPException(
        status_code=409,
        detail="This pull request stack is already settled.",
      )
    return ordered, "merged"
  if statuses.issubset({"landing", "merged"}) and "landing" in statuses:
    # Marking a successful multi-record landing is intentionally idempotent but
    # cannot be one filesystem transaction. A process exit between record
    # writes leaves a truthful merged/landing mix; keep reconciling the shared
    # pre-push journal instead of stranding the stack.
    return ordered, "recover"
  if statuses.issubset({"open", "landing"}) and "landing" in statuses:
    # The same partial-write window exists while first claiming the stack or
    # reopening it after a proven pre-push failure. Complete the saved claim,
    # then let upstream-ref reconciliation decide whether to settle or reopen.
    # The journal values must still match the reviewed chain exactly.
    target_branch = validated[0]["stack"]["base_branch"]
    expected_base = str(
      (validated[0]["record"].get("plan") or {}).get("base_sha") or ""
    )
    landed_sha = str(
      (validated[-1]["record"].get("plan") or {}).get("head_sha") or ""
    )
    expected_journal = (target_branch, expected_base, landed_sha)
    if (
      not _GIT_SHA.match(expected_base)
      or not _GIT_SHA.match(landed_sha)
      or any(
        item["record"].get("status") == "landing"
        and (
          str(item["record"].get("land_target_branch") or ""),
          str(item["record"].get("land_expected_base_sha") or ""),
          str(item["record"].get("land_head_sha") or ""),
        ) != expected_journal
        for item in validated
      )
    ):
      raise HTTPException(
        status_code=409,
        detail=(
          "This stack has a partial landing journal that no longer matches "
          "the reviewed chain. Refresh Contribute before trying again."
        ),
      )
    started_at = next(
      (
        str(item["record"].get("land_started_at"))
        for item in validated
        if item["record"].get("land_started_at")
      ),
      _now_iso(),
    )
    now = _now_iso()
    repaired = []
    for row in ordered:
      record = {
        **row["record"],
        "status": "landing",
        "land_started_at": started_at,
        "land_target_branch": target_branch,
        "land_expected_base_sha": expected_base,
        "land_head_sha": landed_sha,
        "updated_at": now,
      }
      record.pop("last_land_error", None)
      _write_record(row["record_path"], record)
      repaired.append({**row, "record": record})
    return repaired, "recover"
  if statuses != {"open"}:
    raise HTTPException(
      status_code=409,
      detail=(
        "Every pull request in this stack must be open before it can land. "
        "Refresh Contribute and try again."
      ),
    )

  claimed = []
  now = _now_iso()
  target_branch = validated[0]["stack"]["base_branch"]
  expected_base = str((validated[0]["record"].get("plan") or {}).get("base_sha") or "")
  landed_sha = str((validated[-1]["record"].get("plan") or {}).get("head_sha") or "")
  if not _GIT_SHA.match(expected_base) or not _GIT_SHA.match(landed_sha):
    raise HTTPException(
      status_code=409,
      detail="This stack has no complete reviewed landing journal. Prepare it again.",
    )
  for row in ordered:
    record = {
      **row["record"],
      "status": "landing",
      "land_started_at": now,
      "land_target_branch": target_branch,
      "land_expected_base_sha": expected_base,
      "land_head_sha": landed_sha,
      "updated_at": now,
    }
    record.pop("last_land_error", None)
    _write_record(row["record_path"], record)
    claimed.append({**row, "record": record})
  return claimed, "new"


def _landing_journal(rows: list[dict]) -> tuple[str, str, str]:
  """Return one consistent durable landing intent from claimed records."""
  journals = {
    (
      str(row["record"].get("land_target_branch") or ""),
      str(row["record"].get("land_expected_base_sha") or ""),
      str(row["record"].get("land_head_sha") or ""),
    )
    for row in rows
  }
  if len(journals) != 1:
    raise ContributionSubmitError(
      "This landing journal is incomplete. Nothing new was pushed; refresh Contribute."
    )
  target_branch, expected_base, landed_sha = journals.pop()
  if (
    not target_branch
    or not _GIT_SHA.match(expected_base)
    or not _GIT_SHA.match(landed_sha)
  ):
    raise ContributionSubmitError(
      "This landing journal is invalid. Nothing new was pushed; refresh Contribute."
    )
  return _git_ops._validate_branch(target_branch), expected_base, landed_sha


def _reconcile_stack_landing(rows: list[dict]) -> tuple[str, str]:
  """Resolve a previously claimed landing without ever issuing another push."""
  currents = [_read_record(row["record_path"]) for row in rows]
  if all(current.get("status") == "merged" for current in currents):
    target = str(currents[0].get("last_land_target_branch") or "")
    head = str(currents[0].get("last_land_head_sha") or "")
    return _git_ops._validate_branch(target), head
  if any(current.get("status") not in {"landing", "merged"} for current in currents):
    raise ContributionSubmitError(
      "This landing changed while it was being recovered. Refresh Contribute."
    )
  live_rows = [
    {**row, "record": current}
    for row, current in zip(rows, currents, strict=True)
  ]
  target_branch, expected_base, landed_sha = _landing_journal(live_rows)
  first_plan = live_rows[0]["record"].get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(
    first_plan.get("repo") or live_rows[0]["record"].get("repo")
  )
  repo = _safe_repo_path(first_plan.get("repo_path"))
  actual = _git_ops._upstream_branch_sha(repo, upstream_repo, target_branch)
  if actual == landed_sha:
    return target_branch, landed_sha
  if actual == expected_base:
    raise ContributionSubmitError(
      "The earlier landing stopped before changing upstream. The stack is open again."
    )
  if actual is None:
    raise ContributionSubmitError(
      "GitHub has not yet confirmed whether the saved landing completed. "
      "The recovery journal is still intact; check again shortly.",
      status_code=503,
      code="landing_unconfirmed",
    )
  raise ContributionSubmitError(
    f"Upstream {target_branch} changed while the earlier landing was unresolved. "
    "Nothing was overwritten; refresh the stack before trying again."
  )


def _mark_stack_land_failure(rows: list[dict], message: str) -> list[dict]:
  snapshots = []
  now = _now_iso()
  for row in rows:
    current = _read_record(row["record_path"])
    if current.get("status") == "landing":
      current = {
        **current,
        "status": "open",
        "last_land_error": message,
        "updated_at": now,
      }
      _write_record(row["record_path"], current)
    snapshots.append(current)
  return snapshots


def _mark_stack_land_success(
  rows: list[dict], *, target_branch: str, landed_sha: str,
) -> list[dict]:
  currents = [_read_record(row["record_path"]) for row in rows]
  settled = [
    current.get("status") == "merged"
    and current.get("last_land_target_branch") == target_branch
    and current.get("last_land_head_sha") == landed_sha
    for current in currents
  ]
  if all(settled):
    return currents
  if any(
    current.get("status") != "landing" and not is_settled
    for current, is_settled in zip(currents, settled, strict=True)
  ):
    raise ContributionSubmitError(
      "This PR stack changed while it was landing. Refresh Contribute."
    )
  snapshots = []
  now = _now_iso()
  for row, current, is_settled in zip(rows, currents, settled, strict=True):
    if is_settled:
      snapshots.append(current)
      continue
    current = {
      **current,
      "status": "merged",
      "merged_at": now,
      "landed_at": now,
      "last_land_mode": "atomic-fast-forward",
      "last_land_target_branch": target_branch,
      "last_land_head_sha": landed_sha,
      "updated_at": now,
    }
    current.pop("last_land_error", None)
    _write_record(row["record_path"], current)
    snapshots.append(current)
  return snapshots


def _mark_submit_failure(
  *,
  app_id: int,
  record_path: Path,
  message: str,
  record_patch: dict | None = None,
  detail: str = "",
) -> dict | None:
  try:
    record = _read_record(record_path)
  except HTTPException:
    return None
  if record.get("status") != "submitting":
    return record
  next_record = {
    **record,
    **(record_patch or {}),
    "status": "prepared",
    "last_submit_error": message,
    "updated_at": _now_iso(),
  }
  # The diagnostic belongs to exactly one attempt. Carrying a previous
  # transcript next to a new message would explain the wrong failure.
  if detail:
    next_record["last_submit_error_detail"] = detail
  else:
    next_record.pop("last_submit_error_detail", None)
  _write_record(record_path, next_record)
  return next_record


def _mark_submit_success(
  *,
  record_path: Path,
  record: dict,
  pr_url: str,
  number: int | None,
  record_patch: dict | None = None,
) -> dict:
  now = _now_iso()
  next_record = {
    **record,
    **(record_patch or {}),
    "status": "open",
    "url": pr_url,
    "updated_at": now,
    "submitted_at": now,
  }
  if number is not None:
    next_record["number"] = number
  next_record.pop("last_submit_error", None)
  next_record.pop("last_submit_error_detail", None)
  _write_record(record_path, next_record)
  return next_record


def _mark_stack_submit_failure(
  rows: list[dict],
  message: str,
  *,
  failed_id: str | None = None,
  record_patch: dict | None = None,
  detail: str = "",
) -> list[dict]:
  snapshots = []
  for row in rows:
    current = _read_record(row["record_path"])
    if current.get("status") == "submitting":
      is_failed = current.get("id") == failed_id
      patch = record_patch if is_failed else None
      current = _mark_submit_failure(
        app_id=0,
        record_path=row["record_path"],
        message=message,
        record_patch=patch,
        # Only the layer that actually failed owns the transcript; the
        # siblings were stopped, not rejected.
        detail=detail if is_failed else "",
      ) or current
    snapshots.append(current)
  return snapshots


def _stack_record_snapshots(rows: list[dict]) -> list[dict]:
  return [_read_record(row["record_path"]) for row in rows]


def _parse_pr_number(url: str) -> int | None:
  m = re.search(r"/pull/(\d+)(?:$|[/?#])", url)
  return int(m.group(1)) if m else None


def _reviewed_pr_labels(plan: dict) -> list[str]:
  """Return only the two labels the owner could see in Contribute review."""
  raw = plan.get("labels")
  if not isinstance(raw, list):
    return []
  # Mirror Contribute's review surface: it filters malformed/blank values,
  # trims them, and then shows at most two. Security validation and duplicate
  # folding happen only after that visibility boundary, so an unseen third
  # label can never replace a visible-but-unusable one at submit time.
  visible = []
  for value in raw:
    if not isinstance(value, str):
      continue
    label = value.strip()
    if not label:
      continue
    visible.append(label)
    if len(visible) == 2:
      break
  labels = []
  seen = set()
  for label in visible:
    folded = label.casefold()
    if len(label) > 50 or "\n" in label or folded in seen:
      continue
    seen.add(folded)
    labels.append(label)
  return labels


def _apply_reviewed_pr_labels(
  repo: Path,
  upstream_repo: str,
  number: int | None,
  labels: list[str],
) -> dict:
  """Best-effort add reviewed labels that already exist in the target repo.

  Labeling is deliberately secondary to PR creation: a missing repository
  label, permission restriction, or transient API failure must not turn an
  already-open pull request into an apparent failed submission. The outcome is
  persisted so the review never claims an unavailable label was applied.
  """
  if not labels:
    return {}
  patch = {
    "last_submit_labels_requested": labels,
    "last_submit_labels_applied": [],
  }
  if number is None:
    return {
      **patch,
      "last_submit_labels_note": "GitHub did not return a PR number for labeling.",
    }

  try:
    available = _git_ops._gh(
      repo,
      "api", "--paginate",
      f"repos/{upstream_repo}/labels?per_page=100",
      "--jq", ".[].name",
      check=False,
    )
  except subprocess.TimeoutExpired:
    return {
      **patch,
      "last_submit_labels_note": (
        "Timed out while checking repository labels; the pull request is "
        "open without confirmed labels."
      ),
    }
  except OSError:
    return {
      **patch,
      "last_submit_labels_note": (
        "Could not start the GitHub label lookup; the pull request is open "
        "without confirmed labels."
      ),
    }
  if available.returncode != 0:
    return {
      **patch,
      "last_submit_labels_note": (
        "Could not verify the repository labels; the pull request is open "
        "without confirmed labels."
      ),
    }
  by_name = {}
  for raw_name in (available.stdout or "").splitlines():
    name = raw_name.strip()
    if name:
      by_name[name.casefold()] = name
  applicable = [by_name[label.casefold()] for label in labels
                if label.casefold() in by_name]
  missing = [label for label in labels if label.casefold() not in by_name]
  if not applicable:
    return {
      **patch,
      "last_submit_labels_missing": missing,
      "last_submit_labels_note": "The reviewed labels do not exist in this repository.",
    }

  try:
    applied = _git_ops._gh(
      repo,
      "api", "--method", "POST",
      f"repos/{upstream_repo}/issues/{number}/labels",
      *(part for label in applicable for part in ("-f", f"labels[]={label}")),
      check=False,
    )
  except subprocess.TimeoutExpired:
    return {
      **patch,
      "last_submit_labels_missing": missing,
      "last_submit_labels_note": (
        "Timed out while applying reviewed labels; the pull request is open, "
        "but GitHub did not confirm the label result."
      ),
    }
  except OSError:
    return {
      **patch,
      "last_submit_labels_missing": missing,
      "last_submit_labels_note": (
        "Could not start the GitHub label update; the pull request is open "
        "without confirmed labels."
      ),
    }
  if applied.returncode != 0:
    return {
      **patch,
      "last_submit_labels_missing": missing,
      "last_submit_labels_note": (
        "GitHub did not confirm these labels were applied; the pull request "
        "is still open."
      ),
    }
  result = {
    **patch,
    "last_submit_labels_applied": applicable,
  }
  if missing:
    result["last_submit_labels_missing"] = missing
    result["last_submit_labels_note"] = "Some reviewed labels no longer exist."
  return result


def _find_existing_pr(
  repo: Path,
  upstream_repo: str,
  login: str,
  branch: str,
  *,
  expected_head_sha: str,
  base_branch: str | None = None,
  same_repo: bool = False,
) -> str | None:
  if not _GIT_SHA.match(str(expected_head_sha or "")):
    return None
  # `gh pr create --head` accepts owner:branch for a fork, but `gh pr list
  # --head` only matches the branch name. Passing owner:branch here returns an
  # empty list even when GitHub's create response says that exact PR already
  # exists. Query by branch, then prove the expected repository owner and
  # pushed commit from the returned metadata.
  expected_owner = upstream_repo.split("/", 1)[0] if same_repo else login
  args = [
    "pr", "list",
    "-R", upstream_repo,
    "--head", branch,
  ]
  if base_branch:
    args.extend(("--base", _git_ops._validate_branch(base_branch)))
  args.extend((
    "--state", "open",
    "--json", "url,headRefName,headRefOid,headRepositoryOwner",
    "--limit", "10",
  ))
  try:
    proc = _git_ops._gh(
      repo,
      *args,
      check=False,
    )
  except (subprocess.TimeoutExpired, OSError):
    return None
  if proc.returncode != 0:
    return None
  try:
    rows = json.loads(proc.stdout or "[]")
  except ValueError:
    return None
  if isinstance(rows, list):
    for row in rows:
      if not isinstance(row, dict):
        continue
      owner = row.get("headRepositoryOwner")
      owner_login = owner.get("login") if isinstance(owner, dict) else ""
      if str(owner_login or "").casefold() != expected_owner.casefold():
        continue
      if str(row.get("headRefName") or "") != branch:
        continue
      if str(row.get("headRefOid") or "") != expected_head_sha:
        continue
      url = row.get("url")
      if isinstance(url, str) and url.startswith("https://github.com/"):
        return url
  return None


def _existing_branch_pr(
  repo: Path,
  upstream_repo: str,
  login: str,
  branch: str,
  *,
  same_repo: bool = False,
) -> tuple[str, str] | None:
  """Truth check before first publication: this branch's existing OPEN or
  MERGED pull request in the upstream repo, as ``(url, state)``, else None.

  The submit preflights above prove WHAT would be sent (the exact reviewed
  diff); only the agent-writable ledger row claims the record was never sent
  before — and that row can lie (field incident 2026-07-29: a freshly-merged
  PR's record was rewritten back to ``prepared`` by a stale re-stage, so one
  more Send would have force-pushed the merged branch and opened a duplicate
  PR). GitHub is the one store that cannot drift from the public truth, so
  ask it directly. Fail CLOSED: a lookup that cannot complete raises instead
  of letting the send proceed blind — the send needs GitHub reachable anyway.
  A PR closed WITHOUT merging is not returned: rework-and-resend of a
  rejected branch stays legitimate. An open PR outranks a merged one in the
  report so the message names the row that would collide first.
  """
  could_not_verify = (
    "Could not verify whether this branch already has a pull request. "
    "Nothing was pushed; try again once GitHub is reachable."
  )
  expected_owner = upstream_repo.split("/", 1)[0] if same_repo else login
  try:
    proc = _git_ops._gh(
      repo,
      "pr", "list",
      "-R", upstream_repo,
      "--head", branch,
      "--state", "all",
      "--json", "url,state,headRefName,headRepositoryOwner",
      "--limit", "20",
      check=False,
    )
  except (subprocess.TimeoutExpired, OSError) as exc:
    raise ContributionSubmitError(could_not_verify) from exc
  if proc.returncode != 0:
    raise ContributionSubmitError(
      could_not_verify,
      detail=readable_output(proc.stderr or proc.stdout or ""),
    )
  try:
    rows = json.loads(proc.stdout or "[]")
  except ValueError:
    raise ContributionSubmitError(could_not_verify) from None
  merged: tuple[str, str] | None = None
  if isinstance(rows, list):
    for row in rows:
      if not isinstance(row, dict):
        continue
      owner = row.get("headRepositoryOwner")
      owner_login = owner.get("login") if isinstance(owner, dict) else ""
      if str(owner_login or "").casefold() != expected_owner.casefold():
        continue
      if str(row.get("headRefName") or "") != branch:
        continue
      url = row.get("url")
      if not (isinstance(url, str) and url.startswith("https://github.com/")):
        continue
      state_label = str(row.get("state") or "").upper()
      if state_label == "OPEN":
        return (url, "open")
      if state_label == "MERGED" and merged is None:
        merged = (url, "merged")
  return merged


def _is_workflow_scope_push_error(message: str) -> bool:
  """Recognize GitHub's stable OAuth workflow-scope rejection."""
  detail = str(message or "").lower()
  return (
    "workflow" in detail
    and (
      "refusing to allow" in detail
      or ".github/workflows" in detail
      or "oauth app" in detail
    )
  )


def _is_transient_push_error(message: str) -> bool:
  """Retry transport/server failures, never deterministic push rejections."""
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


def _push_branch(
  repo: Path,
  remote: str,
  branch: str,
  source: str = "HEAD",
) -> str | None:
  """Push once on deterministic failures; briefly retry transient failures."""
  last_error = ""
  for attempt in range(_PUSH_RETRIES):
    proc = _git_ops._git(
      repo, "push", remote, f"{source}:refs/heads/{branch}", check=False,
    )
    if proc.returncode == 0:
      return None
    last_error = (proc.stderr or proc.stdout or "").strip()
    if not _is_transient_push_error(last_error):
      break
    if attempt + 1 < _PUSH_RETRIES:
      time.sleep(_PUSH_RETRY_BASE_SECONDS * (2 ** attempt))
  return last_error or "Git push failed."


def _push_topic_branch(repo: Path, branch: str, source: str = "HEAD") -> str | None:
  """Push a reviewed topic to the owner's configured fork remote."""
  return _push_branch(repo, "fork", branch, source)


def _github_remote_slug(remote_url: str) -> str | None:
  """Return owner/repo for GitHub remotes we can verify."""
  raw = str(remote_url or "").strip()
  if raw.startswith("git@github.com:"):
    path = raw.removeprefix("git@github.com:")
  else:
    parsed = urlparse(raw)
    if (parsed.hostname or "").lower() != "github.com":
      return None
    path = parsed.path.lstrip("/")
  path = path.removesuffix(".git").strip("/")
  parts = path.split("/")
  if len(parts) != 2 or not parts[0] or not parts[1]:
    return None
  return f"{parts[0]}/{parts[1]}"


def _ensure_owner_fork_remote(repo: Path, upstream_repo: str, login: str) -> str:
  """Make local remote `fork` point at the approving owner's fork."""
  existing = _git_ops._git(repo, "remote", "get-url", "fork", check=False)
  if existing.returncode == 0:
    actual_slug = _github_remote_slug(existing.stdout)
    if actual_slug and actual_slug.split("/", 1)[0].lower() == login.lower():
      return actual_slug
    # The staged contribution checkout is disposable. Replacing a stale
    # remote is safer than pushing reviewed code to an ambient `fork` URL.
    _git_ops._git(repo, "remote", "remove", "fork", check=False)

  origin = _git_ops._git(repo, "remote", "get-url", "origin", check=False)
  origin_slug = (
    _github_remote_slug(origin.stdout) if origin.returncode == 0 else None
  )
  if not origin_slug or origin_slug.lower() != upstream_repo.lower():
    _git_ops._git(
      repo,
      "remote", "set-url" if origin.returncode == 0 else "add",
      "origin", f"https://github.com/{upstream_repo}.git",
    )

  # gh 2.96 rejects --remote with a repository argument; origin selects
  # the upstream repo for the in-repo fork command.
  _git_ops._gh(repo, "repo", "fork", "--remote", "--remote-name", "fork")
  final = _git_ops._git(repo, "remote", "get-url", "fork", check=False)
  final_slug = _github_remote_slug(final.stdout) if final.returncode == 0 else None
  if not final_slug or final_slug.split("/", 1)[0].lower() != login.lower():
    raise ContributionSubmitError(
      "Could not verify the fork remote for this GitHub account. "
      "Reconnect GitHub or ask the agent to prepare the contribution again."
    )
  return final_slug


def _inspect_owner_fork_default_branch(
  repo: Path,
  fork_slug: str,
  *,
  upstream_branch: str,
  upstream_sha: str,
) -> dict:
  """Inspect a reusable PR fork without mutating its default branch.

  GitHub rejects an OAuth push that would introduce a new or changed Actions
  workflow to a repository unless the token also has the broad `workflow`
  scope. The same restriction applies to GitHub's merge-upstream endpoint, so
  a public_repo-only connection cannot refresh a stale fork that crossed a
  workflow change. Instead, a strictly-behind fork is handled by preparing the
  reviewed change on its existing tip; the fork's default branch stays put.

  A current fork (or one containing upstream) can receive the reviewed branch
  normally. A diverged default branch is left untouched and stops submission.
  """
  upstream_branch = _git_ops._validate_branch(upstream_branch)
  if not _GIT_SHA.match(str(upstream_sha or "")):
    raise ContributionSubmitError(
      "Could not resolve the upstream tip before inspecting the PR fork."
    )
  fork_branch = _git_ops._upstream_default_branch(repo, fork_slug)
  fork_url = f"https://github.com/{fork_slug}.git"
  ref_key = hashlib.sha256(
    f"{fork_slug}\0{fork_branch}\0{time.time_ns()}".encode("utf-8")
  ).hexdigest()[:24]
  fork_ref = f"refs/mobius-submit/fork-{ref_key}"
  fork_heads_prefix = f"refs/mobius-submit/fork-heads-{ref_key}"
  patch = {
    "last_submit_fork_branch": fork_branch,
    "last_submit_upstream_branch": upstream_branch,
  }

  def fetch_fork_tip() -> str:
    fetched = _git_ops._git(
      repo,
      "fetch", "--no-tags", "--force",
      fork_url,
      f"+refs/heads/{fork_branch}:{fork_ref}",
      check=False,
    )
    if fetched.returncode != 0:
      raise ContributionSubmitError(
        "Could not inspect the GitHub fork before pushing this PR. Try Send "
        "again, or leave feedback if it keeps failing.",
        record_patch=patch,
      ) from None
    fork_sha = _git_ops._git(
      repo, "rev-parse", "--verify", f"{fork_ref}^{{commit}}",
    ).stdout.strip()
    if not _GIT_SHA.match(fork_sha):
      raise ContributionSubmitError(
        "Could not resolve the GitHub fork's default branch before pushing.",
        record_patch=patch,
      )
    return fork_sha

  def is_ancestor(older: str, newer: str) -> bool:
    result = _git_ops._git(
      repo, "merge-base", "--is-ancestor", older, newer, check=False,
    )
    if result.returncode not in (0, 1):
      raise ContributionSubmitError(
        "Could not compare the GitHub fork with current upstream.",
        record_patch=patch,
      )
    return result.returncode == 0

  try:
    fork_sha = fetch_fork_tip()
    patch["last_submit_fork_sha"] = fork_sha
    if fork_sha == upstream_sha:
      return {**patch, "last_submit_fork_sync": "current"}
    if is_ancestor(upstream_sha, fork_sha):
      return {**patch, "last_submit_fork_sync": "contains-upstream"}
    if not is_ancestor(fork_sha, upstream_sha):
      raise ContributionSubmitError(
        f"Your PR fork's {fork_branch} branch has diverged from upstream, so "
        "Contribute left it untouched. Review that fork on GitHub or leave "
        "feedback for your agent before trying again.",
        record_patch={**patch, "last_submit_fork_sync": "diverged"},
      )

    # GitHub's own "Update branch" action can merge current upstream into a
    # topic branch while leaving the reusable fork's default branch stale. In
    # that case the workflow-bearing upstream commits already exist in the
    # fork, so a public_repo-only token may safely create another reviewed
    # topic ref without introducing workflow history. Discover that carrier
    # branch before asking for broader workflow access.
    fetched_heads = _git_ops._git(
      repo,
      "fetch", "--no-tags", "--force",
      fork_url,
      f"+refs/heads/*:{fork_heads_prefix}/*",
      check=False,
    )
    if fetched_heads.returncode == 0:
      refs = _git_ops._git(
        repo,
        "for-each-ref", "--format=%(refname)%00%(objectname)",
        f"{fork_heads_prefix}/",
      ).stdout.splitlines()
      for row in refs:
        ref, separator, tip = row.partition("\0")
        if not separator or not _GIT_SHA.match(tip):
          continue
        if is_ancestor(upstream_sha, tip):
          carrier = ref.removeprefix(f"{fork_heads_prefix}/")
          return {
            **patch,
            "last_submit_fork_sync": "contains-upstream",
            "last_submit_fork_carrier_branch": carrier,
            "last_submit_fork_carrier_sha": tip,
          }
    return {**patch, "last_submit_fork_sync": "strictly-behind"}
  finally:
    _git_ops._git(repo, "update-ref", "-d", fork_ref, check=False)
    refs = _git_ops._git(
      repo,
      "for-each-ref", "--format=%(refname)", f"{fork_heads_prefix}/",
      check=False,
    )
    if refs.returncode == 0:
      for ref in (refs.stdout or "").splitlines():
        _git_ops._git(repo, "update-ref", "-d", ref, check=False)


def _build_fork_compatible_topic_commit(
  repo: Path,
  *,
  branch: str,
  fork_sha: str,
  upstream_sha: str,
  diff_path: Path,
  expected_diff: str,
  author_name: str,
  author_email: str,
) -> str:
  """Re-parent an exact reviewed change onto a strictly-behind fork tip.

  The fork default branch is never changed. The temporary topic commit is
  accepted only when merging it into current upstream produces the exact
  reviewed source diff byte-for-byte. This avoids OAuth's workflow restriction
  without weakening review or silently changing the contribution.
  """
  message = _git_ops._git(repo, "log", "-1", "--format=%B", branch).stdout
  if _COAUTHOR_TRAILER not in message:
    raise ContributionSubmitError(
      "This staged commit is missing the Möbius Agent co-author trailer. "
      "Leave feedback so your agent can prepare it again."
    )

  message_path = None
  detached = False
  try:
    with tempfile.NamedTemporaryFile(
      "w", encoding="utf-8", delete=False,
    ) as message_file:
      message_file.write(message)
      message_path = message_file.name

    _git_ops._git(repo, "checkout", "-q", "--detach", fork_sha)
    detached = True
    applied = _git_ops._git(
      repo,
      "apply", "--index", "--3way", "--binary", str(diff_path),
      check=False,
    )
    if applied.returncode != 0:
      raise ContributionSubmitError(
        "The reviewed change cannot be placed safely on this stale PR fork. "
        "Leave feedback so your agent can refresh the contribution."
      )

    workflows = _git_ops._git(
      repo, "diff", "--cached", "--name-only", "--", ".github/workflows",
    ).stdout.strip()
    if workflows:
      raise ContributionSubmitError(
        "This reviewed contribution changes a GitHub Actions workflow. "
        "Reconnect GitHub with a classic token granting public_repo and "
        "workflow, then try Send again."
      )

    _git_ops._git(
      repo,
      "-c", f"user.name={author_name}",
      "-c", f"user.email={author_email}",
      "commit", "--no-gpg-sign", "-F", message_path,
    )
    push_sha = _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
    if not _GIT_SHA.match(push_sha):
      raise ContributionSubmitError(
        "Could not prepare the reviewed branch for this stale PR fork."
      )

    merged = _git_ops._git(
      repo, "merge-tree", "--write-tree", upstream_sha, push_sha,
      check=False,
    )
    merged_tree = (merged.stdout or "").strip().splitlines()[0:1]
    if (
      merged.returncode != 0
      or not merged_tree
      or not _GIT_SHA.match(merged_tree[0])
    ):
      raise ContributionSubmitError(
        "The stale-fork branch no longer merges cleanly with upstream. Leave "
        "feedback so your agent can refresh the contribution."
      )
    merged_hash = hashlib.sha256(
      _git_ops._reviewed_branch_diff(repo, upstream_sha, merged_tree[0])
    ).hexdigest()
    if merged_hash != expected_diff:
      raise ContributionSubmitError(
        "Adapting this branch to the stale PR fork would change the reviewed "
        "result, so Contribute stopped before pushing anything."
      )
    return push_sha
  finally:
    if message_path:
      try:
        os.unlink(message_path)
      except OSError:
        pass
    if detached:
      _git_ops._git(repo, "reset", "--hard", fork_sha, check=False)
      _git_ops._git(repo, "checkout", "-q", branch, check=False)


def _sync_owner_fork_with_workflow_scope(
  repo: Path,
  fork_slug: str,
  *,
  upstream_branch: str,
  upstream_sha: str,
) -> dict:
  """Fast-forward a proven-behind fork when the owner granted workflow scope."""
  synced = _git_ops._gh(
    repo,
    "api", "--method", "POST",
    f"repos/{fork_slug}/merge-upstream",
    "-f", f"branch={_git_ops._validate_branch(upstream_branch)}",
    check=False,
  )
  if synced.returncode != 0:
    detail = (synced.stderr or synced.stdout or "").strip()
    raise ContributionSubmitError(
      detail[:400] or "GitHub could not bring the PR fork up to date."
    )

  verified = _inspect_owner_fork_default_branch(
    repo,
    fork_slug,
    upstream_branch=upstream_branch,
    upstream_sha=upstream_sha,
  )
  if verified.get("last_submit_fork_sync") not in {
    "current", "contains-upstream",
  }:
    raise ContributionSubmitError(
      "GitHub did not finish refreshing the PR fork, so Contribute stopped "
      "before pushing the reviewed branch.",
      record_patch=verified,
    )
  return {**verified, "last_submit_fork_sync": "fast-forwarded"}


def _push_reviewed_topic(
  repo: Path,
  *,
  branch: str,
  fork_slug: str,
  merge_patch: dict,
  record_patch: dict,
  diff_path: Path,
  expected_diff: str,
  author_name: str,
  author_email: str,
  workflow_scope: bool = False,
) -> tuple[str, dict]:
  """Push the reviewed topic, inspecting a stale fork only when required."""
  push_source = "HEAD"
  last_push_error = _push_topic_branch(repo, branch, push_source)
  if not last_push_error:
    return push_source, record_patch
  if not _is_workflow_scope_push_error(last_push_error):
    raise push_rejected(last_push_error, record_patch=record_patch)

  # Most topic branches can be pushed without consulting the fork's default
  # branch. GitHub only makes that state relevant when a public_repo-only
  # OAuth token would introduce a workflow that landed upstream after the
  # fork fell behind. Inspect and adapt only on that specific rejection.
  try:
    fork_sync_patch = _inspect_owner_fork_default_branch(
      repo,
      fork_slug,
      upstream_branch=str(merge_patch["last_submit_upstream_branch"]),
      upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
    )
    record_patch = _git_ops._record_patch_with(record_patch, fork_sync_patch)
  except ContributionSubmitError as exc:
    raise _git_ops._merge_error_patch(exc, record_patch) from exc
  if fork_sync_patch.get("last_submit_fork_sync") != "strictly-behind":
    raise ContributionSubmitError(
      "GitHub refused this branch because the connection does not grant "
      "workflow access. Reconnect GitHub with a classic token granting "
      "public_repo and workflow, then try Send again.",
      record_patch=record_patch,
    )
  try:
    push_source = _build_fork_compatible_topic_commit(
      repo,
      branch=branch,
      fork_sha=str(fork_sync_patch["last_submit_fork_sha"]),
      upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
      diff_path=diff_path,
      expected_diff=expected_diff,
      author_name=author_name,
      author_email=author_email,
    )
    record_patch = _git_ops._record_patch_with(record_patch, {
      "last_submit_fork_sync": "stale-base-compatible",
      "last_submit_push_sha": push_source,
    })
  except ContributionSubmitError as exc:
    if not workflow_scope:
      raise ContributionSubmitError(
        "This reviewed change depends on newer code in a stale PR fork. "
        "In Contribute, enable optional workflow access, then try Send "
        "again; Contribute will fast-forward only that fork's default "
        "branch before pushing the reviewed topic branch.",
        record_patch=_git_ops._record_patch_with(record_patch, {
          "last_submit_requires_workflow_scope": True,
          "last_submit_compatible_error": exc.message,
        }),
      ) from exc
    try:
      synced_patch = _sync_owner_fork_with_workflow_scope(
        repo,
        fork_slug,
        upstream_branch=str(merge_patch["last_submit_upstream_branch"]),
        upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
      )
      record_patch = _git_ops._record_patch_with(record_patch, synced_patch)
      push_source = "HEAD"
    except ContributionSubmitError as sync_exc:
      raise _git_ops._merge_error_patch(sync_exc, record_patch) from sync_exc
  last_push_error = _push_topic_branch(repo, branch, push_source)
  if last_push_error:
    raise push_rejected(last_push_error, record_patch=record_patch)
  return push_source, record_patch


def _submit_prepared_pr(
  record: dict,
  diff_path: Path,
  *,
  direct_base_branch: str | None = None,
  expected_existing_pr_number: int | None = None,
) -> tuple[str, int | None, dict]:
  if not shutil.which("git") or not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs git and gh installed before it can submit PRs.",
      status_code=409,
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  login = str(state.get("login") or "")
  if not token or not login:
    raise ContributionSubmitError("Connect GitHub before approving this PR.", 401)
  author_name, author_email = _git_ops._connected_git_identity(state, login)

  plan = record.get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(plan.get("repo") or record.get("repo"))
  branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
  direct_base = (
    _git_ops._validate_branch(direct_base_branch) if direct_base_branch else None
  )
  repo = _safe_repo_path(plan.get("repo_path"))
  if not (repo / ".git").exists():
    raise ContributionSubmitError("The staged repo is not a git checkout.")

  title = str(plan.get("title") or record.get("title") or "").strip()
  body = str(plan.get("body_draft") or "").strip()
  if not title:
    raise ContributionSubmitError("This prepared PR is missing a title.")
  if not body:
    raise ContributionSubmitError("This prepared PR is missing its reviewed body.")

  checkout_back = None
  try:
    current_branch = _git_ops._git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    checkout_back = (
      _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
      if current_branch == "HEAD"
      else current_branch
    )
    _git_ops._git(repo, "check-ref-format", "--branch", branch)
    _git_ops._assert_clean_worktree(repo)
    _git_ops._git(repo, "checkout", "-q", branch)
    _git_ops._assert_clean_worktree(repo)
    expected_base, _, expected_diff = _git_ops._assert_fresh(record, diff_path, repo, branch)
    _git_ops._assert_coauthor_trailer(repo, branch)
    if direct_base:
      _git_ops._assert_head_attribution(
        repo,
        branch,
        author_name=author_name,
        author_email=author_email,
      )
      record_patch = {}
    else:
      record_patch = _git_ops._normalize_head_attribution(
        repo,
        branch,
        author_name=author_name,
        author_email=author_email,
        base_sha=expected_base,
        expected_diff=expected_diff,
        record=record,
      )
    _git_ops._assert_clean_worktree(repo)

    try:
      merge_patch = _git_ops._assert_merges_with_upstream(repo, upstream_repo, branch)
      record_patch = _git_ops._record_patch_with(record_patch, merge_patch)
    except ContributionSubmitError as exc:
      raise _git_ops._merge_error_patch(exc, record_patch) from exc
    # The merge preflight proves one exact upstream base. Pin that same branch
    # into both create and ambiguous-response recovery. Without an explicit
    # standalone --base, gh may honor stale branch.<name>.gh-merge-base config
    # from the durable staging checkout and publish the reviewed diff against a
    # different target.
    submit_base = direct_base or _git_ops._validate_branch(
      str(merge_patch.get("last_submit_upstream_branch") or "")
    )

    # Pre-publication truth check: everything above proves WHAT would be sent;
    # only the ledger row says WHETHER it was already sent, and that row is
    # agent-writable state that can regress (see _existing_branch_pr). Ask
    # GitHub before touching anything public — an OPEN or MERGED pull request
    # from this exact branch means this send can only be a duplicate, and
    # today's flow would push FIRST (silently rewriting that PR's public
    # branch) before GitHub refused the create. The resume path
    # (expected_existing_pr_number) legitimately expects its open PR and
    # keeps its own stricter exact-commit verification below.
    if expected_existing_pr_number is None:
      conflict = _existing_branch_pr(
        repo,
        upstream_repo,
        login,
        branch,
        same_repo=bool(direct_base),
      )
      if conflict is not None:
        conflict_url, conflict_state = conflict
        raise ContributionSubmitError(
          f"This branch already has a {conflict_state} pull request: "
          f"{conflict_url}. Nothing was pushed. Reconcile this card with "
          "that pull request — or re-stage the work on a fresh branch — "
          "instead of sending it again.",
          record_patch=record_patch,
        )

    push_source = "HEAD"
    if direct_base:
      try:
        _git_ops._assert_upstream_push_permission(repo, upstream_repo)
      except ContributionSubmitError as exc:
        raise _git_ops._merge_error_patch(exc, record_patch) from exc
      push_remote = f"https://github.com/{upstream_repo}.git"
      published_repo = upstream_repo
      record_patch = _git_ops._record_patch_with(record_patch, {
        "head_repository": upstream_repo,
        "last_submit_base_branch": direct_base,
        "last_submit_mode": "stack",
        "last_submit_push_sha": _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip(),
      })
      last_push_error = _push_branch(
        repo, push_remote, branch, push_source,
      )
      if last_push_error:
        raise push_rejected(last_push_error, record_patch=record_patch)
    else:
      try:
        fork_slug = _ensure_owner_fork_remote(repo, upstream_repo, login)
      except ContributionSubmitError as exc:
        raise _git_ops._merge_error_patch(exc, record_patch) from exc
      record_patch = _git_ops._record_patch_with(record_patch, {"head_repository": fork_slug})
      push_source, record_patch = _push_reviewed_topic(
        repo,
        branch=branch,
        fork_slug=fork_slug,
        merge_patch=merge_patch,
        record_patch=record_patch,
        diff_path=diff_path,
        expected_diff=expected_diff,
        author_name=author_name,
        author_email=author_email,
        workflow_scope="workflow" in set(state.get("scopes") or []),
      )
      published_repo = fork_slug
    pushed_branch_url = (
      f"https://github.com/{published_repo}/tree/{quote(branch, safe='/')}"
    )
    pushed_patch = {
      **record_patch,
      "last_submit_stage": "pushed",
      "last_pushed_branch": (
        branch if direct_base else f"{login}:{branch}"
      ),
      "last_pushed_branch_url": pushed_branch_url,
    }
    pushed_sha = str(
      pushed_patch.get("last_submit_push_sha")
      or pushed_patch.get("head_sha")
      or plan.get("head_sha")
      or ""
    ).strip()
    if not _GIT_SHA.match(pushed_sha):
      pushed_sha = _git_ops._git(repo, "rev-parse", push_source).stdout.strip()
    if not _GIT_SHA.match(pushed_sha):
      raise ContributionSubmitError(
        "Could not verify the exact reviewed commit after pushing this branch.",
        record_patch=pushed_patch,
      )
    pushed_patch["last_submit_push_sha"] = pushed_sha

    if expected_existing_pr_number is not None:
      existing = _find_existing_pr(
        repo,
        upstream_repo,
        login,
        branch,
        expected_head_sha=pushed_sha,
        base_branch=submit_base,
        same_repo=bool(direct_base),
      )
      if (
        not existing
        or _parse_pr_number(existing) != expected_existing_pr_number
      ):
        raise ContributionSubmitError(
          "The approved pull request is no longer open on this exact branch. "
          f"The reviewed branch was pushed to {pushed_branch_url}, but no new "
          "pull request was created.",
          record_patch=pushed_patch,
        )
      return existing, expected_existing_pr_number, pushed_patch

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
      f.write(body)
      body_file = f.name
    try:
      try:
        create_args = [
          "pr", "create",
          "-R", upstream_repo,
          "-H", branch if direct_base else f"{login}:{branch}",
          "--title", title,
          "--body-file", body_file,
        ]
        create_args.extend(("--base", submit_base))
        create_transport_error = None
        try:
          pr = _git_ops._gh(repo, *create_args, check=False)
        except subprocess.TimeoutExpired:
          pr = None
          create_transport_error = (
            "Timed out while waiting for GitHub to confirm pull request creation."
          )
        except OSError:
          pr = None
          create_transport_error = (
            "Could not start the GitHub pull request creation command."
          )
        if pr is None or pr.returncode != 0:
          # Retried sends commonly arrive after GitHub already created the PR.
          # A create transport failure is also ambiguous: GitHub may have
          # accepted the request before the local process lost its response.
          # Probe the reviewed branch and require its exact pushed commit before
          # treating the PR as open. Never issue a second create in this call.
          existing = _find_existing_pr(
            repo,
            upstream_repo,
            login,
            branch,
            expected_head_sha=pushed_sha,
            base_branch=submit_base,
            same_repo=bool(direct_base),
          )
          if existing:
            existing_number = _parse_pr_number(existing)
            label_patch = _apply_reviewed_pr_labels(
              repo,
              upstream_repo,
              existing_number,
              _reviewed_pr_labels(plan),
            )
            return (
              existing,
              existing_number,
              _git_ops._record_patch_with(pushed_patch, label_patch),
            )
          detail = create_transport_error or (
            pr.stderr or pr.stdout or "GitHub command failed."
          ).strip()
          raise ContributionSubmitError(detail[:600] or "GitHub command failed.")
      except ContributionSubmitError as exc:
        raise ContributionSubmitError(
          f"{exc.message} The branch was pushed to {pushed_branch_url}.",
          exc.status_code,
          record_patch=pushed_patch,
        )
    finally:
      try:
        os.unlink(body_file)
      except OSError:
        pass
    url = (pr.stdout or "").strip().splitlines()[-1].strip()
    if not url.startswith("https://github.com/"):
      raise ContributionSubmitError(
        f"GitHub did not return a pull request URL. The branch was pushed "
        f"to {pushed_branch_url}.",
        record_patch=pushed_patch,
      )
    number = _parse_pr_number(url)
    label_patch = _apply_reviewed_pr_labels(
      repo,
      upstream_repo,
      number,
      _reviewed_pr_labels(plan),
    )
    return url, number, _git_ops._record_patch_with(pushed_patch, label_patch)
  finally:
    if checkout_back:
      _git_ops._git(repo, "checkout", "-q", checkout_back, check=False)


def _preflight_prepared_stack(rows: list[dict]) -> None:
  """Prove every private layer before the first upstream branch is pushed."""
  if not shutil.which("git") or not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs git and gh installed before it can submit PRs."
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  login = str(state.get("login") or "")
  if not token or not login:
    raise ContributionSubmitError("Connect GitHub before approving this PR stack.", 401)
  author_name, author_email = _git_ops._connected_git_identity(state, login)
  sendable = [row for row in rows if row["record"].get("status") == "submitting"]
  if not sendable:
    raise ContributionSubmitError("Every PR in this stack has already been submitted.")

  first_plan = rows[0]["record"].get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(
    first_plan.get("repo") or rows[0]["record"].get("repo")
  )
  permission_repo = _safe_repo_path(
    (sendable[0]["record"].get("plan") or {}).get("repo_path")
  )
  default_branch = _git_ops._upstream_default_branch(permission_repo, upstream_repo)
  if rows[0]["stack"]["base_branch"] != default_branch:
    raise ContributionSubmitError(
      f"The first PR in this stack must target upstream {default_branch}."
    )
  _git_ops._assert_upstream_push_permission(permission_repo, upstream_repo)

  # A retry can legitimately contain a public parent plus a private child.
  # Verify an open/draft parent's branch before any new branch is pushed. A
  # merged parent needs a fresh child review on the default branch: squash and
  # rebase merges do not preserve the reviewed parent commit, so silently
  # retargeting the old child could repeat parent changes in its PR diff.
  for index, row in enumerate(rows):
    if row["record"].get("status") != "submitting" or index == 0:
      continue
    previous = rows[index - 1]
    previous_record = previous["record"]
    if previous_record.get("status") == "merged":
      raise ContributionSubmitError(
        "A parent PR in this stack has already merged. Nothing was sent; "
        "leave feedback so your agent can rebase and review the remaining "
        f"layers on {default_branch}."
      )
    if previous_record.get("status") in {"draft", "open"}:
      previous_plan = previous_record.get("plan") or {}
      _git_ops._assert_upstream_branch_at(
        permission_repo,
        upstream_repo,
        previous_plan.get("branch") or previous_record.get("branch"),
        str(previous_plan.get("head_sha") or ""),
      )

  for row in sendable:
    record = row["record"]
    plan = record.get("plan") or {}
    repo = _safe_repo_path(plan.get("repo_path"))
    branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
    if not (repo / ".git").exists():
      raise ContributionSubmitError("A staged stack repo is not a git checkout.")
    checkout_back = None
    try:
      current_branch = _git_ops._git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
      checkout_back = (
        _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
        if current_branch == "HEAD"
        else current_branch
      )
      _git_ops._git(repo, "check-ref-format", "--branch", branch)
      _git_ops._assert_clean_worktree(repo)
      _git_ops._git(repo, "checkout", "-q", branch)
      _git_ops._assert_clean_worktree(repo)
      _git_ops._assert_fresh(record, row["diff_path"], repo, branch)
      _git_ops._assert_coauthor_trailer(repo, branch)
      _git_ops._assert_head_attribution(
        repo,
        branch,
        author_name=author_name,
        author_email=author_email,
      )
      _git_ops._assert_merges_with_upstream(repo, upstream_repo, branch)
    finally:
      if checkout_back:
        _git_ops._git(repo, "checkout", "-q", checkout_back, check=False)


def _push_stack_tip_with_lease(
  repo: Path,
  *,
  upstream_repo: str,
  target_branch: str,
  expected_base: str,
  landed_sha: str,
) -> None:
  """Atomically advance one unchanged upstream ref to a proven stack tip."""
  remote = f"https://github.com/{upstream_repo}.git"
  last_error = ""
  last_actual = expected_base
  for attempt in range(_PUSH_RETRIES):
    proc = _git_ops._git(
      repo,
      "push",
      f"--force-with-lease=refs/heads/{target_branch}:{expected_base}",
      remote,
      f"{landed_sha}:refs/heads/{target_branch}",
      check=False,
    )
    if proc.returncode == 0:
      return
    last_error = (proc.stderr or proc.stdout or "").strip()
    # A transport can fail after GitHub has accepted the ref update. Re-read the
    # target before reporting failure or retrying: the exact landed tip is proof
    # that this compare-and-swap succeeded, while every other value remains a
    # safe failure. This mirrors submission's lost-response reconciliation and
    # prevents the ledger from reopening a stack that is already live.
    last_actual = _git_ops._upstream_branch_sha(repo, upstream_repo, target_branch)
    if last_actual == landed_sha:
      return
    if not _is_transient_push_error(last_error):
      break
    if attempt + 1 < _PUSH_RETRIES:
      time.sleep(_PUSH_RETRY_BASE_SECONDS * (2 ** attempt))
  if last_actual is None:
    raise ContributionSubmitError(
      "GitHub did not confirm whether the atomic landing completed. The "
      "recovery journal is still intact; check again shortly.",
      status_code=503,
      code="landing_unconfirmed",
    )
  if (
    last_actual != expected_base
    or "stale info" in last_error.lower()
    or "fetch first" in last_error.lower()
  ):
    raise ContributionSubmitError(
      f"Upstream {target_branch} moved while this stack was landing. Nothing "
      "was overwritten; refresh the stack and run CI again."
    )
  raise ContributionSubmitError(
    (last_error[:600] if last_error else "GitHub rejected the atomic landing.")
  )


def _land_reviewed_stack(rows: list[dict]) -> tuple[str, str]:
  """Prove and atomically fast-forward an open, green PR stack."""
  if not shutil.which("git") or not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs git and gh installed before it can land PR stacks."
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  login = str(state.get("login") or "")
  if not token or not login:
    raise ContributionSubmitError("Connect GitHub before landing this PR stack.", 401)

  first_record = rows[0]["record"]
  first_plan = first_record.get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(
    first_plan.get("repo") or first_record.get("repo")
  )
  anchor_repo = _safe_repo_path(first_plan.get("repo_path"))
  target_branch = _git_ops._upstream_default_branch(anchor_repo, upstream_repo)
  if rows[0]["stack"]["base_branch"] != target_branch:
    raise ContributionSubmitError(
      f"The first PR in this stack no longer targets {target_branch}."
    )
  expected_base = _git_ops._resolve_reviewed_commit(
    anchor_repo, first_plan.get("base_sha"), "base sha",
  )

  _git_ops._assert_upstream_push_permission(anchor_repo, upstream_repo)
  _git_ops._assert_unprotected_landing_target(anchor_repo, upstream_repo, target_branch)
  _git_ops._assert_upstream_branch_at(
    anchor_repo, upstream_repo, target_branch, expected_base,
  )

  previous_head = ""
  top_repo = anchor_repo
  landed_sha = ""
  reviewed_refs = []
  for index, row in enumerate(rows):
    record = row["record"]
    if record.get("status") != "landing":
      raise ContributionSubmitError(
        "This PR stack changed while it was being verified. Refresh Contribute."
      )
    plan = record.get("plan") or {}
    repo = _safe_repo_path(plan.get("repo_path"))
    if not (repo / ".git").exists():
      raise ContributionSubmitError("A staged stack checkout is no longer available.")
    branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
    base_sha = str(plan.get("base_sha") or "")
    head_sha = str(plan.get("head_sha") or record.get("head_sha") or "")
    if index > 0 and base_sha != previous_head:
      raise ContributionSubmitError(
        "This public stack no longer has the exact reviewed parent chain. "
        "Nothing was changed; refresh it before landing."
      )

    checkout_back = None
    try:
      current_branch = _git_ops._git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
      checkout_back = (
        _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
        if current_branch == "HEAD"
        else current_branch
      )
      _git_ops._assert_clean_worktree(repo)
      _git_ops._git(repo, "checkout", "-q", branch)
      _git_ops._assert_clean_worktree(repo)
      _, resolved_head, _ = _git_ops._assert_fresh(
        record, row["diff_path"], repo, branch,
      )
      _git_ops._assert_coauthor_trailer(repo, branch)
      _git_ops._assert_upstream_branch_at(
        repo, upstream_repo, branch, resolved_head,
      )
      _git_ops._assert_pr_checks_green(
        repo,
        upstream_repo=upstream_repo,
        record=record,
        base_branch=row["stack"]["base_branch"],
        head_branch=branch,
      )
      previous_head = resolved_head
      top_repo = repo
      landed_sha = resolved_head
      reviewed_refs.append((branch, resolved_head))
    finally:
      if checkout_back:
        _git_ops._git(repo, "checkout", "-q", checkout_back, check=False)

  ancestry = _git_ops._git(
    top_repo,
    "merge-base", "--is-ancestor", expected_base, landed_sha,
    check=False,
  )
  if ancestry.returncode != 0:
    raise ContributionSubmitError(
      "The top of this stack is no longer a fast-forward from upstream. "
      "Nothing was changed."
    )

  # Recheck every public ref after reading CI so a concurrent branch update
  # cannot make the checks describe a different commit. The target ref is
  # also guarded by the push lease, making the final update compare-and-swap.
  _git_ops._assert_upstream_branch_at(
    anchor_repo, upstream_repo, target_branch, expected_base,
  )
  for branch, head_sha in reviewed_refs:
    _git_ops._assert_upstream_branch_at(
      anchor_repo,
      upstream_repo,
      branch,
      head_sha,
    )
  _push_stack_tip_with_lease(
    top_repo,
    upstream_repo=upstream_repo,
    target_branch=target_branch,
    expected_base=expected_base,
    landed_sha=landed_sha,
  )
  return target_branch, landed_sha
