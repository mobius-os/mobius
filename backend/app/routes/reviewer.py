"""Reviewer automatic-comment grants and guarded public posting.

The grant, exactly-once claims, and ceilings live in
``app.reviewer_automation``; these routes are the owner/app-facing
surface. Split out of ``routes/github.py`` so that file stops
accumulating unrelated features — public paths under ``/api/github``
are unchanged.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import app_services, fs_locks, models
from app.reviewer_automation import REPO as _REVIEWER_REPO
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal,
  get_principal,
  reject_cross_site,
  require_nondelegated_owner_control,
)
from app.github_contribution_git import _gh
from app.github_contributions import _recheck_submit_app, _validate_submit_app

router = APIRouter(prefix="/api/github", tags=["github"])
_limiter = Limiter(key_func=get_remote_address)


class ReviewerGrantBody(BaseModel):
  repositories: list[str]
  guide_hash: str
  max_rounds_per_pr: int = 5
  daily_post_ceiling: int = 12


class ReviewerGrantToggleBody(BaseModel):
  enabled: bool


class ReviewerCommentBody(BaseModel):
  identity: str
  repository: str
  pr_number: int
  head_sha: str
  base_sha: str
  guide_hash: str
  body: str


def _require_reviewer_owner_action(principal: Principal) -> None:
  """Require the real owner/top-level agent for new public authority.

  The app bearer may consume an existing automatic-posting grant, but it cannot
  create, revive, pause, or bypass one. Reviewer settings and drafts are
  intentionally app-writable, so an opaque-frame click alone is not evidence
  of owner confirmation for a GitHub write.
  """
  require_nondelegated_owner_control(principal)
  if principal.scope != "owner" or principal.app_id is not None:
    raise HTTPException(
      403,
      "This Reviewer action needs confirmation through the owner chat.",
    )


# ─────────────────────── Reviewer automatic comments ─────────────────


def _reviewer_live_pr(repository: str, number: int) -> dict:
  if _REVIEWER_REPO.fullmatch(repository) is None or number < 1:
    raise HTTPException(422, "Invalid Reviewer pull request target.")
  try:
    proc = _gh(
      Path(get_settings().data_dir), "api",
      f"repos/{repository}/pulls/{number}",
    )
    value = json.loads(proc.stdout)
  except Exception as exc:
    raise HTTPException(502, "Could not revalidate the Reviewer pull request.") from exc
  if not isinstance(value, dict):
    raise HTTPException(502, "GitHub returned an invalid pull request response.")
  return value


def _reviewer_assert_live_revision(
  repository: str, number: int, head_sha: str, base_sha: str,
) -> dict:
  pull = _reviewer_live_pr(repository, number)
  head = pull.get("head")
  base = pull.get("base")
  if not isinstance(head, dict) or not isinstance(base, dict):
    raise HTTPException(502, "GitHub returned an invalid pull request response.")
  live_head = str(head.get("sha") or "").lower()
  live_base = str(base.get("sha") or "").lower()
  if (
    pull.get("state") != "open" or live_head != head_sha.lower()
    or live_base != base_sha.lower()
  ):
    raise HTTPException(
      409,
      "The pull request changed or closed; collect and verify the new revision.",
    )
  return pull


async def _reviewer_manual_plan(
  app_id: int, body: ReviewerCommentBody, expected_nonce: str | None,
  db: Session,
) -> dict:
  # Never reserve a pooled connection while waiting for filesystem owners.
  db.close()
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = (
      db.query(models.App)
      .populate_existing()
      .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
      .one_or_none()
    )
    if current is None:
      db.close()
      raise HTTPException(409, "Reviewer changed; refresh the review before sending.")
    owner = db.query(models.Owner).first()
    if owner is None:
      db.close()
      raise HTTPException(503, "Owner setup is incomplete.")
    db.expunge(current)
    db.expunge(owner)
    db.close()
    result = await app_services.invoke_policy(
      current, owner, "reviewer/manual-plan", body.model_dump(),
    )
    return result


async def _reviewer_validate_comment(
  db: Session, app_id: int, body: str, head_sha: str,
) -> str:
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .one_or_none()
  )
  owner = db.query(models.Owner).first()
  if app is None or owner is None:
    raise HTTPException(404, "Reviewer app not found.")
  db.expunge(app)
  db.expunge(owner)
  db.close()
  result = await app_services.invoke_policy(
    app, owner, "reviewer/validate-comment",
    {"body": body, "head_sha": head_sha},
  )
  return str(result["body"])


async def _reviewer_prepare_grant(
  db: Session, app_id: int, body: ReviewerGrantBody,
) -> dict:
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .one_or_none()
  )
  owner = db.query(models.Owner).first()
  if app is None or owner is None:
    raise HTTPException(404, "Reviewer app not found.")
  db.expunge(app)
  db.expunge(owner)
  db.close()
  return await app_services.invoke_policy(
    app, owner, "reviewer/normalize-grant", body.model_dump(),
  )


def _reviewer_write_comment(
  db: Session, claim, *, repository: str, pr_number: int,
  head_sha: str, comment: str,
) -> str | None:
  from app import reviewer_automation

  try:
    proc = _gh(
      Path(get_settings().data_dir), "api", "--method", "POST",
      f"repos/{repository}/pulls/{pr_number}/reviews",
      "-f", "event=COMMENT", "-f", f"body={comment}",
      "-f", f"commit_id={head_sha}",
    )
    result = json.loads(proc.stdout) if proc.stdout.strip() else {}
    url = result.get("html_url") if isinstance(result, dict) else None
    reviewer_automation.finish_post(db, claim, github_url=url)
    return url
  except Exception as exc:
    reviewer_automation.fail_post(db, claim, "GitHub outcome is uncertain.")
    raise HTTPException(
      502,
      "GitHub did not confirm the Reviewer comment; it will not retry automatically.",
    ) from exc


@router.get(
  "/reviewer/{app_id}/grant",
  dependencies=[Depends(reject_cross_site)],
)
async def reviewer_grant_status(
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  from app import reviewer_automation

  _validate_submit_app(app_id, principal, db)
  row = db.query(models.ReviewerAutomationGrant).filter_by(app_id=app_id).one_or_none()
  return {"grant": reviewer_automation.public_grant(row)}


@router.get(
  "/reviewer/{app_id}/comments",
  dependencies=[Depends(reject_cross_site)],
)
async def reviewer_post_statuses(
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Bounded audit projection so Reviewer can reconcile uncertain writes."""
  from app import reviewer_automation

  _validate_submit_app(app_id, principal, db)
  return {"comments": reviewer_automation.public_posts(db, app_id)}


@router.post(
  "/reviewer/{app_id}/grant",
  dependencies=[Depends(reject_cross_site)],
)
@_limiter.limit("10/minute")
async def reviewer_grant(
  request: Request,
  app_id: int,
  body: ReviewerGrantBody,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """One explicit grant for exact repos, guide digest, and hard ceilings."""
  from app import reviewer_automation

  _require_reviewer_owner_action(principal)
  expected_nonce = _validate_submit_app(app_id, principal, db)
  grant = await _reviewer_prepare_grant(db, app_id, body)
  _recheck_submit_app(db, app_id, expected_nonce)
  row = reviewer_automation.stamp_grant(
    db, app_id, repositories=grant["repositories"],
    guide_hash=grant["guide_hash"],
    max_rounds_per_pr=grant["max_rounds_per_pr"],
    daily_post_ceiling=grant["daily_post_ceiling"],
  )
  return {"status": "granted", "grant": reviewer_automation.public_grant(row)}


@router.post(
  "/reviewer/{app_id}/grant/toggle",
  dependencies=[Depends(reject_cross_site)],
)
@_limiter.limit("20/minute")
async def reviewer_grant_toggle(
  request: Request,
  app_id: int,
  body: ReviewerGrantToggleBody,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  from app import reviewer_automation

  _require_reviewer_owner_action(principal)
  _validate_submit_app(app_id, principal, db)
  row = reviewer_automation.set_enabled(db, app_id, body.enabled)
  if row is None:
    raise HTTPException(404, "No automatic Reviewer posting grant exists.")
  return {"status": "ok", "grant": reviewer_automation.public_grant(row)}


@router.post(
  "/reviewer/{app_id}/comment",
  dependencies=[Depends(reject_cross_site)],
)
@_limiter.limit("20/minute")
async def reviewer_comment(
  request: Request,
  app_id: int,
  body: ReviewerCommentBody,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Post one COMMENT review after exact grant, freshness and ceiling checks."""
  from app import reviewer_automation

  _validate_submit_app(app_id, principal, db)
  repository = body.repository.strip()
  head_sha = body.head_sha.lower()
  base_sha = body.base_sha.lower()
  guide_hash = body.guide_hash.lower()
  comment = await _reviewer_validate_comment(db, app_id, body.body, head_sha)
  _reviewer_assert_live_revision(
    repository, body.pr_number, head_sha, base_sha,
  )
  claim = reviewer_automation.claim_post(
    db, app_id, identity=body.identity.lower(), repository=repository,
    pr_number=body.pr_number, head_sha=head_sha, base_sha=base_sha,
    guide_hash=guide_hash,
  )
  try:
    # Re-fetch after the durable claim closes the validation/posting race. A
    # push in this narrow window closes the stale identity but does not consume
    # a public post/round slot because no GitHub write has begun.
    _reviewer_assert_live_revision(
      repository, body.pr_number, head_sha, base_sha,
    )
  except HTTPException:
    reviewer_automation.supersede_post(db, claim)
    raise
  url = _reviewer_write_comment(
    db, claim, repository=repository, pr_number=body.pr_number,
    head_sha=head_sha, comment=comment,
  )
  return {"status": "posted", "url": url, "identity": claim.identity}


@router.post(
  "/reviewer/{app_id}/comment/manual",
  dependencies=[Depends(reject_cross_site)],
)
@_limiter.limit("20/minute")
async def reviewer_manual_comment(
  request: Request,
  app_id: int,
  body: ReviewerCommentBody,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Send one exact stored draft without creating an automatic-post grant."""
  from app import reviewer_automation

  _require_reviewer_owner_action(principal)
  expected_nonce = _validate_submit_app(app_id, principal, db)
  plan = await _reviewer_manual_plan(app_id, body, expected_nonce, db)
  comment = plan["body"]
  _reviewer_assert_live_revision(
    plan["repository"], plan["pr_number"], plan["head_sha"], plan["base_sha"],
  )
  claim = reviewer_automation.claim_manual_post(
    db, app_id, identity=plan["identity"], repository=plan["repository"],
    pr_number=plan["pr_number"], head_sha=plan["head_sha"],
    base_sha=plan["base_sha"], guide_hash=plan["guide_hash"],
  )
  if claim.status == "posted":
    return {
      "status": "posted", "url": claim.github_url, "identity": claim.identity,
    }
  claim_identity = claim.identity
  try:
    refreshed = await _reviewer_manual_plan(app_id, body, expected_nonce, db)
    if refreshed != plan:
      raise HTTPException(
        409, "The draft or guidance changed; refresh the review before sending.",
      )
    comment = refreshed["body"]
    _reviewer_assert_live_revision(
      refreshed["repository"], refreshed["pr_number"],
      refreshed["head_sha"], refreshed["base_sha"],
    )
    _recheck_submit_app(db, app_id, expected_nonce)
  except HTTPException:
    claim = db.query(models.ReviewerAutomationPost).filter_by(
      app_id=app_id, identity=claim_identity,
    ).one()
    reviewer_automation.supersede_post(
      db, claim, refund_grant=False,
      error="Reviewer draft, guidance, or pull request changed before the GitHub write.",
    )
    raise

  claim = db.query(models.ReviewerAutomationPost).filter_by(
    app_id=app_id, identity=claim_identity,
  ).one()
  url = _reviewer_write_comment(
    db, claim, repository=plan["repository"], pr_number=plan["pr_number"],
    head_sha=plan["head_sha"], comment=comment,
  )
  return {"status": "posted", "url": url, "identity": claim.identity}
