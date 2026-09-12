"""Trusted automatic-comment grant and exactly-once posting claims."""

from __future__ import annotations

from datetime import UTC, datetime
import re

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc

REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA = re.compile(r"^[0-9a-f]{40,64}$")


def normalized_repositories(values) -> list[str]:
  if not isinstance(values, list):
    raise HTTPException(422, "repositories must be a list")
  result = sorted({str(value).strip() for value in values})
  if not result or len(result) > 100 or any(REPO.fullmatch(repo) is None for repo in result):
    raise HTTPException(422, "repositories must contain 1-100 owner/name values")
  return result


def stamp_grant(
  db: Session, app_id: int, *, repositories, guide_hash: str,
  max_rounds_per_pr: int, daily_post_ceiling: int,
) -> models.ReviewerAutomationGrant:
  repos = normalized_repositories(repositories)
  if SHA.fullmatch(guide_hash) is None:
    raise HTTPException(422, "guide_hash must be a SHA-256 digest")
  if not 1 <= max_rounds_per_pr <= 20:
    raise HTTPException(422, "max_rounds_per_pr must be between 1 and 20")
  if not 1 <= daily_post_ceiling <= 100:
    raise HTTPException(422, "daily_post_ceiling must be between 1 and 100")
  row = db.query(models.ReviewerAutomationGrant).filter_by(app_id=app_id).one_or_none()
  if row is None:
    row = models.ReviewerAutomationGrant(app_id=app_id)
    db.add(row)
  # Scope/guidance change is a fresh grant and resets counters. Old identities
  # remain in the audit table, so re-granting can never duplicate a comment.
  row.enabled = True
  row.repositories_json = repos
  row.guide_hash = guide_hash
  row.max_rounds_per_pr = max_rounds_per_pr
  row.daily_post_ceiling = daily_post_ceiling
  row.daily_window = None
  row.daily_posts_used = 0
  row.rounds_json = {}
  row.granted_at = now_naive_utc()
  row.updated_at = now_naive_utc()
  db.commit()
  db.refresh(row)
  return row


def set_enabled(db: Session, app_id: int, enabled: bool):
  row = db.query(models.ReviewerAutomationGrant).filter_by(app_id=app_id).one_or_none()
  if row is None:
    return None
  row.enabled = bool(enabled)
  row.updated_at = now_naive_utc()
  db.commit()
  return row


def public_grant(row) -> dict | None:
  if row is None:
    return None
  return {
    "enabled": bool(row.enabled),
    "repositories": list(row.repositories_json or []),
    "guide_hash": row.guide_hash,
    "max_rounds_per_pr": row.max_rounds_per_pr,
    "daily_post_ceiling": row.daily_post_ceiling,
    "daily_posts_used": row.daily_posts_used,
    "daily_window": row.daily_window,
    "granted_at": row.granted_at.isoformat() if row.granted_at else None,
  }


def claim_post(
  db: Session, app_id: int, *, identity: str, repository: str,
  pr_number: int, head_sha: str, base_sha: str, guide_hash: str,
) -> models.ReviewerAutomationPost:
  if (
    SHA.fullmatch(identity) is None or SHA.fullmatch(head_sha) is None
    or SHA.fullmatch(base_sha) is None
  ):
    raise HTTPException(422, "invalid review identity, head SHA, or base SHA")
  if REPO.fullmatch(repository) is None or pr_number < 1:
    raise HTTPException(422, "invalid pull request target")

  # Serialize ceiling checks, counter increments, and the exactly-once claim.
  # The production store is SQLite; BEGIN IMMEDIATE prevents two different
  # identities racing through the same last daily/PR slot.
  db.commit()
  db.execute(text("BEGIN IMMEDIATE"))
  grant = db.query(models.ReviewerAutomationGrant).filter_by(app_id=app_id).one_or_none()
  if grant is None or not grant.enabled:
    db.rollback()
    raise HTTPException(403, "Automatic Reviewer posting is not granted.")
  if repository not in (grant.repositories_json or []):
    db.rollback()
    raise HTTPException(403, "Repository is outside the automatic-posting grant.")
  if guide_hash != grant.guide_hash:
    db.rollback()
    raise HTTPException(409, "Review guidance changed; grant automatic posting again.")
  existing = db.query(models.ReviewerAutomationPost).filter_by(
    app_id=app_id, identity=identity,
  ).one_or_none()
  if existing is not None:
    db.rollback()
    raise HTTPException(409, "This exact review identity was already claimed.")

  today = datetime.now(UTC).date().isoformat()
  if grant.daily_window != today:
    grant.daily_window = today
    grant.daily_posts_used = 0
  if grant.daily_posts_used >= grant.daily_post_ceiling:
    db.rollback()
    raise HTTPException(429, "Automatic Reviewer daily posting ceiling reached.")
  round_key = f"{repository.lower()}#{pr_number}"
  rounds = dict(grant.rounds_json or {})
  if int(rounds.get(round_key, 0)) >= grant.max_rounds_per_pr:
    db.rollback()
    raise HTTPException(409, "Automatic Reviewer round ceiling reached for this PR.")

  claim = models.ReviewerAutomationPost(
    app_id=app_id, identity=identity, repository=repository,
    pr_number=pr_number, head_sha=head_sha, base_sha=base_sha,
    guide_hash=guide_hash,
    status="posting", claimed_at=now_naive_utc(),
  )
  db.add(claim)
  grant.daily_posts_used += 1
  rounds[round_key] = int(rounds.get(round_key, 0)) + 1
  grant.rounds_json = rounds
  grant.updated_at = now_naive_utc()
  try:
    db.commit()
  except IntegrityError:
    db.rollback()
    raise HTTPException(409, "This exact review identity was already claimed.")
  db.refresh(claim)
  return claim


def claim_manual_post(
  db: Session, app_id: int, *, identity: str, repository: str,
  pr_number: int, head_sha: str, base_sha: str, guide_hash: str,
) -> models.ReviewerAutomationPost:
  """Claim one owner-triggered comment without creating standing authority.

  Manual and automatic sends intentionally share the same audit identity and
  primary key.  If a click races the background runner, exactly one path can
  reach GitHub; a one-off send never reads or consumes automatic-post limits.
  """
  if (
    SHA.fullmatch(identity) is None or SHA.fullmatch(head_sha) is None
    or SHA.fullmatch(base_sha) is None or SHA.fullmatch(guide_hash) is None
  ):
    raise HTTPException(422, "invalid review identity, head SHA, base SHA, or guide hash")
  if REPO.fullmatch(repository) is None or pr_number < 1:
    raise HTTPException(422, "invalid pull request target")

  db.commit()
  db.execute(text("BEGIN IMMEDIATE"))
  existing = db.query(models.ReviewerAutomationPost).filter_by(
    app_id=app_id, identity=identity,
  ).one_or_none()
  if existing is not None:
    exact_posted = (
      existing.status == "posted"
      and existing.repository == repository
      and existing.pr_number == pr_number
      and existing.head_sha == head_sha
      and existing.base_sha == base_sha
      and existing.guide_hash == guide_hash
    )
    db.rollback()
    if exact_posted:
      return existing
    raise HTTPException(409, "This exact review identity was already claimed.")
  claim = models.ReviewerAutomationPost(
    app_id=app_id, identity=identity, repository=repository,
    pr_number=pr_number, head_sha=head_sha, base_sha=base_sha,
    guide_hash=guide_hash, status="posting", claimed_at=now_naive_utc(),
  )
  db.add(claim)
  try:
    db.commit()
  except IntegrityError:
    db.rollback()
    raise HTTPException(409, "This exact review identity was already claimed.")
  db.refresh(claim)
  return claim


def finish_post(db: Session, claim, *, github_url: str | None) -> None:
  claim.status = "posted"
  claim.github_url = github_url
  claim.posted_at = now_naive_utc()
  db.commit()


def supersede_post(
  db: Session, claim, *, refund_grant: bool = True,
  error: str = "Pull request revision changed before the GitHub write.",
) -> None:
  """Close a pre-write freshness race without charging a public-post slot."""
  grant = (
    db.query(models.ReviewerAutomationGrant).filter_by(app_id=claim.app_id).one_or_none()
    if refund_grant else None
  )
  if grant is not None:
    grant.daily_posts_used = max(0, int(grant.daily_posts_used or 0) - 1)
    round_key = f"{claim.repository.lower()}#{claim.pr_number}"
    rounds = dict(grant.rounds_json or {})
    if int(rounds.get(round_key, 0)) > 1:
      rounds[round_key] = int(rounds[round_key]) - 1
    else:
      rounds.pop(round_key, None)
    grant.rounds_json = rounds
    grant.updated_at = now_naive_utc()
  claim.status = "superseded"
  claim.error = str(error)[-500:]
  db.commit()


def fail_post(db: Session, claim, error: str) -> None:
  # Fail closed: the identity remains claimed. An ambiguous upstream outcome
  # must never trigger an automatic retry that could duplicate a public review.
  claim.status = "uncertain"
  claim.error = str(error)[-500:]
  db.commit()


def public_posts(db: Session, app_id: int, *, limit: int = 200) -> list[dict]:
  """Return a bounded, credential-free audit projection for one Reviewer."""
  bounded = max(1, min(200, int(limit)))
  rows = (
    db.query(models.ReviewerAutomationPost)
    .filter_by(app_id=app_id)
    .order_by(models.ReviewerAutomationPost.claimed_at.desc())
    .limit(bounded)
    .all()
  )
  return [{
    "identity": row.identity,
    "repository": row.repository,
    "pr_number": row.pr_number,
    "head_sha": row.head_sha,
    "base_sha": row.base_sha,
    "guide_hash": row.guide_hash,
    "status": row.status,
    "url": row.github_url,
    "error": row.error,
    "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
    "posted_at": row.posted_at.isoformat() if row.posted_at else None,
  } for row in rows]
