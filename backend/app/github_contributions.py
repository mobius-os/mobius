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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
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

_PERSONAL_PUBLIC_INPUT_FIELDS = (
  "id", "type", "status", "repo", "branch", "title", "url", "number",
  "head_repository", "publication_stage", "submission_mode", "submitter",
  "plan", "quality_review",
)
_PREPARED_PR_ACTIONS = frozenset(("pr", "pr_update"))


def _exact_reviewed_pr_text(record: dict) -> tuple[str, str]:
  """Return GitHub text only when it can be published byte-for-byte.

  GitHub normalizes surrounding title whitespace and rejects oversized text.
  Reject those inputs during the private preflight rather than silently
  changing the review at publication time.
  """
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  title = plan.get("title") if "title" in plan else record.get("title")
  body = plan.get("body_draft")
  if (
    not isinstance(title, str)
    or not title
    or title != title.strip()
    or "\n" in title
    or "\r" in title
    or "\x00" in title
    or len(title) > 256
  ):
    raise ContributionSubmitError(
      "This prepared PR has a title GitHub cannot publish exactly. Ask the "
      "agent to prepare a nonblank single-line title without surrounding "
      "whitespace."
    )
  if (
    not isinstance(body, str)
    or not body.strip()
    or "\x00" in body
    or len(body.encode("utf-8")) > 65_536
  ):
    raise ContributionSubmitError(
      "This prepared PR has a body GitHub cannot publish exactly. Ask the "
      "agent to prepare a nonblank body within GitHub's size limit."
    )
  return title, body


def _personal_publication_input(record: dict) -> dict:
  """Canonical app-writable inputs consumed by one personal publication."""
  return {
    key: record.get(key)
    for key in _PERSONAL_PUBLIC_INPUT_FIELDS
  }


def _personal_publication_input_sha256(record: dict) -> str:
  return hashlib.sha256(json.dumps(
    _personal_publication_input(record),
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
  ).encode("utf-8")).hexdigest()


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
  try:
    _git_ops._canonical_reviewed_oid(plan.get("base_sha"), "base sha")
    head_sha = _git_ops._canonical_reviewed_oid(
      plan.get("head_sha"), "head sha",
    )
    reviewed_head_sha = _git_ops._canonical_reviewed_oid(
      review.get("reviewed_head_sha"), "quality-review head sha",
    )
    canonical = True
  except ContributionSubmitError:
    head_sha = reviewed_head_sha = ""
    canonical = False
  if (
    not canonical
    or review.get("state") != "all_clear"
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
class PublicReconciliation:
  """Authoritative exact public state for one reviewed publication retry."""

  head_repository: str
  pr_url: str | None = None
  pr_number: int | None = None
  publication_stage: str | None = None


@dataclass(frozen=True)
class PublicationHandoffIdentity:
  """Ledger-owned routing identity for one app publication handoff.

  This shape is deliberately weaker than :class:`PublicationHandoffSpec`: it
  is enough to settle an already-true connection in the private ledger, but
  never enough to install or update source.  Source and package proof stay
  mandatory for any operation that would mutate the app itself.
  """

  contribution_id: str
  target_app_id: int
  repo_slug: str
  manifest_url: str
  manifest_id: str


@dataclass(frozen=True)
class PublicationHandoffSpec(PublicationHandoffIdentity):
  """Immutable reviewed inputs for connecting one published local app."""

  source_repo: Path
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


def publication_handoff_identity(record: dict) -> PublicationHandoffIdentity:
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
  return PublicationHandoffIdentity(
    contribution_id=contribution_id,
    target_app_id=target_app_id,
    repo_slug=repo_slug,
    manifest_url=manifest_url,
    manifest_id=parts[1].removeprefix("app-"),
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

  identity = publication_handoff_identity(record)
  plan = record["plan"]
  target = (
    db.query(models.App)
    .filter(
      models.App.id == identity.target_app_id,
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

  previous_id = reviewed_manifest.get("previous_id")
  if (
    manifest_id != identity.manifest_id
    or target.slug not in {manifest_id, previous_id}
  ):
    raise ContributionSubmitError(
      "The reviewed package belongs to a different local app."
    )
  if target.manifest_url is not None and not install._catalog_identity_matches(
    target.manifest_url, identity.manifest_url, manifest_id,
  ):
    raise ContributionSubmitError(
      "This installed app is already connected to a different package."
    )

  return PublicationHandoffSpec(
    **asdict(identity),
    source_repo=source_repo,
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
  """Durable owner source allowed to own private provenance refs.

  Installed apps and the live platform remain the ordinary sources. A direct
  primary checkout under ``/data/worktrees`` is the standalone-project
  adapter: it is durable, owner-controlled, and cannot be a disposable review
  checkout or a linked worktree masquerading as its own source.
  """
  if not isinstance(raw, str) or not raw:
    raise ContributionSubmitError(
      "This review is missing its durable source checkout."
    )
  try:
    repo = Path(raw).resolve()
  except (OSError, RuntimeError):
    raise ContributionSubmitError(
      "The contribution source path is invalid."
    ) from None
  data_dir = Path(get_settings().data_dir).resolve()
  platform = data_dir / "platform"
  apps = data_dir / "apps"
  worktrees = data_dir / "worktrees"
  standalone = repo.parent == worktrees
  if repo != platform and not repo.is_relative_to(apps) and not standalone:
    raise ContributionSubmitError(
      "The contribution source must be the live platform, an installed app, "
      "or a direct checkout under /data/worktrees."
    )
  if not app_git.is_repo(repo):
    raise ContributionSubmitError(
      "The contribution source is no longer a Git-backed project."
    )
  if standalone and app_git.primary_worktree_path(repo) is not None:
    raise ContributionSubmitError(
      "A standalone contribution source must be its primary checkout, not a "
      "linked review worktree."
    )
  return repo


def _equivalence_source_repo(record: dict) -> tuple[Path, Path] | None:
  """Return ``(durable source, review checkout)`` for one contribution."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  review_repo = _safe_repo_path(plan.get("repo_path"))
  raw_source_repo = plan.get("source_repo_path")
  if raw_source_repo:
    return _safe_equivalence_source_path(raw_source_repo), review_repo
  primary = app_git.primary_worktree_path(review_repo)
  if primary is not None:
    return _safe_equivalence_source_path(str(primary)), review_repo
  # Legacy prepared records sometimes used the live source checkout
  # directly rather than a linked worktree. It is already under the stricter
  # apps/platform allowlist, so it can safely own the witness itself.
  try:
    return _safe_equivalence_source_path(str(review_repo)), review_repo
  except ContributionSubmitError:
    return None


@dataclass(frozen=True)
class _PendingEquivalenceSpec:
  source_repo: Path
  review_repo: Path
  base_sha: str
  head_sha: str
  diff_sha256: str
  contribution_id: str
  captured_source_sha: str
  review_identity_sha256: str
  source_candidates: tuple[str, ...]
  source_projection: app_git.PublicationSourceProjection | None


_REVIEWED_SOURCE_IDENTITY_VERSION = 1


def _reviewed_source_identity(record: dict) -> str:
  """Return the canonical identity for one continuity-review envelope.

  The agent request carries this SHA-256 after reading the locked record. The
  host recomputes it while holding the record lock, then stores it only inside
  a private immutable Git ref. App-writable JSON therefore selects what the
  agent must review but cannot create publication authority by itself.

  Version 1 hashes canonical UTF-8 JSON with sorted keys and compact
  separators. The envelope freezes the fields that define source-chat
  authority, the private review verdict, and every reviewed/public PR input;
  operational mirrors such as the prepared-to-submitting lifecycle claim,
  timestamps, checks, and Autopilot state are intentionally outside it so they
  can advance without invalidating source continuity.
  """
  material = {
    "version": _REVIEWED_SOURCE_IDENTITY_VERSION,
    "record": {
      key: record.get(key)
      for key in (
        "id", "type", "repo", "title", "branch",
        "chat_id", "chat_ids", "plan", "quality_review",
      )
    },
  }
  return hashlib.sha256(json.dumps(
    material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  ).encode("utf-8")).hexdigest()


def _pending_equivalence_spec(record: dict) -> _PendingEquivalenceSpec | None:
  """Resolve the immutable inputs shared by preview and witness creation."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repos = _equivalence_source_repo(record)
  if repos is None:
    return None
  source_repo, review_repo = repos
  base_sha = str(plan.get("base_sha") or "")
  head_sha = str(plan.get("head_sha") or "")
  try:
    source_projection = app_git.publication_source_projection(
      plan.get("source_projection"),
      repo_slug=str(plan.get("repo") or record.get("repo") or ""),
    )
  except ValueError:
    return None
  # Older cleanup could remove a linked review worktree before the merged-state
  # poll created its durable witness. The reviewed commits remain in the live
  # repository, but only reuse it when both immutable commits still resolve.
  if (
    not app_git.ref_exists(review_repo, f"{base_sha}^{{commit}}")
    or not app_git.ref_exists(review_repo, f"{head_sha}^{{commit}}")
  ) and (
    app_git.ref_exists(source_repo, f"{base_sha}^{{commit}}")
    and app_git.ref_exists(source_repo, f"{head_sha}^{{commit}}")
  ):
    review_repo = source_repo

  try:
    current_source = app_git.head_sha(source_repo, "HEAD")
    dirty = app_git.worktree_dirty(source_repo)
  except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
    current_source = ""
    dirty = True
  # Send must prove the source that is installed *now*. Its clean committed
  # HEAD is the only source-of-truth candidate. Falling back to the captured
  # preparation SHA when HEAD is unreadable would publish against stale source.
  # Dirty working bytes are equally unprovable: the app serves those bytes, not
  # merely HEAD, so force them through the normal commit + review path first.
  candidates = (current_source,) if current_source and not dirty else ()
  return _PendingEquivalenceSpec(
    source_repo=source_repo,
    review_repo=review_repo,
    base_sha=base_sha,
    head_sha=head_sha,
    diff_sha256=str(plan.get("diff_sha256") or ""),
    contribution_id=str(record.get("id") or ""),
    captured_source_sha=str(plan.get("source_sha") or "").lower(),
    review_identity_sha256=_reviewed_source_identity(record),
    source_candidates=candidates,
    source_projection=source_projection,
  )


def _prepublication_source_continuity(
  spec: _PendingEquivalenceSpec,
  current_source_sha: str,
) -> app_git.ReviewedSourceContinuity | None:
  return app_git.prepublication_source_continuity(
    spec.source_repo,
    base_sha=spec.base_sha,
    head_sha=spec.head_sha,
    source_sha=spec.captured_source_sha,
    current_source_sha=current_source_sha,
    diff_sha256=spec.diff_sha256,
    contribution_id=spec.contribution_id,
    review_identity_sha256=spec.review_identity_sha256,
    source_projection=spec.source_projection,
  )


def _record_prepublication_source_continuity(
  record: dict,
  *,
  reviewed_through_sha: str,
  source_resolution_sha256: str | None = None,
) -> str | None:
  """Create one private continuity witness from the locked clean source."""
  spec = _pending_equivalence_spec(record)
  if (
    spec is None
    or spec.source_candidates != (reviewed_through_sha,)
  ):
    return None
  return app_git.record_prepublication_source_continuity(
    spec.source_repo,
    base_sha=spec.base_sha,
    head_sha=spec.head_sha,
    source_sha=spec.captured_source_sha,
    reviewed_through_sha=reviewed_through_sha,
    diff_sha256=spec.diff_sha256,
    contribution_id=spec.contribution_id,
    review_identity_sha256=spec.review_identity_sha256,
    review_source_dir=spec.review_repo,
    source_projection=spec.source_projection,
    source_resolution_sha256=source_resolution_sha256,
  )


def _assert_pending_equivalence_preflight(record: dict) -> str:
  """Require one read-only proof that Send can later persist as a witness."""
  spec = _pending_equivalence_spec(record)
  if spec is None:
    raise ContributionSubmitError(
      "This review is missing its durable source provenance.",
      code="missing_source_provenance",
    )
  for source_sha in spec.source_candidates:
    proof_mode = app_git.preview_pending_equivalent_change(
      spec.source_repo,
      base_sha=spec.base_sha,
      head_sha=spec.head_sha,
      source_sha=source_sha,
      diff_sha256=spec.diff_sha256,
      review_source_dir=spec.review_repo,
      source_projection=spec.source_projection,
    )
    if proof_mode is not None:
      return proof_mode
    continuity = _prepublication_source_continuity(spec, source_sha)
    if continuity is not None:
      return (
        "reviewed_source_resolution" if continuity.source_resolution_sha256
        else "reviewed_source_continuity"
      )
  raise ContributionSubmitError(
    "The durable source no longer proves that it contains this reviewed change.",
    code="source_provenance_mismatch",
  )


def _assert_pending_equivalence_before_publication(record: dict) -> str:
  """Prove a first push or recognize an exact public reconciliation.

  Contribution JSON is app-writable, so its ``last_submit_*`` journal can only
  select a candidate recovery; it can never authorize one. A retry skips the
  local-source proof only after GitHub itself confirms the exact reviewed
  head on the exact open PR or remote branch. A definitely rejected push has
  no ``pushed`` stage, and a forged/stale journal has no matching public proof,
  so both take the strict live-source path.
  """
  if _authoritative_public_reconciliation(record) is not None:
    return "public_reconciliation"
  return _assert_pending_equivalence_preflight(record)


def _record_pending_equivalence(record: dict) -> str | None:
  """Persist the reviewed local-history witness after the owner sends a PR.

  Linked review worktrees derive their primary live checkout automatically.
  Standalone app review clones carry an explicit ``plan.source_repo_path``;
  :mod:`app_git` copies only the verified reviewed commits into that installed
  repo before recording the witness. The current clean installed HEAD observed
  under the source lock is the only state eligible for a new witness.
  """
  spec = _pending_equivalence_spec(record)
  if spec is None:
    return None
  kwargs = {
    "base_sha": spec.base_sha,
    "head_sha": spec.head_sha,
    "diff_sha256": spec.diff_sha256,
    "contribution_id": spec.contribution_id,
    "review_source_dir": spec.review_repo,
    "source_projection": spec.source_projection,
  }
  for source_sha in spec.source_candidates:
    recorded = app_git.record_pending_equivalent_change(
      spec.source_repo, source_sha=source_sha, **kwargs,
    )
    if recorded is not None:
      app_git.discard_prepublication_source_continuity(
        spec.source_repo, spec.contribution_id,
      )
      return recorded
    continuity = _prepublication_source_continuity(spec, source_sha)
    if continuity is None:
      continue
    if continuity.source_resolution_sha256:
      recorded = app_git.record_reviewed_source_equivalence(
        spec.source_repo, witness=continuity,
      )
    else:
      recorded = app_git.record_pending_equivalent_change(
        spec.source_repo, source_sha=continuity.source_sha, **kwargs,
      )
    if recorded is not None:
      app_git.discard_prepublication_source_continuity(
        spec.source_repo, spec.contribution_id,
      )
      return recorded
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
  allow_personal_resume: bool = False,
  before_claim_write: Callable[[dict, Path, bool, dict], None] | None = None,
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
  resumable_personal = (
    allow_personal_resume
    and record.get("status") == "submitting"
    and expected_action in _PREPARED_PR_ACTIONS
    and record.get("submitter") == submitter
  )
  if (
    record.get("status") != "prepared"
    and not resumable_successor
    and not resumable_personal
  ):
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
  if resumable_successor or resumable_personal:
    claimed = {
      **record,
      "personal_submit_input_sha256": _personal_publication_input_sha256(record),
    }
    if before_claim_write is not None:
      before_claim_write(claimed, record_path, True, record)
    _write_record(record_path, claimed)
    return claimed, record_path, diff_path
  _require_all_clear_review(record)
  now = _now_iso()
  claimed = {
    **record,
    "status": "submitting",
    "submitter": submitter,
    "submit_started_at": now,
    "updated_at": now,
  }
  claimed["personal_submit_input_sha256"] = (
    _personal_publication_input_sha256(claimed)
  )
  if before_claim_write is not None:
    before_claim_write(claimed, record_path, False, record)
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

  Publishing a new stack keeps the narrow ``pr`` default. Callers that execute
  one explicit phase may additionally admit ``pr_update`` as structural
  context; only ``prepared_actions`` are claimed by the endpoint.
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
  deferred_prepared_actions: frozenset[str] = frozenset(),
  submitter: str = "contribute-stack-button",
  already_detail: str = "Every PR in this stack has already been submitted.",
  before_claim_write: Callable[[dict, Path, bool, dict], None] | None = None,
) -> list[dict]:
  """Claim only the current public phase of one complete reviewed stack.

  ``deferred_prepared_actions`` remain byte-for-byte prepared. They are still
  validated as part of the ordered chain but receive no attempt owner and no
  public authority from this call.
  """
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
  claimable_actions = (
    prepared_actions if prepared_actions is not None else allowed_actions
  )
  private_actions = [
    str((item["record"].get("plan") or {}).get("action") or "")
    for item in validated
    if item["record"].get("status") in {"prepared", "submitting"}
  ]
  if private_actions:
    phase_action = private_actions[0]
    saw_create = False
    for action in private_actions:
      if action == "pr":
        saw_create = True
      elif action == "pr_update" and saw_create:
        raise HTTPException(
          status_code=409,
          detail=(
            "An existing pull-request update cannot follow a new private "
            "stack layer."
          ),
        )
    if phase_action not in claimable_actions:
      raise HTTPException(
        status_code=409,
        detail="A private stack layer is prepared for a different public action.",
      )
  by_id = {row["record"]["id"]: row for row in rows}
  for item in validated:
    if item["record"].get("status") == "prepared":
      plan = item["record"].get("plan") or {}
      action = str(plan.get("action") or "")
      if action in deferred_prepared_actions:
        continue
      if action not in claimable_actions:
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
    resumed = record.get("status") == "submitting"
    if (
      record.get("status") == "prepared"
      and str((record.get("plan") or {}).get("action") or "")
          in claimable_actions
    ):
      record = {
        **record,
        "status": "submitting",
        "submitter": submitter,
        "submit_started_at": now,
        "updated_at": now,
      }
      record["personal_submit_input_sha256"] = (
        _personal_publication_input_sha256(record)
      )
      if before_claim_write is not None:
        before_claim_write(record, row["record_path"], False, row["record"])
      _write_record(row["record_path"], record)
    elif resumed and before_claim_write is not None:
      before_claim_write(record, row["record_path"], True, row["record"])
    ordered.append({
      **row, "record": record, "stack": item["stack"], "resumed": resumed,
    })
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
  """Settle an owner-approved update of an already-open PR.

  Standalone and stack-root updates remain fast-forward-only. A reviewed stack
  child may instead be restacked under exact public-head and parent leases.

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


def _authoritative_public_reconciliation(
  record: dict,
  *,
  base_branch: str | None = None,
  same_repo: bool | None = None,
  expected_pr_number: int | None = None,
  expected_head_repository: str | None = None,
) -> PublicReconciliation | None:
  """Return an exact public PR/branch recovery authenticated by GitHub.

  The contribution ledger is deliberately not a trust anchor: an app or agent
  can rewrite it. Its post-push journal only makes this bounded read eligible;
  the returned PR must independently match the reviewed commit, branch, target
  base, and connected owner's head repository. No remote branch mutation occurs
  here, so a lost create/update response can reconcile after local source moves
  without turning forged journal fields into publication authority.
  """
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  reviewed_head = str(plan.get("head_sha") or "").strip().lower()
  reviewed_metadata = None
  if plan.get("action") == "pr_update":
    try:
      reviewed_metadata = _reviewed_existing_pr_metadata(record)
    except ContributionSubmitError:
      return None
  journal_head = str(record.get("last_submit_push_sha") or "").strip().lower()
  if (
    record.get("last_submit_stage") not in {
      "pushed", "push_pending", "push_ambiguous",
    }
    or not _GIT_SHA.fullmatch(reviewed_head)
    or journal_head != reviewed_head
  ):
    return None

  state = github_auth.read_state() or {}
  login = str(state.get("login") or "").strip()
  if not github_auth.get_token() or not login or not has_full_pr_access(
    state.get("scopes")
  ):
    return None

  try:
    repo = _safe_repo_path(plan.get("repo_path"))
    upstream_repo = _git_ops._validate_repo_slug(
      plan.get("repo") or record.get("repo")
    )
    branch = _git_ops._validate_branch(
      plan.get("branch") or record.get("branch")
    )
    stack = plan.get("stack") if isinstance(plan.get("stack"), dict) else {}
    target_base = _git_ops._validate_branch(
      base_branch
      or record.get("last_submit_base_branch")
      or record.get("last_submit_upstream_branch")
      or stack.get("base_branch")
    )
    head_repository = _git_ops._validate_repo_slug(
      expected_head_repository or record.get("head_repository")
    )
  except (ContributionSubmitError, TypeError, ValueError):
    return None
  if not (repo / ".git").exists():
    return None

  inferred_same_repo = head_repository.casefold() == upstream_repo.casefold()
  if same_repo is not None and bool(same_repo) != inferred_same_repo:
    return None
  expected_owner = (
    upstream_repo.split("/", 1)[0] if inferred_same_repo else login
  )
  if head_repository.split("/", 1)[0].casefold() != expected_owner.casefold():
    return None

  number = expected_pr_number
  if number is None:
    try:
      number = int(record.get("number"))
    except (TypeError, ValueError):
      number = 0
  if number and number > 0:
    reviewed_title, reviewed_body = reviewed_metadata or (None, None)
    confirmed = _confirm_existing_pr_update(
      repo,
      upstream_repo,
      number,
      expected_head_repository=head_repository,
      expected_head_sha=reviewed_head,
      branch=branch,
      base_branch=target_base,
      expected_title=reviewed_title,
      expected_body=reviewed_body,
    )
    if confirmed is not None:
      url, stage = confirmed
      return PublicReconciliation(
        head_repository=head_repository,
        pr_url=url,
        pr_number=number,
        publication_stage=stage,
      )
    # The known PR endpoint can briefly lag an accepted branch update. The
    # exact remote branch is still enough to suppress a duplicate push, while
    # the ordinary known-PR confirmation below remains responsible for the
    # final success result. Never infer this from the local journal alone.
    try:
      remote_head = _git_ops._upstream_branch_sha(
        repo, head_repository, branch,
      )
    except (subprocess.TimeoutExpired, OSError):
      return None
    if remote_head == reviewed_head:
      return PublicReconciliation(head_repository=head_repository)
    return None

  url = _find_existing_pr(
    repo,
    upstream_repo,
    login,
    branch,
    expected_head_sha=reviewed_head,
    base_branch=target_base,
    same_repo=inferred_same_repo,
  )
  parsed_number = _parse_pr_number(url or "")
  if url and parsed_number is not None:
    reviewed_title, reviewed_body = reviewed_metadata or (None, None)
    confirmed = _confirm_existing_pr_update(
      repo,
      upstream_repo,
      parsed_number,
      expected_head_repository=head_repository,
      expected_head_sha=reviewed_head,
      branch=branch,
      base_branch=target_base,
      expected_title=reviewed_title,
      expected_body=reviewed_body,
    )
    if confirmed is None or confirmed[0] != url:
      return None
    return PublicReconciliation(
      head_repository=head_repository,
      pr_url=url,
      pr_number=parsed_number,
      publication_stage=confirmed[1],
    )

  # A push can succeed while PR creation loses its response before GitHub has
  # an open PR to list. The exact remote branch tip is sufficient authority to
  # skip a second push, but never to ignore an existing open/merged PR.
  try:
    conflict = _existing_branch_pr(
      repo,
      upstream_repo,
      login,
      branch,
      same_repo=inferred_same_repo,
    )
  except ContributionSubmitError:
    return None
  if conflict is not None:
    return None
  try:
    remote_head = _git_ops._upstream_branch_sha(
      repo, head_repository, branch,
    )
  except (subprocess.TimeoutExpired, OSError):
    return None
  if remote_head != reviewed_head:
    return None
  return PublicReconciliation(head_repository=head_repository)


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
  expected_title: str | None = None,
  expected_body: str | None = None,
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
    or ((expected_title is None) != (expected_body is None))
  ):
    return None
  args = ("api", f"repos/{upstream_repo}/pulls/{number}")
  moved_reviewed_base = False
  moved_reviewed_metadata = False
  unconfirmed_reviewed_base = False
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
      exact_identity = (
        live.get("state") == "open"
        and head_repo.get("full_name") == expected_head_repository
        and head.get("ref") == branch
        and head.get("sha") == expected_head_sha
        and base.get("ref") == base_branch
        and isinstance(live.get("draft"), bool)
        and isinstance(url, str)
        and url == f"https://github.com/{upstream_repo}/pull/{number}"
      )
      exact_metadata = (
        expected_title is None
        or (
          live.get("title") == expected_title
          and live.get("body") == expected_body
        )
      )
      if exact_identity and not exact_metadata:
        moved_reviewed_metadata = True
      exact_pr = exact_identity and exact_metadata
      current_base_sha = None
      if exact_pr and expected_base_sha is not None:
        try:
          current_base_sha = _git_ops._upstream_branch_sha(
            repo, upstream_repo, base_branch,
          )
        except (ContributionSubmitError, subprocess.TimeoutExpired, OSError):
          current_base_sha = None
        if current_base_sha is None:
          unconfirmed_reviewed_base = True
      if (
        exact_pr
        and expected_base_sha is not None
        and current_base_sha is not None
        and current_base_sha != expected_base_sha
      ):
        moved_reviewed_base = True
      elif exact_pr and (
        expected_base_sha is None or current_base_sha == expected_base_sha
      ):
        return url, ("draft" if bool(live.get("draft")) else "ready")
    if attempt + 1 < _PR_VISIBILITY_RETRIES:
      time.sleep(_PR_VISIBILITY_RETRY_BASE_SECONDS * (2 ** attempt))
  if moved_reviewed_metadata:
    raise ContributionSubmitError(
      "The pull request title or body changed during publication validation. "
      "Refresh and review the current public text before trying again.",
      code="review_refresh_needed",
      detail="The live pull request text differs from the exact reviewed witness.",
    )
  if moved_reviewed_base:
    raise ContributionSubmitError(
      "The reviewed child branch was pushed, but its parent branch moved before "
      "GitHub confirmed the update.",
      code="review_refresh_needed",
      detail="The pull request's base branch moved from the reviewed parent commit.",
    )
  if unconfirmed_reviewed_base:
    raise ContributionSubmitError(
      "The reviewed child branch was pushed, but GitHub did not confirm its "
      "current parent branch.",
      status_code=503,
      code="update_unconfirmed",
      detail="The current base branch tip could not be verified after the push.",
    )
  return None


def _reviewed_existing_pr_metadata(record: dict) -> tuple[str, str]:
  """Return the exact public text witnessed by one reviewed PR update."""
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  title, body = _exact_reviewed_pr_text(record)
  metadata = plan.get("pr_metadata")
  if (
    plan.get("action") != "pr_update"
    or plan.get("title") != title
    or not isinstance(metadata, dict)
    or metadata.get("old_title") != title
    or metadata.get("old_body") != body
  ):
    raise ContributionSubmitError(
      "This pull request update is missing its exact reviewed public title/body "
      "witness. Nothing was pushed. Ask the agent to refresh the review.",
      code="review_refresh_needed",
    )
  return title, body


def _assert_reviewed_existing_pr_metadata(
  record: dict, *, live_title: object, live_body: object,
) -> str:
  """Require exact reviewed text before mutating an existing PR branch.

  GitHub does not expose a conditional title/body update. Reading old text and
  then editing would therefore race maintainers, so Contribute never edits an
  existing PR's metadata. The reviewed old-text witness and desired text must
  be byte-identical and already public.
  """
  desired = _reviewed_existing_pr_metadata(record)
  live = (
    live_title if isinstance(live_title, str) else None,
    live_body if isinstance(live_body, str) else None,
  )
  if live == desired:
    return "desired"
  raise ContributionSubmitError(
    "This pull request's title or body does not match the reviewed update. "
    "Nothing was pushed. Ask the agent to refresh and review the current PR text.",
    code="review_refresh_needed",
    detail="The live pull request text differs from the exact reviewed desired text.",
  )


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
  try:
    head_sha = _git_ops._canonical_reviewed_oid(
      record.get("last_submit_push_sha"), "published head sha",
    )
  except ContributionSubmitError:
    head_sha = ""
  if number <= 0 or url != expected_url or not head_sha:
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
  try:
    _git_ops._canonical_reviewed_oid(plan.get("base_sha"), "base sha")
    plan_head = _git_ops._canonical_reviewed_oid(
      plan.get("head_sha"), "head sha",
    )
    reviewed_head = _git_ops._canonical_reviewed_oid(
      review.get("reviewed_head_sha"), "quality-review head sha",
    )
    normalized_from_raw = plan.get("attribution_normalized_from")
    normalized_from = (
      _git_ops._canonical_reviewed_oid(
        normalized_from_raw, "pre-normalization head sha",
      )
      if normalized_from_raw else ""
    )
  except ContributionSubmitError as exc:
    raise HTTPException(status_code=409, detail=exc.message) from exc
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
  expected_remote_sha: str | None = None,
) -> str | None:
  """Push once behind the exact remote tip observed before the intent.

  A transport failure is ambiguous: retrying before an authoritative read can
  turn an accepted first request into a misleading lease rejection. The
  caller probes GitHub and resumes from its signed intent instead.
  """
  if expected_remote_sha is not None:
    _git_ops._canonical_reviewed_oid(expected_remote_sha, "remote branch head")
  lease = f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha or ''}"
  proc = _git_ops._git(
    repo, "push", lease, remote, f"{source}:refs/heads/{branch}", check=False,
  )
  if proc.returncode == 0:
    return None
  return (proc.stderr or proc.stdout or "").strip() or "Git push failed."


def _push_topic_branch(
  repo: Path,
  branch: str,
  source: str = "HEAD",
  expected_remote_sha: str | None = None,
) -> str | None:
  """Push a reviewed topic to the owner's configured fork remote."""
  return _push_branch(repo, "fork", branch, source, expected_remote_sha)


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


def _recover_normalized_attribution(
  record: dict,
  repo: Path,
  branch: str,
  request: dict,
) -> dict:
  """Accept only the exact deterministic commit named before ref mutation."""
  old_head = str(request.get("previous_head_sha") or "").lower()
  expected_head = str(
    request.get("expected_normalized_head_sha") or ""
  ).lower()
  if (
    not _GIT_SHA.fullmatch(old_head)
    or not _GIT_SHA.fullmatch(expected_head)
  ):
    raise ContributionSubmitError("The attribution recovery receipt is incomplete.")
  current_head = _git_ops._head_commit_metadata(repo, branch)["sha"].lower()
  if current_head == old_head:
    return {}
  if current_head != expected_head:
    raise ContributionSubmitError(
      "The branch changed during attribution recovery; nothing was sent."
    )
  return _git_ops._head_sha_patch(record, old_head, current_head)


def _submit_prepared_pr(
  record: dict,
  diff_path: Path,
  *,
  direct_base_branch: str | None = None,
  expected_existing_pr_number: int | None = None,
  expected_existing_head_repository: str | None = None,
  expected_existing_head_sha: str | None = None,
  expected_existing_base_sha: str | None = None,
  existing_branch_lease_sha: str | None = None,
  publication_stage: str = "draft",
  attempt_event: Callable[[str, dict, dict | None], None] | None = None,
  prior_attempt_phase: str | None = None,
  prior_attempt_receipt: dict | None = None,
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
  existing_base_sha = None
  branch_lease_sha = None
  reviewed_existing_metadata = None
  if expected_existing_pr_number is not None:
    if expected_existing_head_repository is None:
      raise ContributionSubmitError(
        "This existing pull request update is missing its verified head repository."
      )
    existing_head_repository = _git_ops._validate_repo_slug(
      expected_existing_head_repository
    )
    expected_existing_head_sha = _git_ops._canonical_reviewed_oid(
      expected_existing_head_sha, "existing pull request head sha",
    )
    reviewed_existing_metadata = _reviewed_existing_pr_metadata(record)
    if (
      existing_head_repository.casefold() != upstream_repo.casefold()
      and existing_head_repository.split("/", 1)[0].casefold()
      != login.casefold()
    ):
      raise ContributionSubmitError(
        "This pull request branch is not owned by the connected GitHub account. "
        "Nothing was pushed."
      )
    if existing_branch_lease_sha is not None:
      branch_lease_sha = str(existing_branch_lease_sha).strip()
      if not _GIT_SHA.fullmatch(branch_lease_sha):
        raise ContributionSubmitError(
          "This reviewed stack update is missing its exact public branch lease. "
          "Nothing was pushed."
        )
    if expected_existing_base_sha is not None:
      existing_base_sha = str(expected_existing_base_sha).strip()
      if not _GIT_SHA.fullmatch(existing_base_sha):
        raise ContributionSubmitError(
          "This reviewed stack update is missing its exact public base commit. "
          "Nothing was pushed."
        )
  elif (
    existing_branch_lease_sha is not None
    or expected_existing_base_sha is not None
  ):
    raise ContributionSubmitError(
      "A stack base or branch lease is valid only for a verified existing "
      "pull request update."
    )
  direct_base = (
    _git_ops._validate_branch(direct_base_branch) if direct_base_branch else None
  )
  if (
    branch_lease_sha is not None or existing_base_sha is not None
  ) and direct_base is None:
    raise ContributionSubmitError(
      "A stack base or branch lease is valid only for a reviewed pull request "
      "stack update."
    )
  if branch_lease_sha is not None and existing_base_sha is None:
    raise ContributionSubmitError(
      "A reviewed child restack must include its exact parent commit."
    )
  repo = _safe_repo_path(plan.get("repo_path"))
  if not (repo / ".git").exists():
    raise ContributionSubmitError("The staged repo is not a git checkout.")

  title, body = _exact_reviewed_pr_text(record)

  checkout_back = None
  recovered_attribution_patch: dict = {}
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
    if prior_attempt_phase == "normalizing":
      request = (
        prior_attempt_receipt.get("effective_request")
        if isinstance(prior_attempt_receipt, dict) else None
      )
      if not isinstance(request, dict):
        raise ContributionSubmitError("The attribution recovery receipt is incomplete.")
      recovered_patch = _recover_normalized_attribution(
        record, repo, branch, request,
      )
      if recovered_patch:
        current_head = str(recovered_patch.get("head_sha") or "")
        recovered_attribution_patch = recovered_patch
        if attempt_event is not None:
          attempt_event("armed", {
            **request, "action": "attribution_normalized",
            "head_sha": current_head,
          }, recovered_patch)
        record = {**record, **recovered_patch}
        plan = record.get("plan") or {}
    expected_base, expected_head, expected_diff = _git_ops._assert_fresh(
      record, diff_path, repo, branch,
    )
    _git_ops._assert_coauthor_trailer(repo, branch)
    if existing_head_repository is not None:
      # Updating a known PR is not the same routing decision as creating a new
      # one. Its live identity fixes the destination repository regardless of
      # generic upstream permission discovered for the connected account.
      same_repo_submission = (
        existing_head_repository.casefold() == upstream_repo.casefold()
      )
    else:
      same_repo_submission = bool(
        direct_base
        or upstream_repo.split("/", 1)[0].casefold() == login.casefold()
        or _git_ops._has_upstream_push_permission(repo, upstream_repo)
      )
    if same_repo_submission:
      _git_ops._assert_head_attribution(
        repo,
        branch,
        author_name=author_name,
        author_email=author_email,
      )
      record_patch = {}
    else:
      record_patch = _git_ops._record_patch_with(
        recovered_attribution_patch,
        _git_ops._normalize_head_attribution(
        repo,
        branch,
        author_name=author_name,
        author_email=author_email,
        base_sha=expected_base,
        expected_diff=expected_diff,
        record=record,
        before_amend=(
          (lambda before: attempt_event("normalizing", {
            "action": "normalize_attribution",
            "previous_head_sha": before["sha"], "tree": before["tree"],
            "parents": before["parents"],
            "expected_normalized_head_sha": before[
              "expected_normalized_head_sha"
            ],
            "author_date": before["author_date"], "base_sha": expected_base,
            "diff_sha256": expected_diff, "author_name": author_name,
            "author_email": author_email,
          }, {})) if attempt_event is not None else None
        ),
        ),
      )
      if record_patch and attempt_event is not None:
        attempt_event("armed", {
          "action": "attribution_normalized",
          "repo": upstream_repo,
          "branch": branch,
          "base_sha": expected_base,
          "previous_head_sha": expected_head,
          "head_sha": str(record_patch.get("head_sha") or ""),
          "author_name": author_name,
          "author_email": author_email,
        }, record_patch)
    _git_ops._assert_clean_worktree(repo)

    # An exact already-public PR is a terminal reconciliation, not a new
    # publication. Confirm it after every immutable local freshness and
    # attribution check but before fetching today's upstream: later upstream
    # movement must not make an already-completed owner action unrecoverable.
    # A branch-only recovery is different: it suppresses a duplicate push but
    # still passes through the current merge preflight before PR creation.
    recorded_base = (
      direct_base
      or record.get("last_submit_base_branch")
      or record.get("last_submit_upstream_branch")
      or (
        (plan.get("stack") or {}).get("base_branch")
        if isinstance(plan.get("stack"), dict)
        else None
      )
    )
    recovery = _authoritative_public_reconciliation(
      record,
      base_branch=str(recorded_base or "") or None,
      # Existing-PR updates carry an authoritative head repository; their
      # explicit base branch does not imply that the head lives upstream.
      same_repo=(
        None if expected_existing_pr_number is not None else bool(direct_base)
      ),
      expected_pr_number=expected_existing_pr_number,
      expected_head_repository=existing_head_repository,
    )
    if recovery is not None and recovery.pr_url is not None:
      pushed_patch = _git_ops._record_patch_with(record_patch, {
        "head_repository": recovery.head_repository,
        "last_submit_stage": "pushed",
        "last_submit_push_sha": str(plan.get("head_sha") or "").lower(),
        "last_submit_base_branch": str(recorded_base or ""),
        "last_pushed_branch": (
          branch if same_repo_submission else f"{login}:{branch}"
        ),
        "last_pushed_branch_url": (
          f"https://github.com/{recovery.head_repository}/tree/"
          f"{quote(branch, safe='/')}"
        ),
        "publication_stage": recovery.publication_stage,
      })
      reconcile_request = {
        "action": "reconcile_pr",
        "repo": upstream_repo,
        "number": recovery.pr_number,
        "url": recovery.pr_url,
        "head_repository": recovery.head_repository,
        "branch": branch,
        "head_sha": str(plan.get("head_sha") or "").lower(),
        "base_branch": str(recorded_base or ""),
        "labels": _reviewed_pr_labels(plan),
      }
      label_patch = _apply_reviewed_pr_labels(
        repo,
        upstream_repo,
        recovery.pr_number,
        _reviewed_pr_labels(plan),
      )
      completed_patch = _git_ops._record_patch_with(pushed_patch, label_patch)
      if attempt_event is not None:
        attempt_event(
          "complete", reconcile_request, completed_patch,
        )
      return (
        recovery.pr_url,
        recovery.pr_number,
        completed_patch,
      )
    public_branch_recovery = recovery is not None
    if prior_attempt_phase in {"branch_published", "pr_ambiguous", "complete"} and (
      recovery is None
    ):
      # The caller's authoritative read is not a lease: the branch can be
      # reset between that read and this owning publication primitive. Before
      # this function can repush after a post-mutation receipt, prove the
      # currently installed source again while the caller still holds its
      # source lock. A receipt alone never authorizes recreating public state.
      _assert_pending_equivalence_preflight(record)

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
    record_patch = _git_ops._record_patch_with(
      record_patch, {"last_submit_base_branch": submit_base},
    )

    # The route's earlier PR read is not a lease. Re-read the exact old public
    # head, base, and reviewed text immediately before the branch mutation.
    # Title/body are preconditions only: this path never PATCHes them.
    if (
      expected_existing_pr_number is not None
      and not public_branch_recovery
    ):
      reviewed_title, reviewed_body = reviewed_existing_metadata or (None, None)
      exact_old_state = _confirm_existing_pr_update(
        repo,
        upstream_repo,
        expected_existing_pr_number,
        expected_head_repository=existing_head_repository,
        expected_head_sha=expected_existing_head_sha,
        branch=branch,
        base_branch=submit_base,
        expected_base_sha=existing_base_sha,
        expected_title=reviewed_title,
        expected_body=reviewed_body,
      )
      if exact_old_state is None:
        raise ContributionSubmitError(
          "GitHub could not confirm the exact reviewed pull request state "
          "immediately before its branch update. Nothing was pushed.",
          status_code=503,
          code="update_unconfirmed",
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
    if expected_existing_pr_number is None and not public_branch_recovery:
      conflict = _existing_branch_pr(
        repo,
        upstream_repo,
        login,
        branch,
        same_repo=same_repo_submission,
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
    push_source_sha = str(
      record_patch.get("head_sha") or expected_head
    ).strip().lower()
    if not _GIT_SHA.fullmatch(push_source_sha):
      raise ContributionSubmitError(
        "Could not resolve the exact reviewed commit before pushing."
      )

    prior_push_request = None
    if prior_attempt_phase in {"push_pending", "push_ambiguous"}:
      prior_push_request = (
        prior_attempt_receipt.get("effective_request")
        if isinstance(prior_attempt_receipt, dict) else None
      )
      if (
        not isinstance(prior_push_request, dict)
        or prior_push_request.get("action") != "push"
        or prior_push_request.get("repo") != upstream_repo
        or prior_push_request.get("branch") != branch
        or prior_push_request.get("head_sha") != push_source_sha
        or "expected_remote_sha" not in prior_push_request
      ):
        raise ContributionSubmitError(
          "The prior branch push receipt is incomplete. Nothing was pushed; "
          "refresh and review this contribution again.",
          code="review_refresh_needed",
          record_patch=record_patch,
        )

    def expected_remote_tip(head_repository: str) -> str | None:
      """Bind first push or no-effect replay to one authoritative old tip."""
      actual = _git_ops._authoritative_upstream_branch_sha(
        repo, head_repository, branch,
      )
      if prior_push_request is not None:
        if prior_push_request.get("head_repository") != head_repository:
          raise ContributionSubmitError(
            "The prior branch push targeted a different repository. Nothing "
            "was pushed.", code="review_refresh_needed",
          )
        expected = prior_push_request.get("expected_remote_sha")
        if expected is not None:
          expected = _git_ops._canonical_reviewed_oid(
            expected, "previous remote branch head",
          )
        if actual != expected:
          raise ContributionSubmitError(
            "The GitHub branch changed after the prior push attempt. Nothing "
            "was pushed again; refresh and review the public branch.",
            code="review_refresh_needed",
            record_patch=record_patch,
          )
        return expected
      if expected_existing_head_sha is not None and actual != expected_existing_head_sha:
        raise ContributionSubmitError(
          "The pull request branch changed during publication validation. "
          "Nothing was pushed; refresh and review the update again.",
          code="review_refresh_needed",
          record_patch=record_patch,
        )
      return expected_existing_head_sha if expected_existing_head_sha is not None else actual

    def push_was_authoritatively_accepted(
      head_repository: str,
    ) -> bool | None:
      """Resolve one exhausted ambiguous push without trusting its journal."""
      try:
        return _git_ops._authoritative_upstream_branch_sha(
          repo, head_repository, branch,
        ) == push_source_sha
      except (ContributionSubmitError, subprocess.TimeoutExpired, OSError):
        return None

    if same_repo_submission:
      try:
        _git_ops._assert_upstream_push_permission(repo, upstream_repo)
      except ContributionSubmitError as exc:
        raise _git_ops._merge_error_patch(exc, record_patch) from exc
      push_remote = f"https://github.com/{upstream_repo}.git"
      published_repo = upstream_repo
      record_patch = _git_ops._record_patch_with(record_patch, {
        "head_repository": upstream_repo,
        "last_submit_base_branch": submit_base,
        "last_submit_mode": "stack" if direct_base else "upstream-repo",
      })
      if not public_branch_recovery:
        remote_before_push = expected_remote_tip(upstream_repo)
        pending_patch = _git_ops._record_patch_with(record_patch, {
          "last_submit_stage": "push_pending",
          "last_submit_push_sha": push_source_sha,
          "head_repository": upstream_repo,
        })
        if attempt_event is not None:
          attempt_event("push_pending", {
            "action": "push",
            "repo": upstream_repo,
            "head_repository": upstream_repo,
            "branch": branch,
            "head_sha": push_source_sha,
            "base_branch": submit_base,
            "expected_remote_sha": remote_before_push,
          }, pending_patch)
        try:
          if branch_lease_sha is not None:
            if remote_before_push != branch_lease_sha:
              raise ContributionSubmitError(
                "The reviewed stack branch lease changed before its push. "
                "Nothing was pushed.",
                code="review_refresh_needed",
                record_patch=pending_patch,
              )
            _push_stack_tip_with_lease(
              repo,
              upstream_repo=upstream_repo,
              target_branch=branch,
              expected_base=branch_lease_sha,
              landed_sha=push_source_sha,
            )
            last_push_error = None
          else:
            last_push_error = _push_branch(
              repo, push_remote, branch, push_source, remote_before_push,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
          ambiguous_patch = _git_ops._record_patch_with(record_patch, {
            "last_submit_stage": "push_ambiguous",
            "last_submit_push_sha": push_source_sha,
            "head_repository": upstream_repo,
          })
          if attempt_event is not None:
            attempt_event("push_ambiguous", {
              "action": "push", "repo": upstream_repo,
              "head_repository": upstream_repo, "branch": branch,
              "head_sha": push_source_sha, "base_branch": submit_base,
              "expected_remote_sha": remote_before_push,
            }, ambiguous_patch)
          raise ContributionSubmitError(
            "The branch push outcome is ambiguous; GitHub must confirm the "
            "exact reviewed head before retrying.", status_code=503,
            code="push_ambiguous", record_patch=ambiguous_patch,
          ) from exc
        if last_push_error:
          accepted = push_was_authoritatively_accepted(upstream_repo)
          if accepted is True:
            last_push_error = None
          elif (
            (accepted is None and prior_push_request is not None)
            or _is_transient_push_error(last_push_error)
          ):
            ambiguous_patch = _git_ops._record_patch_with(record_patch, {
              "last_submit_stage": "push_ambiguous",
              "last_submit_push_sha": push_source_sha,
              "head_repository": upstream_repo,
            })
            if attempt_event is not None:
              attempt_event("push_ambiguous", {
                "action": "push", "repo": upstream_repo,
                "head_repository": upstream_repo, "branch": branch,
                "head_sha": push_source_sha, "base_branch": submit_base,
                "expected_remote_sha": remote_before_push,
              }, ambiguous_patch)
            raise ContributionSubmitError(
              "The branch push outcome is ambiguous; GitHub must confirm "
              "the exact reviewed head before retrying.", status_code=503,
              code="push_ambiguous", record_patch=ambiguous_patch,
            )
          else:
            if attempt_event is not None:
              attempt_event("armed", {
                "action": "push_rejected", "repo": upstream_repo,
                "head_repository": upstream_repo, "branch": branch,
                "head_sha": push_source_sha, "base_branch": submit_base,
              }, record_patch)
            raise push_rejected(last_push_error, record_patch=record_patch)

    else:
      if public_branch_recovery:
        fork_slug = recovery.head_repository
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
      if not public_branch_recovery:
        remote_before_push = expected_remote_tip(fork_slug)
        pending_patch = _git_ops._record_patch_with(record_patch, {
          "last_submit_stage": "push_pending",
          "last_submit_push_sha": push_source_sha,
          "head_repository": fork_slug,
        })
        if attempt_event is not None:
          attempt_event("push_pending", {
            "action": "push",
            "repo": upstream_repo,
            "head_repository": fork_slug,
            "branch": branch,
            "head_sha": push_source_sha,
            "base_branch": submit_base,
            "expected_remote_sha": remote_before_push,
          }, pending_patch)
        try:
          if branch_lease_sha is not None:
            if remote_before_push != branch_lease_sha:
              raise ContributionSubmitError(
                "The reviewed stack branch lease changed before its push. "
                "Nothing was pushed.",
                code="review_refresh_needed",
                record_patch=pending_patch,
              )
            _push_stack_tip_with_lease(
              repo,
              upstream_repo=fork_slug,
              target_branch=branch,
              expected_base=branch_lease_sha,
              landed_sha=push_source_sha,
            )
            last_push_error = None
          else:
            last_push_error = _push_topic_branch(
              repo, branch, push_source, remote_before_push,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
          ambiguous_patch = _git_ops._record_patch_with(record_patch, {
            "last_submit_stage": "push_ambiguous",
            "last_submit_push_sha": push_source_sha,
            "head_repository": fork_slug,
          })
          if attempt_event is not None:
            attempt_event("push_ambiguous", {
              "action": "push", "repo": upstream_repo,
              "head_repository": fork_slug, "branch": branch,
              "head_sha": push_source_sha, "base_branch": submit_base,
              "expected_remote_sha": remote_before_push,
            }, ambiguous_patch)
          raise ContributionSubmitError(
            "The branch push outcome is ambiguous; GitHub must confirm the "
            "exact reviewed head before retrying.", status_code=503,
            code="push_ambiguous", record_patch=ambiguous_patch,
          ) from exc
        if last_push_error:
          accepted = push_was_authoritatively_accepted(fork_slug)
          if accepted is True:
            last_push_error = None
          elif (
            (accepted is None and prior_push_request is not None)
            or _is_transient_push_error(last_push_error)
          ):
            ambiguous_patch = _git_ops._record_patch_with(record_patch, {
              "last_submit_stage": "push_ambiguous",
              "last_submit_push_sha": push_source_sha,
              "head_repository": fork_slug,
            })
            if attempt_event is not None:
              attempt_event("push_ambiguous", {
                "action": "push", "repo": upstream_repo,
                "head_repository": fork_slug, "branch": branch,
                "head_sha": push_source_sha, "base_branch": submit_base,
                "expected_remote_sha": remote_before_push,
              }, ambiguous_patch)
            raise ContributionSubmitError(
              "The branch push outcome is ambiguous; GitHub must confirm "
              "the exact reviewed head before retrying.", status_code=503,
              code="push_ambiguous", record_patch=ambiguous_patch,
            )
          else:
            if attempt_event is not None:
              attempt_event("armed", {
                "action": "push_rejected", "repo": upstream_repo,
                "head_repository": fork_slug, "branch": branch,
                "head_sha": push_source_sha, "base_branch": submit_base,
              }, record_patch)
            raise push_rejected(last_push_error, record_patch=record_patch)

      published_repo = fork_slug
    pushed_branch_url = (
      f"https://github.com/{published_repo}/tree/{quote(branch, safe='/')}"
    )
    pushed_patch = {
      **record_patch,
      "last_submit_stage": "pushed",
      "last_pushed_branch": (
        branch if same_repo_submission else f"{login}:{branch}"

      ),
      "last_pushed_branch_url": pushed_branch_url,
    }
    pushed_sha = str(
      pushed_patch.get("last_submit_push_sha")
      or pushed_patch.get("head_sha")
      or plan.get("head_sha")
      or push_source_sha
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
    if attempt_event is not None:
      attempt_event("branch_published", {
        "action": "push",
        "repo": upstream_repo,
        "head_repository": published_repo,
        "branch": branch,
        "head_sha": pushed_sha,
        "base_branch": submit_base,
      }, pushed_patch)

    if expected_existing_pr_number is not None:
      reviewed_title, reviewed_body = reviewed_existing_metadata or (None, None)
      try:
        existing = _confirm_existing_pr_update(
          repo,
          upstream_repo,
          expected_existing_pr_number,
          expected_head_repository=existing_head_repository,
          expected_head_sha=pushed_sha,
          branch=branch,
          base_branch=submit_base,
          expected_base_sha=existing_base_sha,
          expected_title=reviewed_title,
          expected_body=reviewed_body,
        )
      except ContributionSubmitError as exc:
        raise ContributionSubmitError(
          exc.message,
          exc.status_code,
          record_patch=pushed_patch,
          code=exc.code,
          detail=exc.detail,
        ) from exc
      if not existing:
        raise ContributionSubmitError(
          "The approved pull request is no longer open on this exact branch. "
          f"The reviewed branch was pushed to {pushed_branch_url}, but no new "
          "pull request was created.",
          record_patch=pushed_patch,
        )
      existing_url, existing_stage = existing
      completed_patch = _git_ops._record_patch_with(
        pushed_patch, {"publication_stage": existing_stage},
      )
      if attempt_event is not None:
        attempt_event("complete", {
          "action": "update_pr",
          "repo": upstream_repo,
          "number": expected_existing_pr_number,
          "url": existing_url,
          "head_repository": published_repo,
          "branch": branch,
          "head_sha": pushed_sha,
          "base_branch": submit_base,
        }, completed_patch)
      return (
        existing_url,
        expected_existing_pr_number,
        completed_patch,
      )

    if prior_attempt_phase == "pr_ambiguous":
      raise ContributionSubmitError(
        "GitHub has not yet confirmed whether the earlier pull request "
        "creation completed. The exact branch is preserved; retry after "
        "GitHub exposes the pull request.",
        status_code=503,
        code="create_unconfirmed",
        record_patch=pushed_patch,
      )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
      f.write(body)
      body_file = f.name
    try:
      try:
        create_args = [
          "pr", "create",
          "-R", upstream_repo,
          "-H", branch if same_repo_submission else f"{login}:{branch}",

          "--title", title,
          "--body-file", body_file,
        ]
        if publication_stage == "draft":
          create_args.append("--draft")
        create_args.extend(("--base", submit_base))
        create_transport_error = None
        try:
          create_request = {
            "action": "create_pr",
            "repo": upstream_repo,
            "head_repository": published_repo,
            "branch": branch,
            "head_sha": pushed_sha,
            "base_branch": submit_base,
            "title": title,
            "body": body,
            "publication_stage": publication_stage,
            "labels": _reviewed_pr_labels(plan),
          }
          if attempt_event is not None:
            attempt_event("branch_published", create_request, pushed_patch)
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
        create_detail = (
          create_transport_error
          or ((pr.stderr or pr.stdout or "").strip() if pr is not None else "")
        )
        create_outcome_uncertain = (
          pr is None
          or pr.returncode == 0
          or _is_transient_push_error(create_detail)
        )
        if attempt_event is not None and create_outcome_uncertain:
          attempt_event("pr_ambiguous", create_request, pushed_patch)
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
            same_repo=same_repo_submission,

          )
          if existing:
            existing_number = _parse_pr_number(existing)
            confirmed_existing = (
              _confirm_existing_pr_update(
                repo,
                upstream_repo,
                existing_number,
                expected_head_repository=published_repo,
                expected_head_sha=pushed_sha,
                branch=branch,
                base_branch=submit_base,
              )
              if existing_number is not None else None
            )
            if confirmed_existing is None or confirmed_existing[0] != existing:
              existing = None
          if existing:
            existing_number = _parse_pr_number(existing)
            if attempt_event is not None:
              attempt_event("pr_ambiguous", {
                **create_request,
                "number": existing_number,
                "url": existing,
              }, pushed_patch)
            label_patch = _apply_reviewed_pr_labels(
              repo,
              upstream_repo,
              existing_number,
              _reviewed_pr_labels(plan),
            )
            completed_patch = _git_ops._record_patch_with(
              _git_ops._record_patch_with(
                pushed_patch,
                {"publication_stage": confirmed_existing[1]},
              ),
              label_patch,
            )
            if attempt_event is not None:
              attempt_event("complete", {
                **create_request,
                "number": existing_number,
                "url": existing,
              }, completed_patch)
            return (
              existing,
              existing_number,
              completed_patch,
            )
          detail = create_detail or "GitHub command failed."
          raise ContributionSubmitError(detail[:600] or "GitHub command failed.")
      except ContributionSubmitError as exc:
        raise ContributionSubmitError(
          f"{exc.message} The branch was pushed to {pushed_branch_url}.",
          exc.status_code,
          record_patch=pushed_patch,
          code=exc.code,
          detail=exc.detail,
        )
    finally:
      try:
        os.unlink(body_file)
      except OSError:
        pass
    url = (pr.stdout or "").strip().splitlines()[-1].strip()
    number = _parse_pr_number(url)
    if (
      number is None
      or url != f"https://github.com/{upstream_repo}/pull/{number}"
    ):
      raise ContributionSubmitError(
        f"GitHub did not return the exact expected pull request URL. The "
        f"branch was pushed to {pushed_branch_url}.",
        record_patch=pushed_patch,
      )
    confirmed = _confirm_existing_pr_update(
      repo,
      upstream_repo,
      number,
      expected_head_repository=published_repo,
      expected_head_sha=pushed_sha,
      branch=branch,
      base_branch=submit_base,
    )
    if confirmed is None or confirmed[0] != url:
      raise ContributionSubmitError(
        "GitHub returned a pull request URL, but did not confirm that it "
        f"points to the exact reviewed branch. The branch was pushed to "
        f"{pushed_branch_url}.",
        status_code=503,
        code="create_unconfirmed",
        record_patch=pushed_patch,
      )
    _confirmed_url, confirmed_stage = confirmed
    if attempt_event is not None:
      attempt_event("pr_ambiguous", {
        **create_request,
        "number": number,
        "url": url,
      }, pushed_patch)
    label_patch = _apply_reviewed_pr_labels(
      repo,
      upstream_repo,
      number,
      _reviewed_pr_labels(plan),
    )
    completed_patch = _git_ops._record_patch_with(
      _git_ops._record_patch_with(
        pushed_patch, {"publication_stage": confirmed_stage},
      ),
      label_patch,
    )
    if attempt_event is not None:
      attempt_event("complete", {
        **create_request,
        "number": number,
        "url": url,
      }, completed_patch)
    return (
      url,
      number,
      completed_patch,
    )
  finally:
    if checkout_back:
      _git_ops._git(repo, "checkout", "-q", checkout_back, check=False)


def _preflight_prepared_stack(
  rows: list[dict],
  *,
  source_preflight: Callable[[dict], str] | None = None,
) -> None:
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
      # This runs under the complete review/source lock set acquired by the
      # route. It is the last local-source boundary before any stack layer can
      # push, not merely a review-card hint that can go stale before Send.
      (
        source_preflight(record)
        if source_preflight is not None
        else _assert_pending_equivalence_before_publication(record)
      )
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
  try:
    for label, value in (
      ("old successor head sha", old_head_sha),
      ("old successor base sha", old_base_sha),
      ("successor head sha", successor_head_sha),
      ("successor target base sha", target_base_sha),
    ):
      _git_ops._canonical_reviewed_oid(value, label)
  except ContributionSubmitError:
    raise ContributionSubmitError(
      "This merged-parent successor is missing its exact reviewed commits. "
      "Nothing was pushed."
    ) from None
  if merged_base_sha:
    try:
      _git_ops._canonical_reviewed_oid(
        merged_base_sha, "merged-parent commit sha",
      )
    except ContributionSubmitError:
      raise ContributionSubmitError(
        "This merged-parent successor has an invalid merged-parent commit. "
        "Nothing was pushed."
      ) from None

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
  if _git_ops._is_transient_transport_error(error):
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
  attempt_event: Callable[[str, dict, dict | None], None] | None = None,
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
  reviewed_title, reviewed_body = _reviewed_existing_pr_metadata(record)

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
      expected_title=reviewed_title,
      expected_body=reviewed_body,
    )

  def confirm(base_branch: str, expected_base_sha: str | None):
    return confirm_state(new_head, base_branch, expected_base_sha)

  def complete_successor(
    confirmed: tuple[str, str], target_base_sha: str,
  ) -> tuple[str, int, dict]:
    """Return the exact successor state after both public mutations settle."""
    url, stage = confirmed
    successor_patch = _successor_record_patch(
      journal, witness, target_base_sha=target_base_sha, stage=stage,
    )
    return url, expected_number, successor_patch

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
    return complete_successor(confirmed, target_base_sha)

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
    push_request = {
      "action": "advance_successor_branch",
      "repo": upstream_repo,
      "number": expected_number,
      "head_repository": head_repository,
      "branch": branch,
      "previous_head_sha": old_head,
      "head_sha": new_head,
      "base_branch": old_base_branch,
      "target_base_branch": target_base_branch,
    }
    if attempt_event is not None:
      attempt_event("push_pending", push_request, witness)
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
    if attempt_event is not None:
      attempt_event("branch_published", push_request, witness)
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
    return complete_successor(confirmed, target_base_sha)
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
  retarget_request = {
    "action": "retarget_successor_pr",
    "repo": upstream_repo,
    "number": expected_number,
    "head_repository": head_repository,
    "branch": branch,
    "head_sha": new_head,
    "previous_base_branch": old_base_branch,
    "base_branch": target_base_branch,
  }
  if attempt_event is not None:
    attempt_event("pr_ambiguous", retarget_request, witness)
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
  return complete_successor(confirmed, target_base_sha)


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
