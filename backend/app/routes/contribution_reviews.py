"""Owner-selected PR review runs; exact public merge is a separate DB grant."""
import asyncio
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import chat_queue, contribution_review_runs as reviews, models
from app.chat_start import start_programmatic_chat_turn
from app.config import get_settings
from app.contribution_autopilot import resolve_round_choice
from app.database import get_db
from app.deps import (Principal, get_agent_run_principal, get_principal,
                      reject_cross_site, require_nondelegated_owner_or_app_control)
from app.github_contributions import _validate_submit_app
from app.github_contribution_git import _gh
from app.github_connection import _github_connection_transaction

router = APIRouter(prefix="/api/github/contributions", tags=["contribution-reviews"])
SCOPE = {"correctness", "maintainability", "simplicity", "tests", "security_privacy", "technical_debt"}


class PullIdentity(BaseModel):
  repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=256)
  number: int = Field(ge=1)
  head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class SelectedPR(PullIdentity):
  base_ref: str = Field(min_length=1, max_length=256)
  base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ChatApproval(BaseModel):
  # The owning agent interprets consent in context; this is an auditable
  # attestation, not a keyword classifier or permission inferred from PR text.
  context: str = Field(min_length=1, max_length=2000)

  @field_validator("context")
  @classmethod
  def nonempty_context(cls, value):
    if not value.strip():
      raise ValueError("Explain the owner's explicit approval in this chat.")
    return value.strip()


class StartReviews(BaseModel):
  request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
  mode: Literal["review", "review_merge"]
  items: list[SelectedPR] = Field(min_length=1, max_length=20)
  chat_approval: ChatApproval | None = None


class ReviewOutcome(PullIdentity):
  state: Literal["all_clear", "needs_you"]
  summary: str = Field(min_length=1, max_length=4000)
  scope: list[str] = Field(default_factory=list, max_length=6)
  tests: str = Field(default="", max_length=4000)


def _row(db, app_id, run_id, principal):
  _validate_submit_app(app_id, principal, db)
  row = db.query(models.ContributionReviewRun).filter_by(
    id=run_id, app_id=app_id, owner_id=principal.owner.id,
  ).first()
  if row is None:
    raise HTTPException(404, "Review run not found.")
  return row


def _assert_execution_live(db, row, principal):
  # Recheck after awaited remote preflight; Stop, uninstall and permission
  # revocation must still win before this exact public attempt is armed.
  active = db.query(models.ChatRun.id).filter_by(
    id=principal.run_id, chat_id=row.chat_id, status="running",
  ).first()
  if not active:
    raise HTTPException(409, "This review stopped or Contribute's permission changed. No merge was started.")
  _assert_app_current(db, row.app_id, row.app_nonce)


def _assert_app_current(db, app_id, nonce):
  app = db.query(models.App).populate_existing().filter_by(id=app_id).first()
  if app is None or app.deleted_at is not None or not app.github_access or app.token_nonce != nonce:
    raise HTTPException(409, "Contribute's permission changed. No new review or merge was started.")


def _chat_approval(db, body, principal):
  is_agent = principal.chat_id is not None or principal.run_id is not None
  if principal.delegation_id is not None:
    raise HTTPException(403, "Only the owning conversation can use chat approval, not a delegated helper.")
  if not is_agent:
    if body.chat_approval is not None:
      raise HTTPException(403, "Chat approval requires the current owning agent run.")
    return None
  if (body.chat_approval is None or principal.scope != "owner"
      or principal.app_id is not None or not principal.chat_id or not principal.run_id):
    raise HTTPException(403, "Explicit approval in the owning chat is required. Otherwise share the Contribute approval link.")
  live = db.query(models.ChatRun.id).join(models.Chat).filter(
    models.ChatRun.id == principal.run_id,
    models.ChatRun.chat_id == principal.chat_id,
    models.ChatRun.status == "running", models.Chat.deleted_at.is_(None),
  ).first()
  if not live:
    raise HTTPException(409, "The approving conversation is no longer running.")
  return {"source": "chat", "chat_id": principal.chat_id,
          "run_id": principal.run_id, "context": body.chat_approval.context}


@router.post("/{app_id}/review-runs", dependencies=[Depends(reject_cross_site),
             Depends(require_nondelegated_owner_or_app_control)])
async def start_reviews(app_id: int, body: StartReviews,
                        db: Session = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
  approval = _chat_approval(db, body, principal)
  app_nonce = _validate_submit_app(app_id, principal, db)
  _assert_app_current(db, app_id, app_nonce)
  selection = sorted([{**i.model_dump(), "repo": i.repo.lower()} for i in body.items], key=reviews.key)
  if len({reviews.key(i) for i in selection}) != len(selection):
    raise HTTPException(422, "Select each pull request only once.")
  async with chat_queue.get_transition_lock(f"review-start:{app_id}:{body.request_id}"):
    db.rollback()
    row = db.query(models.ContributionReviewRun).filter_by(
      app_id=app_id, request_id=body.request_id, owner_id=principal.owner.id,
    ).first()
    if row is not None:
      previous = [{"repo": t["repo"].lower(), **{k: t[k] for k in ("number", "head_sha", "base_ref", "base_sha")}} for t in row.targets_json]
      if row.mode != body.mode or previous != selection:
        raise HTTPException(409, "This request already approves a different selection.")
      if approval and row.chat_id != principal.chat_id:
        raise HTTPException(409, {"message": "This selection already belongs to another review conversation. Follow it instead of starting again.",
                                  "chat_id": row.chat_id, "run_id": row.id})
    else:
      cwd = Path(get_settings().data_dir) / "platform"
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
      if not actor.get("id"):
        raise HTTPException(409, "Connect GitHub before starting this review.")
      targets = [await asyncio.to_thread(reviews.inspect_target, _gh, cwd, item, body.mode)
                 for item in selection]
      # Consent in chat stays in that chat. An app selection without a source
      # conversation still receives its own durable review conversation.
      approval = _chat_approval(db, body, principal)
      _assert_app_current(db, app_id, app_nonce)
      if approval:
        chat_id = principal.chat_id
        targets = [{**target, "approval": approval} for target in targets]
      else:
        choice = resolve_round_choice(db)
        chat = models.Chat(id=str(uuid.uuid4()), title="Review selected contributions",
                           provider=choice["provider"], agent_settings_json=choice)
        db.add(chat)
        chat_id = chat.id
      row = models.ContributionReviewRun(id=str(uuid.uuid4()), app_id=app_id,
        owner_id=principal.owner.id, request_id=body.request_id, mode=body.mode,
        targets_json=targets, outcomes_json={}, chat_id=chat_id,
        github_actor_id=str(actor["id"]),
        app_nonce=app_nonce)
      db.add(row)
      db.commit()
      db.refresh(row)
    # Any durable first turn owns recovery; clicking twice never starts a second
    # review or revives a stopped run. An admission failure retries the empty chat.
    if not db.query(models.ChatRun).filter_by(chat_id=row.chat_id).first():
      chat = db.get(models.Chat, row.chat_id)
      started = await start_programmatic_chat_turn(chat_id=chat.id, title=chat.title,
        content=reviews.brief(row), provider=chat.provider, initiated_by_app_id=app_id)
      if not started:
        raise HTTPException(503, "The review conversation could not start. Retry this same selection.")
    return {"run": reviews.view(row), "brief": reviews.brief(row)}


@router.get("/{app_id}/review-runs")
def list_reviews(app_id: int, db: Session = Depends(get_db),
                 principal: Principal = Depends(get_principal)):
  _validate_submit_app(app_id, principal, db)
  rows = db.query(models.ContributionReviewRun).filter_by(
    app_id=app_id, owner_id=principal.owner.id,
  ).order_by(models.ContributionReviewRun.created_at.desc()).limit(100).all()
  ranked = db.query(models.ChatRun.chat_id, models.ChatRun.status,
    func.row_number().over(partition_by=models.ChatRun.chat_id,
      order_by=models.ChatRun.started_at.desc()).label("position"),
  ).filter(models.ChatRun.chat_id.in_([row.chat_id for row in rows])).subquery()
  latest = dict(db.query(ranked.c.chat_id, ranked.c.status).filter(ranked.c.position == 1).all())
  result = []
  for row in rows:
    value = reviews.view(row)
    if value["state"] != "complete" and latest.get(row.chat_id) in {"stopped", "failed", "interrupted"}:
      value.update(state="needs_you", summary="The review conversation stopped. Open it to continue the remaining work.")
    result.append(value)
  return {"runs": result}


@router.post("/{app_id}/review-runs/{run_id}/outcomes",
             dependencies=[Depends(reject_cross_site)])
async def report_outcome(app_id: int, run_id: str, body: ReviewOutcome,
                         db: Session = Depends(get_db),
                         principal: Principal = Depends(get_agent_run_principal)):
  row = _row(db, app_id, run_id, principal)
  if principal.delegation_id is not None or principal.chat_id != row.chat_id:
    raise HTTPException(403, "Only this review's parent conversation can report outcomes.")
  item_key = reviews.key(body.model_dump())
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    target = next((t for t in row.targets_json if reviews.key(t) == item_key), None)
    if target is None or target["head_sha"] != body.head_sha:
      raise HTTPException(409, "This head was not in the approved selection.")
    previous = row.outcomes_json.get(item_key, {})
    attempted = previous.get("merge_attempted") is True or previous.get("state") in {"merging", "merge_unknown", "queued", "merged"}
    if previous.get("state") in {"merged", "all_clear"}:
      return {"run": reviews.view(row)}
    cwd = Path(get_settings().data_dir) / "platform"
    try:
      repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
    except HTTPException as exc:
      # A changed/unavailable PR must not erase the durable attempt receipt.
      # If that same head returns later, it is still reconciliation, not retry.
      reviews.save_outcome(db, row, item_key, {**previous, "state": "needs_you", "summary": str(exc.detail),
        "head_sha": target["head_sha"], "merge_attempted": attempted})
      return {"run": reviews.view(row)}
    outcome = {"state": body.state, "summary": body.summary, "scope": body.scope,
               "tests": body.tests, "head_sha": body.head_sha,
               "review_chat_id": row.chat_id, "review_run_id": principal.run_id}
    if attempted:
      if pull.get("merged"):
        outcome = {**previous, "state": "merged", "merge_sha": pull.get("merge_commit_sha")}
      else:
        checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
        entry = checks.get("mergeQueueEntry")
        if entry and entry.get("headCommit", {}).get("oid") == target["head_sha"]:
          outcome = {**previous, "state": "queued", "queue_entry_id": entry["id"]}
        else:
          outcome = {**previous, "state": "merge_unknown", "summary":
            "The earlier merge or queue request is not confirmed. Check GitHub; it was not repeated."}
      reviews.save_outcome(db, row, item_key, outcome)
      return {"run": reviews.view(row)}
    if body.state == "all_clear" and (set(body.scope) != SCOPE or not body.tests.strip()):
      raise HTTPException(422, "Record the complete review scope and test evidence before all clear.")
    if body.state == "all_clear" and row.mode == "review_merge":
      if pull.get("merged"):
        outcome.update(state="merged", merge_sha=pull.get("merge_commit_sha"))
        reviews.save_outcome(db, row, item_key, outcome)
        return {"run": reviews.view(row)}
      checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
      existing_entry = checks.get("mergeQueueEntry")
      if existing_entry and existing_entry.get("headCommit", {}).get("oid") == target["head_sha"]:
        outcome.update(state="queued", queue_entry_id=existing_entry["id"])
        reviews.save_outcome(db, row, item_key, outcome)
        return {"run": reviews.view(row)}
      blocker = reviews.merge_blocker(target, repo, pull, checks)
      if blocker:
        outcome.update(state="needs_you", summary=blocker)
      else:
        # Hold the existing credential mutation lock from actor recheck through
        # public I/O: reconnecting another account cannot spend this grant.
        async with _github_connection_transaction():
          actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
          if str(actor.get("id") or "") != row.github_actor_id:
            outcome.update(state="needs_you", summary="The connected GitHub account changed. Approve a new review selection.")
          else:
            _assert_execution_live(db, row, principal)
            # Persist before network I/O. A crash or lost response cannot
            # authorize replay. CAS also fences other server processes.
            owned_elsewhere = reviews.arm_merge(db, row, target, outcome, principal)
            if owned_elsewhere:
              if owned_elsewhere.get("review_selection_id") != row.id:
                reviews.save_outcome(db, row, item_key, owned_elsewhere)
              else:
                db.refresh(row)
              return {"run": reviews.view(row)}
            outcome["merge_attempted"] = True
            try:
              if checks.get("isMergeQueueEnabled") is True:
                receipt = await asyncio.to_thread(reviews.enqueue, _gh, cwd, target)
                outcome.update(state="queued", queue_entry_id=receipt["id"])
              else:
                receipt = await asyncio.to_thread(reviews.perform_merge, _gh, cwd, target, repo)
                if receipt.get("merged") is not True:
                  raise HTTPException(409, "GitHub did not confirm a merge.")
                outcome.update(state="merged", merge_sha=receipt.get("sha"))
            except Exception:
              outcome.update(state="merge_unknown", summary=
                "GitHub did not confirm the merge or queue request. Reconcile it before any new action.")
    reviews.save_outcome(db, row, item_key, outcome)
    return {"run": reviews.view(row)}
