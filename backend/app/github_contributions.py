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
from dataclasses import dataclass
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
from app.github_connection import has_full_pr_access
from app.terminal_output import readable_output
from app.github_contribution_contract import (
  BRANCH_NAME as _BRANCH_NAME,
  GITHUB_LOGIN as _GITHUB_LOGIN,
  GITHUB_REPO as _GITHUB_REPO,
  GIT_SHA as _GIT_SHA,
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
_PR_VISIBILITY_RETRIES = 3
_PR_VISIBILITY_RETRY_BASE_SECONDS = 0.5
_PUBLICATION_STAGES = frozenset({"draft", "ready"})


def _publication_status(stage: str) -> str:
  """Map the explicit GitHub publication stage onto the ledger lifecycle."""
  if stage not in _PUBLICATION_STAGES:
    raise ContributionSubmitError("This pull request has an invalid publication stage.")
  return "draft" if stage == "draft" else "open"


def _require_all_clear_review(record: dict) -> None:
  """Require an agent verdict pinned to the exact immutable prepared head."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  review = (
    record.get("quality_review")
    if isinstance(record.get("quality_review"), dict)
    else {}
  )
  head_sha = str(plan.get("head_sha") or "").lower()
  reviewed_head_sha = str(review.get("reviewed_head_sha") or "").lower()
  if (
    review.get("state") != "all_clear"
    or not _GIT_SHA.fullmatch(head_sha)
    or reviewed_head_sha != head_sha
  ):
    raise HTTPException(
      status_code=409,
      detail=(
        "This contribution needs a complete agent review on its exact current "
        "head before it can be sent."
      ),
    )


@dataclass(frozen=True)
class PersonalReadyTarget:
  """Exact personal-GitHub PR identity approved by one Ready action."""

  repo_path: Path
  repo: str
  number: int
  url: str
  head_repository: str
  head_branch: str
  base_branch: str
  head_sha: str

  def journal(self) -> dict:
    return {
      "version": 1,
      "repo": self.repo,
      "number": self.number,
      "url": self.url,
      "head_repository": self.head_repository,
      "head_branch": self.head_branch,
      "base_branch": self.base_branch,
      "expected_head_sha": self.head_sha,
    }


@dataclass(frozen=True)
class PublicationHandoffSpec:
  """Immutable reviewed inputs for connecting one published local app."""

  contribution_id: str
  target_app_id: int
  source_repo: Path
  repo_slug: str
  manifest_url: str
  manifest_id: str
  reviewed_base_sha: str
  reviewed_head_sha: str
  reviewed_source_sha: str
  diff_sha256: str
  package_digest: str
  capability_digest: str

  def pinned_manifest_url(self, merge_sha: str) -> str:
    parsed = urlparse(self.manifest_url)
    parts = [part for part in parsed.path.split("/") if part]
    return (
      f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/"
      f"{merge_sha}/mobius.json"
    )


def publication_handoff_spec(
  record: dict,
  db: Session,
) -> PublicationHandoffSpec:
  """Validate a reviewed app-publication handoff against the live app row.

  The ledger is app-writable, so it is routing data rather than authority. The
  immutable reviewed Git objects and landed equivalence witness are verified
  separately before installation; this step narrows that later proof to the
  one local app, canonical repository, and package the owner reviewed.
  """
  from app import install
  from app.app_capabilities import contract_and_digest

  plan = record.get("plan") if isinstance(record.get("plan"), dict) else None
  handoff = plan.get("after_merge") if plan else None
  if (
    record.get("type") != "pr"
    or record.get("status") != "merged"
    or not isinstance(handoff, dict)
    or handoff.get("action") != "connect_app"
  ):
    raise ContributionSubmitError(
      "This contribution has no merged app publication to connect."
    )

  contribution_id = str(record.get("id") or "")
  if not _CONTRIBUTION_ID.fullmatch(contribution_id):
    raise ContributionSubmitError("This contribution record is invalid.")
  try:
    target_app_id = int(handoff.get("app_id"))
  except (TypeError, ValueError):
    raise ContributionSubmitError(
      "This publication no longer identifies an installed app."
    ) from None
  target = (
    db.query(models.App)
    .filter(
      models.App.id == target_app_id,
      models.App.deleted_at.is_(None),
    )
    .first()
  )
  if target is None:
    raise ContributionSubmitError(
      "The local app this publication reviewed is no longer installed."
    )

  source_repo = _safe_equivalence_source_path(plan.get("source_repo_path"))
  try:
    target_source = Path(target.source_dir).resolve()
  except (OSError, RuntimeError):
    raise ContributionSubmitError(
      "The local app source is no longer available."
    ) from None
  if source_repo != target_source:
    raise ContributionSubmitError(
      "This publication no longer points to the reviewed local app."
    )

  manifest_url = str(handoff.get("manifest_url") or "").strip()
  parsed = urlparse(manifest_url)
  parts = [part for part in parsed.path.split("/") if part]
  if (
    parsed.scheme != "https"
    or parsed.hostname != "raw.githubusercontent.com"
    or parsed.netloc != "raw.githubusercontent.com"
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or len(parts) != 4
    or parts[0] != "mobius-os"
    or not parts[1].startswith("app-")
    or parts[2] != "main"
    or parts[3] != "mobius.json"
  ):
    raise ContributionSubmitError(
      "This publication does not use a canonical Möbius app manifest."
    )
  repo_slug = f"{parts[0]}/{parts[1]}"
  if str(plan.get("repo") or record.get("repo") or "") != repo_slug:
    raise ContributionSubmitError(
      "This publication manifest belongs to a different repository."
    )

  base_sha = str(plan.get("base_sha") or "").lower()
  head_sha = str(plan.get("head_sha") or "").lower()
  source_sha = str(plan.get("source_sha") or "").lower()
  diff_sha256 = str(plan.get("diff_sha256") or "").lower()
  if (
    not _GIT_SHA.fullmatch(base_sha)
    or not _GIT_SHA.fullmatch(head_sha)
    or not _GIT_SHA.fullmatch(source_sha)
    or not re.fullmatch(r"[0-9a-f]{64}", diff_sha256)
  ):
    raise ContributionSubmitError(
      "This older publication is missing its immutable review proof."
    )
  try:
    reviewed_tree = app_git.read_ref_tree(source_repo, head_sha)
    manifest_id, package_digest = install.package_content_digest_from_tree(
      reviewed_tree,
    )
    reviewed_manifest = json.loads(reviewed_tree["mobius.json"])
    _contract, capability_digest = contract_and_digest(reviewed_manifest)
  except (
    install.PackageContentError,
    json.JSONDecodeError,
    KeyError,
    OSError,
    RuntimeError,
    subprocess.SubprocessError,
    UnicodeDecodeError,
    ValueError,
  ) as exc:
    raise ContributionSubmitError(
      "The reviewed app package can no longer be reproduced safely."
    ) from exc

  repo_manifest_id = parts[1].removeprefix("app-")
  previous_id = reviewed_manifest.get("previous_id")
  if (
    manifest_id != repo_manifest_id
    or target.slug not in {manifest_id, previous_id}
  ):
    raise ContributionSubmitError(
      "The reviewed package belongs to a different local app."
    )
  if target.manifest_url is not None and not install._catalog_identity_matches(
    target.manifest_url, manifest_url, manifest_id,
  ):
    raise ContributionSubmitError(
      "This installed app is already connected to a different package."
    )

  return PublicationHandoffSpec(
    contribution_id=contribution_id,
    target_app_id=target_app_id,
    source_repo=source_repo,
    repo_slug=repo_slug,
    manifest_url=manifest_url,
    manifest_id=manifest_id,
    reviewed_base_sha=base_sha,
    reviewed_head_sha=head_sha,
    reviewed_source_sha=source_sha,
    diff_sha256=diff_sha256,
    package_digest=package_digest,
    capability_digest=capability_digest,
  )

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
  # A durable repo must live under one of these roots so a restart can find it.
  # "contrib" is the staging root agents use for private review worktrees.
  allowed_roots = (
    data_dir / "contrib",
    # Prepared records created before the staging-root rename still point at
    # this durable checkout. Keep it reachable for the supported upgrade
    # window; removing the path would strand reviewed owner work.
    data_dir / "contributions",
    data_dir / "apps",
    data_dir / "platform",
  )
  for root in allowed_roots:
    try:
      repo.relative_to(root)
      return repo
    except ValueError:
      continue
  raise ContributionSubmitError(
    "This prepared PR was staged outside Mobius' durable contribution folders. "
    "Ask the agent to prepare it again from /data/contrib, "
    "/data/contributions, /data/apps, or /data/platform; nothing was sent "
    "to GitHub."
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
  raw_repo = plan.get("repo_path")
  repo = _safe_repo_path(raw_repo)
  recorded_repo = Path(raw_repo)
  if not recorded_repo.is_absolute() or recorded_repo != repo:
    return False
  data_dir = Path(get_settings().data_dir).resolve()
  roots = (data_dir / "contrib", data_dir / "contributions")
  if not any(repo.is_relative_to(root) for root in roots):
    return False
  if not repo.exists():
    return True
  marker = repo / ".git"
  if not marker.exists() or marker.is_symlink():
    return False

  # Linked worktrees and separate-git-dir checkouts both use a .git file.
  if marker.is_file():
    try:
      marker_value = marker.read_text().strip()
    except OSError:
      return False
    if not marker_value.startswith("gitdir:"):
      return False
    raw_git_dir = marker_value.removeprefix("gitdir:").strip()
    if not raw_git_dir:
      return False
    marker_git_dir = Path(raw_git_dir)
    if not marker_git_dir.is_absolute():
      marker_git_dir = marker.parent / marker_git_dir
    try:
      marker_git_dir = marker_git_dir.resolve()
    except (OSError, RuntimeError):
      return False

    separate_git_dir = (
      marker_git_dir.name == "git" and marker_git_dir.parent == repo.parent
    )
    if separate_git_dir:
      # Delete the Git directory first. If removing the checkout then fails,
      # the next call sees its missing sibling and can finish idempotently.
      if marker_git_dir.exists():
        shutil.rmtree(marker_git_dir)
      shutil.rmtree(repo)
      return True

    # Git can recycle a pruned worktree admin slot for a newer checkout. Every
    # linked admin directory must still point back to this exact checkout.
    back_reference = marker_git_dir / "gitdir"
    owns_checkout = False
    if back_reference.is_file() and not back_reference.is_symlink():
      try:
        registered_marker = Path(back_reference.read_text().strip())
        if not registered_marker.is_absolute():
          registered_marker = back_reference.parent / registered_marker
        owns_checkout = registered_marker.resolve() == marker.resolve()
      except (OSError, RuntimeError):
        owns_checkout = False
    if not owns_checkout:
      shutil.rmtree(repo)
      return True

    common_pointer = marker_git_dir / "commondir"
    if not common_pointer.is_file() or common_pointer.is_symlink():
      return False
    try:
      raw_common_dir = common_pointer.read_text().strip()
      if not raw_common_dir:
        return False
      common_dir = Path(raw_common_dir)
      if not common_dir.is_absolute():
        common_dir = common_pointer.parent / common_dir
      common_dir = common_dir.resolve()
    except (OSError, RuntimeError):
      return False
    common_roots = (
      data_dir / "platform",
      data_dir / "apps",
      data_dir / "contrib",
      data_dir / "contributions",
    )
    if not any(common_dir.is_relative_to(root) for root in common_roots):
      return False

    env = dict(os.environ)
    for name in (
      "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
      "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    ):
      env.pop(name, None)
    # Prepared reviews are durable owner work, so the contribution workflow
    # locks linked worktrees against an unrelated `git worktree prune`. Once a
    # record is terminal and the reciprocal admin pointer above proves this is
    # the exact disposable checkout, release that lock before removal.
    subprocess.run(
      [
        "git", f"--git-dir={common_dir}",
        "worktree", "unlock", str(repo),
      ],
      cwd=str(data_dir),
      capture_output=True,
      text=True,
      check=False,
      env=env,
    )
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
    return True

  # Ordinary standalone clones keep a real .git directory inside the checkout.
  shutil.rmtree(repo)
  return True


def _claim_record(
  *, app_id: int, record_id: str, db: Session, expected_nonce: str | None,
  submitter: str = "contribute-button",
  expected_action: str = "pr",
) -> tuple[dict, Path, Path]:
  record_path, diff_path = _record_paths(app_id, record_id)
  _recheck_submit_app(db, app_id, expected_nonce)
  record = _read_record(record_path)
  plan = record.get("plan")
  if not isinstance(plan, dict):
    raise HTTPException(
      status_code=409,
      detail="This older contribution needs agent review before it can submit.",
    )
  if plan.get("action") != expected_action or record.get("type") != "pr":
    raise HTTPException(
      status_code=400,
      detail=(
        "Direct approval currently supports pull requests."
        if expected_action == "pr"
        else "This approval action no longer matches the prepared PR update."
      ),
    )
  resumable_successor = (
    record.get("status") == "submitting"
    and expected_action == "pr_update"
    and isinstance(plan.get("successor"), dict)
    and record.get("submitter") == submitter
  )
  if record.get("status") != "prepared" and not resumable_successor:
    raise HTTPException(
      status_code=409,
      detail="This contribution is no longer waiting for approval.",
    )
  if isinstance(plan.get("stack"), dict):
    raise HTTPException(
      status_code=409,
      detail=(
        "This contribution belongs to a PR stack. Review and send the complete "
        "chain together."
      ),
    )
  _require_all_clear_review(record)
  if resumable_successor:
    return record, record_path, diff_path
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


def _validate_stack_records(
  records: list[dict],
  *,
  allowed_actions: frozenset[str] = frozenset({"pr"}),
) -> list[dict]:
  """Validate one complete, immutable parent-to-child contribution chain.

  Publishing a new stack keeps the narrow ``pr`` default. Read-only review
  callers may additionally admit ``pr_update`` so an already-public stack can
  be re-reviewed layer by layer without making the stack submit endpoint a
  second, less-specific update path.
  """
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
    if record.get("type") != "pr" or plan.get("action") not in allowed_actions:
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
  allowed_actions: frozenset[str] = frozenset({"pr"}),
  prepared_actions: frozenset[str] | None = None,
  submitter: str = "contribute-stack-button",
  already_detail: str = "Every PR in this stack has already been submitted.",
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
    validated = _validate_stack_records(
      [row["record"] for row in rows],
      allowed_actions=allowed_actions,
    )
  except ContributionSubmitError as exc:
    raise HTTPException(status_code=409, detail=exc.message) from exc
  claimable_actions = prepared_actions or allowed_actions
  by_id = {row["record"]["id"]: row for row in rows}
  for item in validated:
    if item["record"].get("status") == "prepared":
      plan = item["record"].get("plan") or {}
      if plan.get("action") not in claimable_actions:
        raise HTTPException(
          status_code=409,
          detail="A private stack layer is prepared for a different public action.",
        )
      _require_all_clear_review(item["record"])
  ordered = []
  now = _now_iso()
  for item in validated:
    row = by_id[item["record"]["id"]]
    record = row["record"]
    if record.get("status") == "prepared":
      record = {
        **record,
        "status": "submitting",
        "submitter": submitter,
        "submit_started_at": now,
        "updated_at": now,
      }
      _write_record(row["record_path"], record)
    ordered.append({**row, "record": record, "stack": item["stack"]})
  if not any(row["record"].get("status") == "submitting" for row in ordered):
    raise HTTPException(
      status_code=409,
      detail=already_detail,
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
  code: str = "",
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
  effective_code = code or str(
    (record_patch or {}).get("last_submit_error_code") or ""
  )
  if effective_code:
    next_record["last_submit_error_code"] = effective_code
  else:
    next_record.pop("last_submit_error_code", None)
  _write_record(record_path, next_record)
  return next_record


def _note_submit_unconfirmed(
  *, record_path: Path, message: str, record_patch: dict,
  code: str, detail: str,
) -> dict:
  """Keep one already-authorized public action resumable after ambiguity."""
  record = _read_record(record_path)
  if record.get("status") != "submitting":
    return record
  next_record = {
    **record,
    **record_patch,
    "status": "submitting",
    "last_submit_error": message,
    "last_submit_error_code": code or "update_unconfirmed",
    "updated_at": _now_iso(),
  }
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
  publication_stage = str(
    (record_patch or {}).get("publication_stage") or "draft"
  )
  next_record = {
    **record,
    **(record_patch or {}),
    "status": _publication_status(publication_stage),
    "publication_stage": publication_stage,
    "url": pr_url,
    "updated_at": now,
    "submitted_at": now,
  }
  if number is not None:
    next_record["number"] = number
  next_record.pop("last_submit_error", None)
  next_record.pop("last_submit_error_detail", None)
  next_record.pop("last_submit_error_code", None)
  _write_record(record_path, next_record)
  return next_record


def _mark_existing_pr_update_success(
  *,
  record_path: Path,
  record: dict,
  pr_url: str,
  number: int,
  record_patch: dict | None = None,
) -> dict:
  """Settle an owner-approved fast-forward of an already-open PR.

  Keep the original submission timestamp: this action updates one existing
  public request rather than opening a new one. The reviewed update timestamp
  gives the ledger an exact lifecycle witness without inventing a second PR.
  """
  now = _now_iso()
  publication_stage = str(
    (record_patch or {}).get("publication_stage")
    or record.get("publication_stage")
    or "ready"
  )
  next_record = {
    **record,
    **(record_patch or {}),
    "status": _publication_status(publication_stage),
    "publication_stage": publication_stage,
    "url": pr_url,
    "number": number,
    "updated_at": now,
    "last_updated_pr_at": now,
  }
  next_record.pop("last_submit_error", None)
  next_record.pop("last_submit_error_detail", None)
  next_record.pop("last_submit_error_code", None)
  _write_record(record_path, next_record)
  return next_record


def _mark_stack_submit_failure(
  rows: list[dict],
  message: str,
  *,
  failed_id: str | None = None,
  record_patch: dict | None = None,
  code: str = "",
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
        code=code if is_failed else "",
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
  # A successful branch update can precede the matching PR metadata by a few
  # seconds. Repeat only this read-only exact-head proof; never repeat the push
  # or PR creation that led here.
  for attempt in range(_PR_VISIBILITY_RETRIES):
    try:
      proc = _git_ops._gh(
        repo,
        *args,
        check=False,
      )
    except (subprocess.TimeoutExpired, OSError):
      proc = None
    rows = None
    if proc is not None and proc.returncode == 0:
      try:
        rows = json.loads(proc.stdout or "[]")
      except ValueError:
        pass
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
    if attempt + 1 < _PR_VISIBILITY_RETRIES:
      time.sleep(_PR_VISIBILITY_RETRY_BASE_SECONDS * (2 ** attempt))
  return None


def _confirm_existing_pr_update(
  repo: Path,
  upstream_repo: str,
  number: int,
  *,
  expected_head_repository: str,
  expected_head_sha: str,
  branch: str,
  base_branch: str,
  expected_base_sha: str | None = None,
) -> tuple[str, str] | None:
  """Confirm one known PR after its reviewed branch was pushed.

  Updates already carry an immutable PR number, so a branch-list search is the
  wrong proof: that index can lag even while the PR endpoint already exposes
  the new head. Read the known PR directly and retry only this exact,
  side-effect-free confirmation. The push itself is never repeated here.
  """
  if (
    number < 1
    or not _GIT_SHA.match(str(expected_head_sha or ""))
    or (
      expected_base_sha is not None
      and not _GIT_SHA.match(str(expected_base_sha or ""))
    )
  ):
    return None
  args = ("api", f"repos/{upstream_repo}/pulls/{number}")
  for attempt in range(_PR_VISIBILITY_RETRIES):
    try:
      proc = _git_ops._gh(repo, *args, check=False)
    except (subprocess.TimeoutExpired, OSError):
      proc = None
    live = None
    if proc is not None and proc.returncode == 0:
      try:
        live = json.loads(proc.stdout or "{}")
      except ValueError:
        pass
    if isinstance(live, dict):
      head = live.get("head") if isinstance(live.get("head"), dict) else {}
      base = live.get("base") if isinstance(live.get("base"), dict) else {}
      head_repo = (
        head.get("repo") if isinstance(head.get("repo"), dict) else {}
      )
      url = live.get("html_url")
      if (
        live.get("state") == "open"
        and head_repo.get("full_name") == expected_head_repository
        and head.get("ref") == branch
        and head.get("sha") == expected_head_sha
        and base.get("ref") == base_branch
        and (
          expected_base_sha is None
          or base.get("sha") == expected_base_sha
        )
        and isinstance(live.get("draft"), bool)
        and isinstance(url, str)
        and url == f"https://github.com/{upstream_repo}/pull/{number}"
      ):
        return url, ("draft" if bool(live.get("draft")) else "ready")
    if attempt + 1 < _PR_VISIBILITY_RETRIES:
      time.sleep(_PR_VISIBILITY_RETRY_BASE_SECONDS * (2 ** attempt))
  return None


def _personal_ready_target(record: dict) -> PersonalReadyTarget:
  """Derive the one exact personal PR that a record may mark ready."""
  if (
    record.get("submission_mode") == "mobius-bot"
    or record.get("relay_contribution_id")
  ):
    raise HTTPException(
      status_code=409,
      detail=(
        "Möbius-published drafts stay draft-only until the relay supports its "
        "own owner-approved Ready action."
      ),
    )
  if record.get("type") != "pr":
    raise HTTPException(status_code=400, detail="Ready applies to pull requests only.")
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  try:
    repo = _git_ops._validate_repo_slug(plan.get("repo") or record.get("repo"))
    head_repository = _git_ops._validate_repo_slug(
      record.get("head_repository") or plan.get("head_repository")
    )
    head_branch = _git_ops._validate_branch(
      plan.get("branch") or record.get("branch")
    )
    base_branch = _git_ops._validate_branch(
      record.get("last_submit_base_branch")
      or record.get("last_submit_upstream_branch")
    )
    repo_path = _safe_repo_path(plan.get("repo_path"))
  except ContributionSubmitError as exc:
    raise HTTPException(status_code=409, detail=exc.message) from exc
  try:
    number = int(record.get("number"))
  except (TypeError, ValueError):
    number = 0
  url = str(record.get("url") or "").rstrip("/")
  expected_url = f"https://github.com/{repo}/pull/{number}"
  head_sha = str(record.get("last_submit_push_sha") or "").lower()
  if number <= 0 or url != expected_url or not _GIT_SHA.fullmatch(head_sha):
    raise HTTPException(
      status_code=409,
      detail=(
        "This contribution has no exact server-confirmed personal pull request "
        "head to mark ready. Refresh it before trying again."
      ),
    )
  if not (repo_path / ".git").exists():
    raise HTTPException(
      status_code=409,
      detail="The reviewed checkout for this pull request is no longer available.",
    )

  # Standalone submission may amend only commit attribution before pushing. In
  # that case the reviewed diff remains exact while the public commit is the
  # plan head and `attribution_normalized_from` names the reviewed predecessor.
  review = (
    record.get("quality_review")
    if isinstance(record.get("quality_review"), dict)
    else {}
  )
  plan_head = str(plan.get("head_sha") or "").lower()
  reviewed_head = str(review.get("reviewed_head_sha") or "").lower()
  normalized_from = str(plan.get("attribution_normalized_from") or "").lower()
  if (
    review.get("state") != "all_clear"
    or plan_head != head_sha
    or reviewed_head not in {plan_head, normalized_from}
  ):
    raise HTTPException(
      status_code=409,
      detail=(
        "This pull request no longer has an all-clear review pinned to its "
        "exact public head. Review it again before marking it ready."
      ),
    )
  return PersonalReadyTarget(
    repo_path=repo_path,
    repo=repo,
    number=number,
    url=url,
    head_repository=head_repository,
    head_branch=head_branch,
    base_branch=base_branch,
    head_sha=head_sha,
  )


def _ready_claim_matches(record: dict, target: PersonalReadyTarget) -> bool:
  claim = record.get("readying")
  if not isinstance(claim, dict):
    return False
  expected = target.journal()
  return all(claim.get(key) == value for key, value in expected.items())


def _claim_personal_pr_ready(
  *,
  app_id: int,
  record_id: str,
  expected_head_sha: str,
  db: Session,
  expected_nonce: str | None,
) -> tuple[dict, Path, PersonalReadyTarget, str]:
  """Persist one exact Ready approval before any GitHub read or mutation."""
  _recheck_submit_app(db, app_id, expected_nonce)
  record_path, _diff_path = _record_paths(app_id, record_id)
  record = _read_record(record_path)
  if str(record.get("id") or "") != record_id:
    raise HTTPException(status_code=409, detail="This contribution record changed.")
  if record.get("status") not in {"draft", "open"}:
    raise HTTPException(
      status_code=409,
      detail="This contribution is not an open personal draft.",
    )
  target = _personal_ready_target(record)
  approved_head = str(expected_head_sha or "").lower()
  if not _GIT_SHA.fullmatch(approved_head) or approved_head != target.head_sha:
    raise HTTPException(
      status_code=409,
      detail=(
        "This pull request changed after the Ready action was shown. Refresh "
        "Contribute and approve its current head."
      ),
    )
  if isinstance(record.get("readying"), dict):
    if not _ready_claim_matches(record, target):
      raise HTTPException(
        status_code=409,
        detail="The saved Ready action no longer matches this pull request.",
      )
    return record, record_path, target, "recover"

  now = _now_iso()
  claimed = {
    **record,
    "readying": {**target.journal(), "started_at": now},
    "updated_at": now,
  }
  claimed.pop("last_ready_error", None)
  claimed.pop("last_ready_error_code", None)
  _write_record(record_path, claimed)
  return claimed, record_path, target, "new"


def _inspect_personal_pr_ready_target(target: PersonalReadyTarget) -> dict:
  """Read GitHub and prove the saved PR identity and immutable public head."""
  if not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs gh installed before it can mark a pull request ready."
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  if not token:
    raise ContributionSubmitError(
      "Connect GitHub before marking this pull request ready.", 401,
    )
  if not has_full_pr_access(state.get("scopes")):
    raise ContributionSubmitError(
      "Reconnect GitHub with full PR access before marking this pull request ready."
    )

  last_detail = ""
  for attempt in range(_PR_VISIBILITY_RETRIES):
    try:
      proc = _git_ops._gh(
        target.repo_path,
        "api", f"repos/{target.repo}/pulls/{target.number}",
        check=False,
      )
    except (subprocess.TimeoutExpired, OSError) as exc:
      proc = None
      last_detail = readable_output(str(exc))
    live = None
    if proc is not None and proc.returncode == 0:
      try:
        live = json.loads(proc.stdout or "{}")
      except ValueError:
        last_detail = "GitHub returned invalid pull request metadata."
    elif proc is not None:
      last_detail = readable_output(proc.stderr or proc.stdout or "GitHub lookup failed.")
    if not isinstance(live, dict):
      if attempt + 1 < _PR_VISIBILITY_RETRIES:
        time.sleep(_PR_VISIBILITY_RETRY_BASE_SECONDS * (2 ** attempt))
        continue
      raise ContributionSubmitError(
        "Contribute could not verify this pull request on GitHub. Nothing was changed.",
        status_code=503,
        code="ready_lookup_failed",
        detail=last_detail,
      )

    head = live.get("head") if isinstance(live.get("head"), dict) else {}
    base = live.get("base") if isinstance(live.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    node_id = str(live.get("node_id") or "")
    if (
      live.get("state") != "open"
      or live.get("html_url") != target.url
      or head_repo.get("full_name") != target.head_repository
      or head.get("ref") != target.head_branch
      or str(head.get("sha") or "").lower() != target.head_sha
      or base_repo.get("full_name") != target.repo
      or base.get("ref") != target.base_branch
      or not node_id
      or not isinstance(live.get("draft"), bool)
    ):
      raise ContributionSubmitError(
        "The live pull request no longer matches the exact reviewed Ready action. Nothing was changed.",
        code="ready_target_changed",
      )
    return {
      "node_id": node_id,
      "is_draft": live["draft"],
      # Marking a draft ready can satisfy the last condition of an already
      # armed auto-merge. Keep that separate public action outside this narrow
      # endpoint rather than letting Ready trigger it indirectly.
      "auto_merge_enabled": live.get("auto_merge") is not None,
    }
  raise AssertionError("unreachable")


_MARK_READY_MUTATION = """
mutation MarkContributionReady($pullRequestId: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
    pullRequest { id isDraft headRefOid url }
  }
}
""".strip()


def _mark_personal_pr_ready(
  target: PersonalReadyTarget, *, node_id: str,
) -> None:
  """Issue the one narrow GitHub mutation authorized by a saved Ready claim."""
  try:
    proc = _git_ops._gh(
      target.repo_path,
      "api", "graphql",
      "-f", f"query={_MARK_READY_MUTATION}",
      "-f", f"pullRequestId={node_id}",
      check=False,
    )
  except (subprocess.TimeoutExpired, OSError) as exc:
    raise ContributionSubmitError(
      "GitHub did not confirm whether this pull request became ready. Contribute saved the action and will only re-read its state.",
      status_code=503,
      code="ready_unconfirmed",
      detail=readable_output(str(exc)),
    ) from exc
  detail = readable_output(proc.stderr or proc.stdout or "")
  if proc.returncode != 0:
    raise ContributionSubmitError(
      "GitHub did not confirm whether this pull request became ready. Contribute saved the action and will only re-read its state.",
      status_code=503,
      code="ready_unconfirmed",
      detail=detail,
    )
  try:
    payload = json.loads(proc.stdout or "{}")
  except ValueError as exc:
    raise ContributionSubmitError(
      "GitHub did not confirm whether this pull request became ready. Contribute saved the action and will only re-read its state.",
      status_code=503,
      code="ready_unconfirmed",
      detail="GitHub returned an invalid mutation result.",
    ) from exc
  if not isinstance(payload, dict) or payload.get("errors"):
    raise ContributionSubmitError(
      "GitHub did not confirm whether this pull request became ready. Contribute saved the action and will only re-read its state.",
      status_code=503,
      code="ready_unconfirmed",
      detail=detail or "GitHub returned a Ready mutation error.",
    )


def _assert_ready_claim(
  record: dict, target: PersonalReadyTarget,
) -> None:
  current_target = _personal_ready_target(record)
  if current_target != target or not _ready_claim_matches(record, target):
    raise ContributionSubmitError(
      "This contribution changed while its Ready action was being reconciled."
    )


def _settle_personal_pr_ready(
  record_path: Path, target: PersonalReadyTarget,
) -> dict:
  current = _read_record(record_path)
  _assert_ready_claim(current, target)
  now = _now_iso()
  # Never regress a terminal state if another reconciler observed a merge or
  # close while this owner-approved Ready action was in flight.
  status = current.get("status")
  settled_status = status if status in {"merged", "closed"} else "open"
  updated = {
    **current,
    "status": settled_status,
    "publication_stage": "ready",
    "ready_at": now,
    "last_ready_head_sha": target.head_sha,
    "updated_at": now,
  }
  updated.pop("readying", None)
  updated.pop("last_ready_error", None)
  updated.pop("last_ready_error_code", None)
  _write_record(record_path, updated)
  return updated


def _release_personal_pr_ready(
  record_path: Path,
  target: PersonalReadyTarget,
  error: ContributionSubmitError,
  *,
  confirmed_draft: bool = False,
) -> dict:
  current = _read_record(record_path)
  _assert_ready_claim(current, target)
  updated = {
    **current,
    "last_ready_error": error.message,
    "last_ready_error_code": error.code or "ready_failed",
    "updated_at": _now_iso(),
  }
  if confirmed_draft:
    updated["status"] = "draft"
    updated["publication_stage"] = "draft"
  updated.pop("readying", None)
  _write_record(record_path, updated)
  return updated


def _note_personal_pr_ready_unconfirmed(
  record_path: Path,
  target: PersonalReadyTarget,
  error: ContributionSubmitError,
) -> dict:
  current = _read_record(record_path)
  _assert_ready_claim(current, target)
  updated = {
    **current,
    "last_ready_error": error.message,
    "last_ready_error_code": "ready_unconfirmed",
    "updated_at": _now_iso(),
  }
  _write_record(record_path, updated)
  return updated


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


def _is_transient_push_error(message: str) -> bool:
  """Retry transport/server failures, never deterministic push rejections."""
  return _git_ops._is_transient_transport_error(message)


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
  # A cached `fork` remote is never trusted on its own: it can name a fork the
  # owner has since deleted on GitHub, and pushing reviewed code at a missing
  # repository fails with an opaque error. Drop any existing remote and always
  # re-resolve through `gh repo fork`, which is idempotent — it reuses the
  # owner's fork or creates one — so a stale, ambient, or deleted remote all
  # heal the same way. The staged contribution checkout is disposable, so
  # replacing the remote is safe.
  if _git_ops._git(repo, "remote", "get-url", "fork", check=False).returncode == 0:
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
  """Classify a reusable PR fork's default branch without mutating it."""
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

    return {**patch, "last_submit_fork_sync": "strictly-behind"}
  finally:
    _git_ops._git(repo, "update-ref", "-d", fork_ref, check=False)


def _sync_owner_fork(
  repo: Path,
  fork_slug: str,
  *,
  upstream_branch: str,
  upstream_sha: str,
) -> dict:
  """Fast-forward a proven-behind fork and verify the resulting default."""
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


def _submit_prepared_pr(
  record: dict,
  diff_path: Path,
  *,
  direct_base_branch: str | None = None,
  expected_existing_pr_number: int | None = None,
  expected_existing_head_repository: str | None = None,
  publication_stage: str = "draft",
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
  if not has_full_pr_access(state.get("scopes")):
    raise ContributionSubmitError(
      "Reconnect GitHub with full PR access before approving this PR.",
      status_code=409,
    )
  author_name, author_email = _git_ops._connected_git_identity(state, login)
  _publication_status(publication_stage)

  plan = record.get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(plan.get("repo") or record.get("repo"))
  branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
  existing_head_repository = None
  if expected_existing_pr_number is not None:
    if expected_existing_head_repository is None:
      raise ContributionSubmitError(
        "This existing pull request update is missing its verified head repository."
      )
    existing_head_repository = _git_ops._validate_repo_slug(
      expected_existing_head_repository
    )
    if (
      existing_head_repository.casefold() != upstream_repo.casefold()
      and existing_head_repository.split("/", 1)[0].casefold()
      != login.casefold()
    ):
      raise ContributionSubmitError(
        "This pull request branch is not owned by the connected GitHub account. "
        "Nothing was pushed."
      )
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
      if (
        existing_head_repository is not None
        and fork_slug.casefold() != existing_head_repository.casefold()
      ):
        raise ContributionSubmitError(
          "The connected GitHub fork no longer matches this pull request's "
          "verified head repository. Nothing was pushed.",
          record_patch=record_patch,
        )
      record_patch = _git_ops._record_patch_with(record_patch, {"head_repository": fork_slug})
      push_source = "HEAD"
      last_push_error = _push_topic_branch(repo, branch, push_source)
      if last_push_error:
        raise push_rejected(last_push_error, record_patch=record_patch)
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
      existing = _confirm_existing_pr_update(
        repo,
        upstream_repo,
        expected_existing_pr_number,
        expected_head_repository=existing_head_repository,
        expected_head_sha=pushed_sha,
        branch=branch,
        base_branch=submit_base,
      )
      if not existing:
        raise ContributionSubmitError(
          "The approved pull request is no longer open on this exact branch. "
          f"The reviewed branch was pushed to {pushed_branch_url}, but no new "
          "pull request was created.",
          record_patch=pushed_patch,
        )
      existing_url, existing_stage = existing
      return (
        existing_url,
        expected_existing_pr_number,
        _git_ops._record_patch_with(
          pushed_patch, {"publication_stage": existing_stage},
        ),
      )

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
        if publication_stage == "draft":
          create_args.append("--draft")
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
              _git_ops._record_patch_with(
                _git_ops._record_patch_with(
                  pushed_patch, {"publication_stage": publication_stage},
                ),
                label_patch,
              ),
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
    return (
      url,
      number,
      _git_ops._record_patch_with(
        _git_ops._record_patch_with(
          pushed_patch, {"publication_stage": publication_stage},
        ),
        label_patch,
      ),
    )
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
  if not has_full_pr_access(state.get("scopes")):
    raise ContributionSubmitError(
      "Reconnect GitHub with full PR access before approving this PR stack.",
      status_code=409,
    )
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
  saw_ambiguous_attempt = False
  for attempt in range(_PUSH_RETRIES):
    try:
      proc = _git_ops._git(
        repo,
        "push",
        f"--force-with-lease=refs/heads/{target_branch}:{expected_base}",
        remote,
        f"{landed_sha}:refs/heads/{target_branch}",
        check=False,
      )
    except (subprocess.TimeoutExpired, OSError) as exc:
      proc = None
      last_error = str(exc)
      saw_ambiguous_attempt = True
    else:
      if proc.returncode == 0:
        return
      last_error = (proc.stderr or proc.stdout or "").strip()
    # A transport can fail after GitHub has accepted the ref update. Re-read the
    # target before reporting failure or retrying: the exact landed tip is proof
    # that this compare-and-swap succeeded, while every other value remains a
    # safe failure. This mirrors submission's lost-response reconciliation and
    # prevents the ledger from reopening a stack that is already live.
    try:
      last_actual = _git_ops._upstream_branch_sha(
        repo, upstream_repo, target_branch,
      )
    except (subprocess.TimeoutExpired, OSError):
      last_actual = None
    if last_actual == landed_sha:
      return
    if proc is not None and not _is_transient_push_error(last_error):
      break
    if attempt + 1 < _PUSH_RETRIES:
      time.sleep(_PUSH_RETRY_BASE_SECONDS * (2 ** attempt))
  if last_actual is None or saw_ambiguous_attempt:
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


def _merged_parent_successor_plan(record: dict) -> dict:
  """Validate the durable merged-parent successor claim carried by one card.

  A squash/queue-merged parent leaves its reviewed child pointed at a base
  branch that no longer carries that commit. The agent re-reviews the child
  rebased onto the surviving target base; this claim records BOTH public
  mutations that reviewed successor authorizes — the exact branch rewrite and
  the base retarget — so neither can be inferred from an agent-writable ledger.
  """
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  if plan.get("action") != "pr_update":
    raise ContributionSubmitError(
      "This reviewed update is not a merged-parent successor. Nothing was pushed."
    )
  if isinstance(plan.get("stack"), dict):
    raise ContributionSubmitError(
      "A merged-parent successor must be detached from its settled stack "
      "before review. Nothing was pushed."
    )
  successor = (
    plan.get("successor") if isinstance(plan.get("successor"), dict) else {}
  )
  branch = _git_ops._validate_branch(plan.get("branch") or record.get("branch"))
  old_head_sha = str(successor.get("old_head_sha") or "").strip()
  old_base_sha = str(successor.get("old_base_sha") or "").strip()
  merged_base_sha = str(successor.get("merged_base_sha") or "").strip()
  successor_head_sha = str(plan.get("head_sha") or "").strip()
  target_base_sha = str(plan.get("base_sha") or "").strip()
  if not all(
    _GIT_SHA.fullmatch(value)
    for value in (old_head_sha, old_base_sha, successor_head_sha, target_base_sha)
  ):
    raise ContributionSubmitError(
      "This merged-parent successor is missing its exact reviewed commits. "
      "Nothing was pushed."
    )
  if merged_base_sha and not _GIT_SHA.fullmatch(merged_base_sha):
    raise ContributionSubmitError(
      "This merged-parent successor has an invalid merged-parent commit. "
      "Nothing was pushed."
    )
  old_base_branch = _git_ops._validate_branch(successor.get("old_base_branch"))
  target_base_branch = _git_ops._validate_branch(
    successor.get("base_branch") or plan.get("base_branch")
  )
  if old_base_branch == target_base_branch:
    raise ContributionSubmitError(
      "A merged-parent successor must retarget its base to a new branch. "
      "Nothing was pushed."
    )
  if old_head_sha == successor_head_sha:
    raise ContributionSubmitError(
      "A merged-parent successor must rewrite its branch to a new commit. "
      "Nothing was pushed."
    )
  return {
    "branch": branch,
    "old_head_sha": old_head_sha,
    "old_base_branch": old_base_branch,
    "old_base_sha": old_base_sha,
    "merged_base_sha": merged_base_sha or None,
    "successor_head_sha": successor_head_sha,
    "target_base_branch": target_base_branch,
    "target_base_sha": target_base_sha,
  }


def _classify_merged_parent_successor(
  journal: dict, *, live_head_sha: str, live_base_branch: str,
) -> str:
  """Decide the one safe next step purely from the live public PR facts.

  Never authorize from the ledger: the branch rewrite and base retarget are
  keyed only on what GitHub currently exposes, so a crashed run resumes exactly
  where it stopped and any drifted pull request fails closed with no mutation.

  * old head + old base -> ``push`` (rewrite the branch, then retarget)
  * new head + old base -> ``retarget`` (branch already rewritten; retarget only)
  * new head + new base -> ``settle`` (both mutations landed; ledger only)
  * anything else -> fail closed without touching anything public
  """
  live_head = str(live_head_sha or "").strip()
  live_base = str(live_base_branch or "").strip()
  old_head = journal["old_head_sha"]
  new_head = journal["successor_head_sha"]
  old_base = journal["old_base_branch"]
  new_base = journal["target_base_branch"]
  if live_head == old_head and live_base == old_base:
    return "push"
  if live_head == new_head and live_base == old_base:
    return "retarget"
  if live_head == new_head and live_base == new_base:
    return "settle"
  raise ContributionSubmitError(
    "This pull request changed after the merged-parent successor was reviewed. "
    "Nothing was pushed. Ask the agent to refresh and review it against the "
    "current pull request.",
    code="review_refresh_needed",
    detail=(
      "The live pull request head or base no longer matches the reviewed "
      "successor."
    ),
  )


def _assert_merged_parent_tree_equivalence(
  repo: Path, *, old_base_sha: str, target_base_sha: str,
  merged_base_sha: str | None = None,
) -> None:
  """Prove the merged parent reached the reviewed target base unchanged.

  Immediately after the parent lands, the target tip itself has the parent's
  exact tree. Later contributions may advance that target before the reviewed
  child is ready. In that case the durable claim identifies the exact merged
  parent commit: its tree must equal the old public parent branch, and it must
  be an ancestor of the exact target tip the successor was reviewed on. The
  successor's canonical diff is still owned by that target tip. A read that
  cannot complete fails closed rather than guessing.
  """
  merged_sha = str(merged_base_sha or target_base_sha)
  trees = []
  for sha in (old_base_sha, merged_sha):
    proc = _git_ops._git(
      repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}", check=False,
    )
    tree = (proc.stdout or "").strip()
    if proc.returncode != 0 or not _GIT_SHA.fullmatch(tree):
      raise ContributionSubmitError(
        "Could not verify that the merged parent reached the target base. "
        "Nothing was pushed.",
        status_code=503,
        code="update_unconfirmed",
        detail="The merged-parent or target-base tree could not be resolved.",
      )
    trees.append(tree)
  if trees[0] != trees[1]:
    raise ContributionSubmitError(
      "The merged parent no longer matches the parent branch that was reviewed. "
      "Nothing was pushed. Ask the agent to refresh the successor.",
      code="review_refresh_needed",
      detail="The merged commit's tree differs from the reviewed parent branch.",
    )
  if merged_base_sha:
    ancestry = _git_ops._git(
      repo,
      "merge-base",
      "--is-ancestor",
      merged_sha,
      target_base_sha,
      check=False,
    )
    if ancestry.returncode == 1:
      raise ContributionSubmitError(
        "The reviewed target base does not contain the merged parent. Nothing "
        "was pushed. Ask the agent to refresh the successor.",
        code="review_refresh_needed",
        detail="The merged parent is not an ancestor of the reviewed target base.",
      )
    if ancestry.returncode != 0:
      raise ContributionSubmitError(
        "Could not verify that the merged parent reached the target base. "
        "Nothing was pushed.",
        status_code=503,
        code="update_unconfirmed",
        detail="The merged-parent ancestry check could not complete.",
      )


def _retarget_pr_base(
  repo: Path, upstream_repo: str, number: int, *, base_branch: str,
) -> tuple[str, str]:
  """Retarget one known PR base and classify the single public attempt.

  The caller re-reads the pull request to confirm the new base, so a lost or
  ambiguous edit response never issues a second mutation: the read is the
  authority. This function deliberately attempts the mutation once.
  """
  base_branch = _git_ops._validate_branch(base_branch)
  try:
    proc = _git_ops._gh(
      repo, "pr", "edit", str(number), "-R", upstream_repo,
      "--base", base_branch, check=False,
    )
  except (subprocess.TimeoutExpired, OSError) as exc:
    return "ambiguous", str(exc)
  if proc.returncode == 0:
    return "accepted", ""
  error = (proc.stderr or proc.stdout or "").strip()
  if _is_transient_push_error(error):
    return "ambiguous", error
  return "rejected", error


def _successor_record_patch(
  journal: dict, witness: dict, *, target_base_sha: str, stage: str,
) -> dict:
  """Assemble the settled ledger patch for one detached successor."""
  return {
    **witness,
    "last_successor_base_sha": target_base_sha,
    "last_submit_base_branch": journal["target_base_branch"],
    "publication_stage": stage,
  }


def _advance_merged_parent_successor(
  record: dict,
  diff_path: Path,
  *,
  expected_number: int,
  expected_head_repository: str,
  live_head_sha: str,
  live_base_branch: str,
) -> tuple[str, int, dict]:
  """Apply one reviewed merged-parent successor to an already-open PR.

  This is the single owning primitive for a squash/queue-merged parent's child.
  It rewrites the child branch to the reviewed successor with an exact
  force-with-lease from the old public head, confirms that rewrite while the
  merged-parent base still stands, then retargets the pull request base to the
  surviving target branch and confirms the exact new head and base. Both
  mutations are recorded on the durable claim and keyed only on the live pull
  request, so every crash resumes state-by-state and any drift fails closed
  with nothing pushed. Never authorize from the ledger alone.
  """
  if not shutil.which("git") or not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs git and gh installed before it can update PRs.",
      status_code=409,
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  login = str(state.get("login") or "")
  if not token or not login:
    raise ContributionSubmitError("Connect GitHub before approving this update.", 401)
  if not has_full_pr_access(state.get("scopes")):
    raise ContributionSubmitError(
      "Reconnect GitHub with full PR access before approving this update.",
      status_code=409,
    )
  author_name, author_email = _git_ops._connected_git_identity(state, login)

  journal = _merged_parent_successor_plan(record)
  plan = record.get("plan") or {}
  upstream_repo = _git_ops._validate_repo_slug(plan.get("repo") or record.get("repo"))
  branch = journal["branch"]
  head_repository = _git_ops._validate_repo_slug(expected_head_repository)
  if (
    head_repository.casefold() != upstream_repo.casefold()
    and head_repository.split("/", 1)[0].casefold() != login.casefold()
  ):
    raise ContributionSubmitError(
      "This pull request branch is not owned by the connected GitHub account. "
      "Nothing was pushed."
    )
  repo = _safe_repo_path(plan.get("repo_path"))
  if not (repo / ".git").exists():
    raise ContributionSubmitError("The staged repo is not a git checkout.")

  old_head = journal["old_head_sha"]
  new_head = journal["successor_head_sha"]
  old_base_branch = journal["old_base_branch"]
  target_base_branch = journal["target_base_branch"]

  step = _classify_merged_parent_successor(
    journal, live_head_sha=live_head_sha, live_base_branch=live_base_branch,
  )

  same_repo = head_repository.casefold() == upstream_repo.casefold()
  witness = {
    "last_successor_old_head": old_head,
    "last_successor_base_branch": target_base_branch,
    "last_submit_push_sha": new_head,
    "last_submit_stage": "pushed",
    "head_repository": head_repository,
    "last_pushed_branch": branch if same_repo else f"{login}:{branch}",
    "last_pushed_branch_url": (
      f"https://github.com/{head_repository}/tree/{quote(branch, safe='/')}"
    ),
  }

  def confirm_state(
    expected_head_sha: str, base_branch: str, expected_base_sha: str | None,
  ):
    return _confirm_existing_pr_update(
      repo,
      upstream_repo,
      expected_number,
      expected_head_repository=head_repository,
      expected_head_sha=expected_head_sha,
      branch=branch,
      base_branch=base_branch,
      expected_base_sha=expected_base_sha,
    )

  def confirm(base_branch: str, expected_base_sha: str | None):
    return confirm_state(new_head, base_branch, expected_base_sha)

  def require_target_base(*, record_patch: dict | None = None) -> str:
    try:
      current = _git_ops._upstream_branch_sha(
        repo, upstream_repo, target_base_branch,
      )
    except (subprocess.TimeoutExpired, OSError):
      current = None
    if current is None:
      raise ContributionSubmitError(
        "GitHub could not verify the merged-parent successor's target base. "
        "Nothing further was changed.",
        status_code=503,
        code="update_unconfirmed",
        detail="The current target base branch tip could not be verified.",
        record_patch=record_patch,
      )
    if current != journal["target_base_sha"]:
      raise ContributionSubmitError(
        "The successor's target base branch moved after review. Nothing "
        "further was changed. Ask the agent to refresh the successor.",
        code="review_refresh_needed",
        detail="The target base branch tip changed from the reviewed commit.",
        record_patch=record_patch,
      )
    return current

  # Every entry and recovery state must still prove the exact reviewed local
  # branch, stored diff, base, attribution, and merge result. Public state alone
  # never substitutes for the private all-clear snapshot.
  checkout_back = None
  try:
    current_branch = _git_ops._git(
      repo, "rev-parse", "--abbrev-ref", "HEAD",
    ).stdout.strip()
    checkout_back = (
      _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
      if current_branch == "HEAD"
      else current_branch
    )
    _git_ops._git(repo, "check-ref-format", "--branch", branch)
    _git_ops._assert_clean_worktree(repo)
    _git_ops._git(repo, "checkout", "-q", branch)
    _git_ops._assert_clean_worktree(repo)
    expected_base, expected_head, _diff = _git_ops._assert_fresh(
      record, diff_path, repo, branch,
    )
    if (
      expected_base != journal["target_base_sha"]
      or expected_head != new_head
    ):
      raise ContributionSubmitError(
        "The reviewed successor branch changed after review. Nothing was "
        "changed.",
        code="review_refresh_needed",
      )
    _git_ops._assert_coauthor_trailer(repo, branch)
    _git_ops._assert_head_attribution(
      repo, branch, author_name=author_name, author_email=author_email,
    )
    _git_ops._assert_clean_worktree(repo)
    _git_ops._assert_merges_with_upstream(repo, upstream_repo, branch)
    landed_sha = _git_ops._git(repo, "rev-parse", "HEAD").stdout.strip()
    if landed_sha != new_head:
      raise ContributionSubmitError(
        "Could not verify the exact reviewed successor commit."
      )
  finally:
    if checkout_back:
      _git_ops._git(repo, "checkout", "-q", checkout_back, check=False)

  # Settle-only recovery: both public mutations already landed. Never re-push or
  # re-edit; only prove the exact new head and base, then settle the ledger.
  if step == "settle":
    target_base_sha = require_target_base(record_patch=witness)
    try:
      confirmed = confirm(target_base_branch, target_base_sha)
    except ContributionSubmitError as exc:
      raise ContributionSubmitError(
        exc.message,
        status_code=exc.status_code,
        code=exc.code or "update_unconfirmed",
        detail=exc.detail,
        record_patch=_git_ops._record_patch_with(witness, exc.record_patch),
      ) from exc
    if not confirmed:
      raise ContributionSubmitError(
        "The merged-parent successor could not be confirmed on GitHub.",
        status_code=503,
        code="update_unconfirmed",
        detail=(
          "The live pull request did not expose the reviewed successor head "
          "and base."
        ),
        record_patch=witness,
      )
    url, stage = confirmed
    return url, expected_number, _successor_record_patch(
      journal, witness, target_base_sha=target_base_sha, stage=stage,
    )

  # GitHub's pull-request ``base.sha`` is a comparison snapshot, not a lease on
  # the current base ref. Resolve the named old base directly before either
  # mutation; this also supports a parent branch that advanced after the child
  # PR was opened but whose reviewed final tree is what reached main.
  try:
    current_old_base_sha = _git_ops._upstream_branch_sha(
      repo, upstream_repo, old_base_branch,
    )
  except (subprocess.TimeoutExpired, OSError):
    current_old_base_sha = None
  if current_old_base_sha is None:
    raise ContributionSubmitError(
      "GitHub could not verify the merged-parent successor's current base. "
      "Nothing further was changed.",
      status_code=503,
      code="update_unconfirmed",
      detail="The current merged-parent branch tip could not be verified.",
      record_patch=(witness if step == "retarget" else None),
    )
  if current_old_base_sha != journal["old_base_sha"]:
    raise ContributionSubmitError(
      "The successor's current base branch moved after review. Nothing was "
      "pushed. Ask the agent to refresh the successor.",
      code="review_refresh_needed",
      detail="The pull request base no longer points at the reviewed parent commit.",
      record_patch=(witness if step == "retarget" else None),
    )

  # Both push and retarget need a verified, tree-equivalent target base.
  target_base_sha = require_target_base(
    record_patch=(witness if step == "retarget" else None),
  )
  try:
    _assert_merged_parent_tree_equivalence(
      repo,
      old_base_sha=journal["old_base_sha"],
      merged_base_sha=journal["merged_base_sha"],
      target_base_sha=target_base_sha,
    )
  except ContributionSubmitError as exc:
    if step == "retarget" and exc.code == "update_unconfirmed":
      raise ContributionSubmitError(
        exc.message,
        status_code=exc.status_code,
        code=exc.code,
        detail=exc.detail,
        record_patch=_git_ops._record_patch_with(witness, exc.record_patch),
      ) from exc
    raise

  if step == "push":
    # Re-read both public bases and the exact old PR state immediately before
    # mutation. The earlier route snapshot and local checks are not leases.
    target_base_sha = require_target_base()
    confirmed_old = confirm_state(
      old_head, old_base_branch, journal["old_base_sha"],
    )
    if not confirmed_old:
      raise ContributionSubmitError(
        "The pull request changed while its reviewed successor was being "
        "checked. Nothing was pushed.",
        status_code=503,
        code="update_unconfirmed",
        detail="The exact old head and base could not be confirmed before push.",
      )
    # Public mutation #1: exact force-with-lease from the old public head to
    # the reviewed successor. A moved lease fails closed inside this call.
    try:
      _push_stack_tip_with_lease(
        repo,
        upstream_repo=head_repository,
        target_branch=branch,
        expected_base=old_head,
        landed_sha=new_head,
      )
    except ContributionSubmitError as exc:
      if exc.code == "landing_unconfirmed":
        raise ContributionSubmitError(
          exc.message,
          status_code=exc.status_code,
          code=exc.code,
          detail=exc.detail,
          record_patch=witness,
        ) from exc
      raise
    # Confirm the rewrite while the merged-parent base still stands, before
    # any retarget. A base that already moved here is drift, not this action.
    try:
      confirmed_pre = confirm(old_base_branch, journal["old_base_sha"])
    except ContributionSubmitError as exc:
      raise ContributionSubmitError(
        exc.message,
        status_code=exc.status_code,
        code=exc.code or "update_unconfirmed",
        detail=exc.detail,
        record_patch=_git_ops._record_patch_with(witness, exc.record_patch),
      ) from exc
    if not confirmed_pre:
      raise ContributionSubmitError(
        "The reviewed successor branch was pushed, but GitHub did not confirm "
        "the rewrite before the base retarget.",
        status_code=503,
        code="update_unconfirmed",
        detail=(
          "The live pull request did not expose the successor head on the "
          "merged-parent base."
        ),
        record_patch=witness,
      )
  # Re-read the target and both possible exact PR states immediately before the
  # second mutation. If an earlier ambiguous edit actually landed, settle it;
  # if the PR is still conclusively on the reviewed old base, one new attempt is
  # safe. Any other state fails closed without another edit.
  target_base_sha = require_target_base(record_patch=witness)
  try:
    confirmed = confirm(target_base_branch, target_base_sha)
  except ContributionSubmitError as exc:
    raise ContributionSubmitError(
      exc.message,
      status_code=exc.status_code,
      code=exc.code or "update_unconfirmed",
      detail=exc.detail,
      record_patch=_git_ops._record_patch_with(witness, exc.record_patch),
    ) from exc
  if confirmed:
    url, stage = confirmed
    return url, expected_number, _successor_record_patch(
      journal, witness, target_base_sha=target_base_sha, stage=stage,
    )
  try:
    confirmed_old = confirm(old_base_branch, journal["old_base_sha"])
  except ContributionSubmitError as exc:
    raise ContributionSubmitError(
      exc.message,
      status_code=exc.status_code,
      code=exc.code or "update_unconfirmed",
      detail=exc.detail,
      record_patch=_git_ops._record_patch_with(witness, exc.record_patch),
    ) from exc
  if not confirmed_old:
    raise ContributionSubmitError(
      "The reviewed successor branch is live, but its current base could not "
      "be confirmed. Nothing further was changed.",
      status_code=503,
      code="update_unconfirmed",
      detail="Neither the reviewed old nor target base state was authoritative.",
      record_patch=witness,
    )

  # Public mutation #2: retarget the base to the surviving branch, then confirm
  # the exact new head and base. Each request attempts the edit once, and only
  # after the authoritative read above proves it still has not landed.
  edit_result, edit_error = _retarget_pr_base(
    repo, upstream_repo, expected_number, base_branch=target_base_branch,
  )
  try:
    confirmed = confirm(target_base_branch, target_base_sha)
  except ContributionSubmitError as exc:
    raise ContributionSubmitError(
      exc.message,
      status_code=exc.status_code,
      code=exc.code or "update_unconfirmed",
      detail=exc.detail,
      record_patch=_git_ops._record_patch_with(witness, exc.record_patch),
    ) from exc
  if not confirmed:
    if edit_result in {"accepted", "ambiguous"}:
      raise ContributionSubmitError(
        "The reviewed successor branch is live, but GitHub did not confirm the "
        "base retarget.",
        status_code=503,
        code="update_unconfirmed",
        detail=(
          "The pull request did not expose the retargeted base after the edit."
          if edit_result == "accepted"
          else "GitHub's response to the base retarget was ambiguous."
        ),
        record_patch=witness,
      )
    raise ContributionSubmitError(
      "The reviewed successor branch is live, but its base could not be "
      "retargeted. Nothing else was changed.",
      code="review_refresh_needed",
      detail=(edit_error[:300] if edit_error else "The base retarget was rejected."),
      record_patch=witness,
    )
  url, stage = confirmed
  return url, expected_number, _successor_record_patch(
    journal, witness, target_base_sha=target_base_sha, stage=stage,
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
  if not has_full_pr_access(state.get("scopes")):
    raise ContributionSubmitError(
      "Reconnect GitHub with full PR access before landing this PR stack.",
      status_code=409,
    )

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
