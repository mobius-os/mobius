"""Owner-selected PR review runs; exact public merge is a separate DB grant."""
import asyncio
import hashlib
import json
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import chat_queue, chat_writer, contribution_review_runs as reviews, models, providers, transcript_rows
from app import contribution_review_presets as presets
from app.chat_start import start_programmatic_chat_turn
from app.chat_visibility import coerce_agent_settings
from app.config import get_settings
from app.contribution_autopilot import resolve_round_choice
from app.database import get_db
from app.deps import (Principal, get_agent_run_principal, get_principal, is_owner_input_principal,
                      reject_cross_site, require_nondelegated_owner_or_app_control)
from app.github_contributions import _validate_submit_app
from app.github_contribution_git import _gh
from app.github_connection import _github_connection_transaction

router = APIRouter(tags=["contribution-reviews"])
APP_PREFIX = "/api/github/contributions"
SCOPE = {"correctness", "maintainability", "simplicity", "tests", "security_privacy", "technical_debt"}
REVIEW_CAPABILITIES = {"draft_takeover": True, "post_review": True}
TAKEOVER_SCOPES = {"named_pr_repairs_and_reviewed_successors", "named_pr_repairs_ready_and_reviewed_successors"}


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


class ReviewOptions(BaseModel):
  model_config = ConfigDict(extra="forbid")
  review_prompt: str | None = Field(default=None, max_length=20000)
  fix_prompt: str | None = Field(default=None, max_length=20000)
  merge_prompt: str | None = Field(default=None, max_length=20000)
  max_rounds: int | None = Field(default=None, ge=1, strict=True)
  autopilot: bool = False
  post_review: bool | None = None


class ReviewAgentChoice(BaseModel):
  model_config = ConfigDict(extra="forbid")
  provider: str = Field(max_length=32)
  model: str = Field(min_length=1, max_length=256)
  effort: str | None = Field(default=None, max_length=32)


class StartReviews(BaseModel):
  source_chat_id: str | None = Field(default=None, min_length=1, max_length=64)
  agent: ReviewAgentChoice | None = None
  preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
  request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
  mode: Literal["review", "review_merge", "review_fix_merge"]
  options: ReviewOptions | None = None
  confirmation_scope: Literal["named_pr_repairs_and_reviewed_successors", "named_pr_repairs_ready_and_reviewed_successors"] | None = None
  items: list[SelectedPR] = Field(min_length=1, max_length=20)
  chat_approval: ChatApproval | None = None


class ReviewOutcome(PullIdentity):
  state: Literal["all_clear", "needs_you"]
  summary: str = Field(min_length=1, max_length=4000)
  scope: list[str] = Field(default_factory=list, max_length=6)
  tests: str = Field(default="", max_length=4000)
  tests_passed: bool | None = None
  reviewed_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
  independent_receipt_id: str | None = Field(default=None, max_length=64)


def _row(db, app_id, run_id, principal):
  _authorize_context(db, app_id, principal)
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
  if app_id is None:
    return
  app = db.query(models.App).populate_existing().filter_by(id=app_id).first()
  if app is None or app.deleted_at is not None or not app.github_access or app.token_nonce != nonce:
    raise HTTPException(409, "Contribute's permission changed. No new review or merge was started.")


def _authorize_context(db, app_id, principal):
  if app_id is not None:
    return _validate_submit_app(app_id, principal, db)
  if principal.scope != "owner" or principal.app_id is not None:
    raise HTTPException(403, "Core GitHub review workflows require an owner token.")
  return None


def _fingerprint(snapshot):
  return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _options_input(body, frozen_options=None):
  if body.options is None:
    return None
  options = body.options.model_dump(exclude_none=True)
  # Keep null as an explicit uncapped selection, unlike absent prompt overrides.
  options["max_rounds"] = body.options.max_rounds
  previous = (frozen_options or {}).get("input_options") or {}
  if "max_rounds" not in body.options.model_fields_set and previous.get("max_rounds") == 5:
    # Prior requests materialized the then-default finite limit. A retry with
    # the same omitted field still names that saved grant, not today's default.
    options["max_rounds"] = 5
  return options


def _snapshot(db, body, principal):
  choice = resolve_round_choice(db)
  requested = getattr(body, "agent", None)
  if requested:
    if requested.provider not in providers.PROVIDERS or providers._model_belongs_to_other_provider(requested.model, requested.provider):
      raise HTTPException(422, "The selected provider/model does not match.")
    try:
      settings = providers.snapshot_chat_agent_settings(get_settings().data_dir,
        requested.provider, model=requested.model, effort=requested.effort,
        fallback_model=requested.model)
    except ValueError as exc:
      raise HTTPException(422, str(exc)) from exc
    choice = {"provider": requested.provider, **settings}
  if principal.chat_id and principal.run_id:
    chat = db.get(models.Chat, principal.chat_id)
    if chat:
      actual = {"provider": chat.provider, **coerce_agent_settings(chat.agent_settings_json)}
      if actual.get("model"):
        if requested and any(actual.get(k) != choice.get(k) for k in ("provider", "model", "effort")):
          raise HTTPException(409, "The owning chat is executing a different model. Preview its actual run choice.")
        choice = {k: actual[k] for k in ("provider", "model", "effort") if k in actual}
  try:
    return presets.resolve_options(db, _options_input(body), choice=choice)
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc


def _chat_approval(db, body, principal):
  is_agent = principal.chat_id is not None or principal.run_id is not None
  if body.source_chat_id is not None and (not is_agent or body.source_chat_id != principal.chat_id):
    raise HTTPException(403, "A source chat may only name the current authenticated owning conversation.")
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


@router.post(APP_PREFIX + "/{app_id}/review-runs", dependencies=[Depends(reject_cross_site),
             Depends(require_nondelegated_owner_or_app_control)])
async def start_reviews(app_id: int | None, body: StartReviews,
                        db: Session = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
  approval = _chat_approval(db, body, principal)
  app_nonce = _authorize_context(db, app_id, principal)
  _assert_app_current(db, app_id, app_nonce)
  if body.mode == "review_fix_merge" and body.confirmation_scope not in TAKEOVER_SCOPES:
    raise HTTPException(422, "Confirm scoped repairs to the named PR and freshly reviewed successors before takeover.")
  if body.options is not None and body.options.post_review and body.mode != "review":
    raise HTTPException(422, "Posting a review on GitHub is available for Review only.")
  selection = sorted([{**i.model_dump(), "repo": i.repo.lower()} for i in body.items], key=reviews.key)
  if len({reviews.key(i) for i in selection}) != len(selection):
    raise HTTPException(422, "Select each pull request only once.")
  async with chat_queue.get_transition_lock(f"review-start:{principal.owner.id}:{app_id}:{body.request_id}"):
    db.rollback()
    row = db.query(models.ContributionReviewRun).filter_by(
      app_id=app_id, request_id=body.request_id, owner_id=principal.owner.id,
    ).first()
    if row is not None:
      previous = [{"repo": t["repo"].lower(), **{k: t[k] for k in ("number", "head_sha", "base_ref", "base_sha")}} for t in row.targets_json]
      if (row.mode != body.mode or previous != selection
          or (row.options_json and (
            row.options_json.get("input_options") != (_options_input(body, row.options_json) or {})
            or row.options_json.get("input_agent") != (body.agent.model_dump() if body.agent else None)
            or row.options_json.get("confirmation_scope") != body.confirmation_scope
            or (body.preview_sha256 and row.options_json.get("preview_sha256") != body.preview_sha256)))):
        raise HTTPException(409, "This request already approves a different selection.")
      if approval and row.chat_id != principal.chat_id:
        raise HTTPException(409, {"message": "This selection already belongs to another review conversation. Follow it instead of starting again.",
                                  "chat_id": row.chat_id, "run_id": row.id})
    else:
      if body.mode == "review_fix_merge" and not body.preview_sha256:
        raise HTTPException(422, "Preview and confirm the exact frozen takeover prompts/model before admission.")
      snapshot = _snapshot(db, body, principal)
      if body.preview_sha256 and body.preview_sha256 != _fingerprint(snapshot):
        raise HTTPException(409, "The resolved prompts or model changed. Preview and confirm again.")
      cwd = Path(get_settings().data_dir) / "platform"
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
      if not actor.get("id"):
        raise HTTPException(409, "Connect GitHub before starting this review.")
      targets = [await asyncio.to_thread(reviews.inspect_target, _gh, cwd, item, body.mode)
                 for item in selection]
      if body.mode == "review_fix_merge":
        for target in targets:
          target["allowed_files"] = await asyncio.to_thread(reviews.repair_file_scope, _gh, cwd, target)
          await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
          await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
      # Consent in chat stays in that chat. An app selection without a source
      # conversation still receives its own durable review conversation.
      approval = _chat_approval(db, body, principal)
      _assert_app_current(db, app_id, app_nonce)
      if approval:
        chat_id = principal.chat_id
        targets = [{**target, "approval": approval} for target in targets]
      else:
        choice = snapshot["choice"]
        chat = chat_writer.create_chat(id=str(uuid.uuid4()), title="Review selected contributions",
                           provider=choice["provider"], created_by_app_id=app_id,
                           agent_settings_json={
                             key: value for key, value in choice.items()
                             if key != "provider"
                           })
        db.add(chat)
        chat_id = chat.id
      row = models.ContributionReviewRun(id=str(uuid.uuid4()), app_id=app_id,
        owner_id=principal.owner.id, request_id=body.request_id, mode=body.mode,
        targets_json=targets, outcomes_json={}, chat_id=chat_id,
        github_actor_id=str(actor["id"]),
        app_nonce=app_nonce, options_json={**snapshot, "input_options": _options_input(body) or {},
          "confirmation_scope": body.confirmation_scope, "preview_sha256": _fingerprint(snapshot),
          "input_agent": body.agent.model_dump() if body.agent else None})
      db.add(row)
      db.commit()
      db.refresh(row)
    # Any durable first turn owns recovery; clicking twice never starts a second
    # review or revives a stopped run. An admission failure retries the empty chat.
    if not db.query(models.ChatRun).filter_by(chat_id=row.chat_id).first():
      chat = db.get(models.Chat, row.chat_id)
      frozen = row.options_json or {}
      actual = coerce_agent_settings(chat.agent_settings_json)
      if frozen.get("model") and (chat.provider != frozen["provider"] or actual.get("model") != frozen["model"] or actual.get("effort") != frozen.get("reasoning_effort")):
        raise HTTPException(409, "The saved review chat no longer matches the frozen provider/model/effort.")
      _assert_app_current(db, row.app_id, row.app_nonce)
      started = await start_programmatic_chat_turn(chat_id=chat.id, title=chat.title,
        content=reviews.brief(row), provider=chat.provider, initiated_by_app_id=app_id)
      if not started:
        raise HTTPException(503, "The review conversation could not start. Retry this same selection.")
    return {"run": _run_view(db, row), "brief": reviews.brief(row)}


@router.get(APP_PREFIX + "/{app_id}/review-runs")
def list_reviews(app_id: int | None, db: Session = Depends(get_db),
                 principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  rows = db.query(models.ContributionReviewRun).filter(
    models.ContributionReviewRun.owner_id == principal.owner.id,
    _read_context_filter(app_id),
  ).order_by(models.ContributionReviewRun.created_at.desc()).limit(100).all()
  return {"runs": [_run_view(db, row, context_app_id=app_id) for row in rows]}


def _last_reply_error(chat) -> str:
  """The provider's own error on the conversation's last reply, if it recorded one.

  A run that stopped on out-of-credits or a provider block should say so,
  rather than only that it stopped.
  """
  for message in reversed(transcript_rows.history(chat) if chat else []):
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    errors = [block["message"].strip() for block in (message.get("blocks") or [])
              if isinstance(block, dict) and block.get("type") == "error"
              and isinstance(block.get("message"), str) and block["message"].strip()]
    return errors[-1].splitlines()[0][:240] if errors else ""
  return ""


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/outcomes",
             dependencies=[Depends(reject_cross_site)])
async def report_outcome(app_id: int | None, run_id: str, body: ReviewOutcome,
                         db: Session = Depends(get_db),
                         principal: Principal = Depends(get_agent_run_principal)):
  row = _row(db, app_id, run_id, principal)
  _parent(db, row, principal)
  item_key = reviews.key(body.model_dump())
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    original = next((t for t in row.targets_json if reviews.key(t) == item_key), None)
    target = reviews.effective_target(row, original) if original else None
    if target is None or target["head_sha"] != body.head_sha:
      raise HTTPException(409, "This head was not in the approved selection.")
    previous = row.outcomes_json.get(item_key, {})
    attempted = previous.get("merge_attempted") is True or previous.get("state") in {"merging", "merge_unknown", "queued", "merged"}
    if previous.get("state") == "merged":
      return {"run": _run_view(db, row)}
    cwd = Path(get_settings().data_dir) / "platform"
    try:
      repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
    except HTTPException as exc:
      # A changed/unavailable PR must not erase the durable attempt receipt.
      # If that same head returns later, it is still reconciliation, not retry.
      reviews.save_outcome(db, row, item_key, {**previous, "state": "needs_you", "summary": str(exc.detail),
        "head_sha": target["head_sha"], "merge_attempted": attempted})
      return {"run": _run_view(db, row)}
    outcome = {**previous, "state": body.state, "summary": body.summary, "scope": body.scope,
               "tests": body.tests, "tests_passed": body.tests_passed,
               "reviewed_base_sha": body.reviewed_base_sha, "independent_receipt_id": body.independent_receipt_id,
               "head_sha": body.head_sha,
               "review_chat_id": row.chat_id, "review_run_id": principal.run_id}
    if attempted:
      if pull.get("merged"):
        outcome = {**previous, "state": "merged", "merge_sha": pull.get("merge_commit_sha")}
      else:
        checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
        entry = reviews.queue_entry(checks, target)
        if entry:
          outcome = {**previous, "state": "queued", "queue_entry_id": entry["id"]}
        else:
          outcome = {**previous, "state": "merge_unknown", "summary":
            "The earlier merge or queue request is not confirmed. Check GitHub; it was not repeated."}
      _parent(db, row, principal)
      reviews.save_outcome(db, row, item_key, outcome)
      return {"run": _run_view(db, row)}
    if body.state == "all_clear" and (set(body.scope) != SCOPE or not body.tests.strip() or body.tests_passed is False):
      raise HTTPException(422, "Record the complete review scope and test evidence before all clear.")
    if body.state == "all_clear":
      try:
        await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
      except HTTPException as exc:
        if row.mode == "review_merge":
          raise
        reviews.save_outcome(db, row, item_key, {**previous, "state": "needs_you",
          "head_sha": target["head_sha"], "summary": str(exc.detail)})
        return {"run": _run_view(db, row)}
      _parent(db, row, principal)
      if row.mode == "review_fix_merge":
        reviews.require_independent_clear(row, target, body)
    if body.state == "all_clear" and row.mode in {"review_merge", "review_fix_merge"}:
      ready_attempt = previous.get("ready_attempt")
      if ready_attempt and ready_attempt.get("state") != "ready":
        raise HTTPException(409, "The draft readiness attempt is uncertain. Reconcile it read-only before merge.")
      if pull.get("merged"):
        outcome.update(state="merged", merge_sha=pull.get("merge_commit_sha"))
        reviews.save_outcome(db, row, item_key, outcome)
        return {"run": _run_view(db, row)}
      checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
      existing_entry = reviews.queue_entry(checks, target)
      if existing_entry:
        await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
        _parent(db, row, principal)
        outcome.update(state="queued", queue_entry_id=existing_entry["id"])
        reviews.save_outcome(db, row, item_key, outcome)
        return {"run": _run_view(db, row)}
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
            await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
            _parent(db, row, principal)
            # Persist before network I/O. A crash or lost response cannot
            # authorize replay. CAS also fences other server processes.
            owned_elsewhere = reviews.arm_merge(db, row, target, outcome, principal)
            if owned_elsewhere:
              if owned_elsewhere.get("review_selection_id") != row.id:
                reviews.save_outcome(db, row, item_key, owned_elsewhere)
              else:
                db.refresh(row)
              return {"run": _run_view(db, row)}
            outcome["merge_attempted"] = True
            try:
              _parent(db, row, principal)
              if checks.get("isMergeQueueEnabled") is True:
                receipt = await asyncio.to_thread(reviews.enqueue, _gh, cwd, target)
                outcome.update(state="queued", queue_entry_id=receipt["id"])
              else:
                receipt = await asyncio.to_thread(reviews.perform_merge, _gh, cwd, target, repo)
                if receipt.get("merged") is not True:
                  raise HTTPException(409, "GitHub did not confirm a merge.")
                outcome.update(state="merged", merge_sha=receipt.get("sha"))
              _assert_execution_live(db, row, principal)
            except Exception as exc:
              outcome.update(
                state="merge_unknown",
                summary=reviews.merge_failure_summary(exc),
              )
    if (row.mode == "review" and (row.options_json or {}).get("post_review") is True
        and outcome.get("state") in {"all_clear", "needs_you"} and not previous.get("public_review")):
      outcome = await _post_public_review(db, row, item_key, target, outcome, principal, cwd)
    if not outcome.get("merge_attempted"):
      _parent(db, row, principal)
    reviews.save_outcome(db, row, item_key, outcome)
    return {"run": _run_view(db, row)}


async def _post_public_review(db, row, item_key, target, outcome, principal, cwd):
  """Post the recorded verdict once, as a GitHub comment review on the exact head.

  The owner chose this at launch (frozen `post_review`). The server, not the
  agent, performs the write, under the same connected-account guard as merges.
  The receipt is saved before network I/O, so a lost response is reported as
  unknown and never posted twice.
  """
  async with _github_connection_transaction():
    actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
    if str(actor.get("id") or "") != row.github_actor_id:
      return {**outcome, "public_review": {"state": "skipped",
        "summary": "The connected GitHub account changed, so the review was not posted."}}
    _parent(db, row, principal)
    reviews.save_outcome(db, row, item_key, {**outcome, "public_review": {"state": "posting"}})
    try:
      receipt = await asyncio.to_thread(reviews.post_review, _gh, cwd, target, reviews.review_comment_body(outcome))
      return {**outcome, "public_review": {"state": "posted", **receipt}}
    except Exception:
      return {**outcome, "public_review": {"state": "unknown",
        "summary": "GitHub did not confirm the posted review. Check the PR before posting it again."}}


class ReviewPreview(BaseModel):
  agent: ReviewAgentChoice | None = None
  options: ReviewOptions | None = None


@router.get(APP_PREFIX + "/{app_id}/review-presets")
def review_presets(app_id: int, db: Session = Depends(get_db),
                   principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  return {"presets": presets.presets(), "capabilities": REVIEW_CAPABILITIES}


@router.post(APP_PREFIX + "/{app_id}/review-preview", dependencies=[Depends(reject_cross_site)])
def review_preview(app_id: int, body: ReviewPreview, db: Session = Depends(get_db),
                   principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  snapshot = _snapshot(db, body, principal)
  return {"options": snapshot, "preview_sha256": _fingerprint(snapshot)}


@router.get(APP_PREFIX + "/{app_id}/review-runs/{run_id}")
def get_review(app_id: int, run_id: str, db: Session = Depends(get_db),
               principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  row = db.query(models.ContributionReviewRun).filter(
    models.ContributionReviewRun.id == run_id,
    models.ContributionReviewRun.owner_id == principal.owner.id,
    _read_context_filter(app_id),
  ).first()
  if row is None:
    raise HTTPException(404, "Review run not found.")
  return {"run": _run_view(db, row, context_app_id=app_id), "brief": reviews.brief(row)}


def _read_context_filter(app_id):
  """App presentation may include core work; its mutation grant stays core."""
  if app_id is None:
    return models.ContributionReviewRun.app_id.is_(None)
  return or_(models.ContributionReviewRun.app_id == app_id,
             models.ContributionReviewRun.app_id.is_(None))


def _run_view(db, row, *, context_app_id=None):
  value = reviews.view(row)
  chat = db.get(models.Chat, row.chat_id)
  value["chat_created_by_app_id"] = chat.created_by_app_id if chat else None
  stop_context = context_app_id if context_app_id is not None else row.app_id
  value["can_stop"] = bool(chat and (stop_context is None or chat.created_by_app_id == stop_context))
  value["stop_semantics"] = "revokes_future_actions; an already-admitted public action may finish"
  frozen = row.options_json or {}
  actual = coerce_agent_settings(chat.agent_settings_json) if chat else {}
  if frozen.get("model") and (not chat or chat.provider != frozen["provider"] or actual.get("model") != frozen["model"] or actual.get("effort") != frozen.get("reasoning_effort")):
    value.update(state="needs_you", summary="The owning chat model choice no longer matches this frozen workflow.")
  last = db.query(models.ChatRun).filter_by(chat_id=row.chat_id).order_by(
    models.ChatRun.started_at.desc()).first()
  if value["state"] != "complete" and chat and chat.pending_question_id:
    # An open owner card is the conversation's real state, however its last
    # turn ended: it continues when the owner answers, so it is not stopped.
    value.update(state="needs_you", execution_state="awaiting_owner",
                 summary="The review conversation asked you a question. It continues when you answer.")
  elif value["state"] != "complete" and last and last.status in {"stopped", "failed", "interrupted"}:
    reason = _last_reply_error(chat)
    value.update(state="needs_you", execution_state=last.status, summary=(
      f"The review conversation stopped: {reason}" if reason
      else "The review conversation stopped. Open it to continue the remaining work."))
  elif last:
    value["execution_state"] = last.status
  return value


def _parent(db, row, principal):
  if principal.delegation_id is not None or principal.chat_id != row.chat_id:
    raise HTTPException(403, "Only this review's owning conversation may repair or merge.")
  _assert_execution_live(db, row, principal)
  frozen = row.options_json or {}
  if frozen.get("model"):
    chat = db.query(models.Chat).populate_existing().filter_by(id=row.chat_id).first()
    settings = coerce_agent_settings(chat.agent_settings_json) if chat else {}
    if not chat or chat.provider != frozen["provider"] or settings.get("model") != frozen["model"] or settings.get("effort") != frozen.get("reasoning_effort"):
      raise HTTPException(409, "The owning conversation's model choice changed. This frozen workflow cannot continue under a different choice.")


def _target(row, body):
  original = next((t for t in row.targets_json if reviews.key(t) == reviews.key(body.model_dump())), None)
  target = reviews.effective_target(row, original) if original else None
  if target is None or target["head_sha"] != body.head_sha:
    raise HTTPException(409, "This head has no approved selection or guarded successor receipt.")
  return target


def _assert_independent_reviewer(row, child, target):
  # Trusted execution is the current delegation contract. Read-only is this
  # server-created task's instruction, not a retired execution permission mode.
  if child.scope != "write":
    raise HTTPException(403, "Legacy read-mode reviewers cannot supply new evidence. Approve a fresh review.")
  if child.parent_chat_id != row.chat_id:
    raise HTTPException(403, "This independent reviewer belongs to a different owning conversation.")
  previous = row.outcomes_json.get(reviews.key(target), {})
  step = next((s for s in previous.get("reviewer_steps", [])
    if s.get("delegation_id") == child.id and s.get("head_sha") == target["head_sha"]
    and s.get("base_sha") == target["base_sha"]), None)
  if step is None:
    raise HTTPException(403, "Only the server-registered independent reviewer for this exact head and base may record evidence.")
  frozen = row.options_json or {}
  expected_prompt = hashlib.sha256(reviews.independent_brief(row, target).encode("utf-8")).hexdigest()
  if (child.prompt_sha256 != expected_prompt or child.provider != frozen.get("provider")
      or child.model != frozen.get("model") or child.effort != frozen.get("reasoning_effort")
      or any(step.get(k) != getattr(child, k) for k in ("prompt_sha256", "provider", "model", "effort"))):
    raise HTTPException(409, "This child does not match the run-frozen independent review prompt and model.")


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/independent-reviews",
             dependencies=[Depends(reject_cross_site)])
async def independent_review(app_id: int | None, run_id: str, body: ReviewOutcome,
                             db: Session = Depends(get_db),
                             principal: Principal = Depends(get_agent_run_principal)):
  row = _row(db, app_id, run_id, principal)
  child = db.query(models.Delegation).filter_by(id=principal.delegation_id,
    parent_chat_id=row.chat_id, child_chat_id=principal.chat_id).first() if principal.delegation_id else None
  if child is None or child.cancelled_at is not None:
    raise HTTPException(403, "A separate server-created independent reviewer must record this evidence.")
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    target = _target(row, body)
    _assert_independent_reviewer(row, child, target)
    if body.reviewed_base_sha != target["base_sha"]:
      raise HTTPException(409, "Review evidence must name the exact target base.")
    if body.state == "all_clear" and (set(body.scope) != SCOPE or body.tests_passed is not True or not body.tests.strip()):
      raise HTTPException(422, "Independent all-clear requires full-diff scope and passing test evidence.")
    cwd = Path(get_settings().data_dir) / "platform"
    await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
    await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
    _assert_app_current(db, row.app_id, row.app_nonce)
    db.refresh(child)
    if child.cancelled_at is not None:
      raise HTTPException(409, "This independent reviewer was cancelled during preflight.")
    if child.child_chat_id != principal.chat_id:
      raise HTTPException(409, "The independent reviewer conversation changed during preflight.")
    db.refresh(row)
    _assert_independent_reviewer(row, child, _target(row, body))
    active = db.query(models.ChatRun).filter_by(id=principal.run_id,
      chat_id=principal.chat_id, status="running").first()
    parent_live = db.query(models.ChatRun.id).filter_by(chat_id=row.chat_id, status="running").first()
    if not active or not parent_live:
      raise HTTPException(409, "Review execution stopped. No new evidence admitted.")
    previous = row.outcomes_json.get(reviews.key(target), {})
    receipts = list(previous.get("independent_reviews", []))
    # Retries of one child execution cannot manufacture fresh review rounds.
    existing = next((r for r in receipts if r["review_run_id"] == principal.run_id), None)
    receipt = {"id": str(uuid.uuid4()), "head_sha": body.head_sha,
      "base_sha": body.reviewed_base_sha, "state": body.state, "summary": body.summary,
      "scope": body.scope, "tests": body.tests, "tests_passed": body.tests_passed,
      "review_chat_id": principal.chat_id, "review_run_id": principal.run_id,
      "delegation_id": child.id}
    if existing:
      if any(existing[k] != receipt[k] for k in receipt if k != "id"):
        raise HTTPException(409, "This independent review execution already recorded different evidence.")
      return {"independent_receipt_id": existing["id"], "run": _run_view(db, row)}
    receipts.append(receipt)
    reviews.save_outcome(db, row, reviews.key(target), {**previous,
      "state": previous.get("state", "reviewing"), "independent_reviews": receipts})
    return {"independent_receipt_id": receipt["id"], "run": _run_view(db, row)}


class RepairCheckout(PullIdentity):
  findings: str = Field(min_length=1, max_length=4000)


class RepairPublish(PullIdentity):
  summary: str = Field(min_length=1, max_length=4000)
  tests: str = Field(min_length=1, max_length=4000)
  tests_passed: bool


def _repair_allowed(row):
  if row.mode != "review_fix_merge" or (row.options_json or {}).get("confirmation_scope") not in TAKEOVER_SCOPES:
    raise HTTPException(403, "This mode does not authorize public repairs. Confirm scoped takeover separately.")


def _require_ready_attempt_resolved(previous):
  attempt = previous.get("ready_attempt")
  if attempt and attempt.get("state") != "ready":
    raise HTTPException(409, "The draft readiness attempt is uncertain. Reconcile it read-only before any new public action.")


class DraftReady(PullIdentity):
  reviewed_base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
  independent_receipt_id: str = Field(min_length=1, max_length=64)
  summary: str = Field(min_length=1, max_length=4000)
  scope: list[str] = Field(min_length=6, max_length=6)
  tests: str = Field(min_length=1, max_length=4000)
  tests_passed: bool


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/ready",
             dependencies=[Depends(reject_cross_site)])
async def mark_draft_ready(app_id: int | None, run_id: str, body: DraftReady,
                           db: Session = Depends(get_db),
                           principal: Principal = Depends(get_agent_run_principal)):
  row = _row(db, app_id, run_id, principal)
  if not reviews.draft_ready_allowed(row):
    raise HTTPException(403, "This frozen grant does not authorize marking a draft ready for review.")
  _parent(db, row, principal)
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    target = _target(row, body)
    item_key = reviews.key(target)
    previous = (row.outcomes_json or {}).get(item_key, {})
    cwd = Path(get_settings().data_dir) / "platform"
    attempt = previous.get("ready_attempt")
    if attempt:
      # The mutation is unrepeatable after admission, including lost responses.
      try:
        live_repo, live = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
      except Exception:
        return {"run": _run_view(db, row), "blocked": "The earlier readiness attempt is uncertain; it was not repeated."}
      if live.get("state") == "open" and live.get("draft") is False:
        if not reviews.merge_permission(live_repo):
          return {"run": _run_view(db, row), "blocked": "The earlier readiness attempt is visible, but write permission changed; no action was repeated."}
        actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
        if str(actor.get("id") or "") != row.github_actor_id:
          return {"run": _run_view(db, row), "blocked": "The earlier readiness attempt is visible, but the GitHub actor changed; no action was repeated."}
        try:
          await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
        except HTTPException:
          return {"run": _run_view(db, row), "blocked": "The target base changed; exact readiness evidence cannot be confirmed."}
        if attempt.get("state") != "ready":
          reviews.save_outcome(db, row, item_key, {**previous,
            "ready_attempt": {**attempt, "state": "ready"}})
        return {"run": _run_view(db, row)}
      return {"run": _run_view(db, row), "blocked": "The earlier readiness attempt is not confirmed; it was not repeated."}
    reviews.require_public_transition_clear(db, row, target)
    if body.tests_passed is not True or set(body.scope) != SCOPE or not body.tests.strip():
      raise HTTPException(422, "Fresh full-rubric review and passing tests are required before draft readiness.")
    reviews.require_independent_clear(row, target, body)
    async with _github_connection_transaction():
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed. Approve a new selection.")
      repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
      await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
      checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
      blocker = reviews.readiness_blocker(target, repo, pull, checks)
      if blocker:
        raise HTTPException(409, blocker)
      _parent(db, row, principal)
      # GitHub has no expected-head argument for this mutation. Minimize the
      # race by re-reading identity, base, rights and checks after preflight.
      repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
      await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
      checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
      blocker = reviews.readiness_blocker(target, repo, pull, checks)
      if blocker:
        raise HTTPException(409, blocker)
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed during preflight.")
      _parent(db, row, principal)
      receipt = {"id": str(uuid.uuid4()), "state": "attempting",
        "head_sha": target["head_sha"], "base_sha": target["base_sha"],
        "independent_receipt_id": body.independent_receipt_id,
        "review_run_id": principal.run_id, "summary": body.summary,
        "tests": body.tests, "tests_passed": True, "scope": body.scope}
      reviews.arm_ready(db, row, target, receipt, principal)
      try:
        _parent(db, row, principal)
        await asyncio.to_thread(reviews.mark_ready, _gh, cwd, target)
        # The GraphQL response confirms the PR/head but not the target base.
        # A second live read keeps a post-mutation base drift uncertain.
        confirmed_repo, confirmed = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
        await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
        if (not reviews.merge_permission(confirmed_repo) or confirmed.get("state") != "open"
            or confirmed.get("draft") is not False):
          raise HTTPException(409, "GitHub did not confirm the exact draft is ready.")
        actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
        if str(actor.get("id") or "") != row.github_actor_id:
          raise HTTPException(409, "The connected GitHub actor changed after the readiness attempt.")
        _parent(db, row, principal)
      except Exception:
        receipt["state"] = "unknown"
      else:
        receipt["state"] = "ready"
      db.refresh(row)
      previous = row.outcomes_json[item_key]
      reviews.save_outcome(db, row, item_key, {**previous, "ready_attempt": receipt})
    return {"run": _run_view(db, row), **({"blocked": "The readiness response is uncertain; observe read-only before proceeding."} if receipt["state"] == "unknown" else {})}


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/repair-checkout",
             dependencies=[Depends(reject_cross_site)])
async def repair_checkout(app_id: int | None, run_id: str, body: RepairCheckout,
                          db: Session = Depends(get_db),
                          principal: Principal = Depends(get_agent_run_principal)):
  from app import contribution_review_repairs as repairs
  row = _row(db, app_id, run_id, principal)
  _repair_allowed(row)
  _parent(db, row, principal)
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    target = _target(row, body)
    previous = row.outcomes_json.get(reviews.key(target), {})
    _require_ready_attempt_resolved(previous)
    if previous.get("merge_attempted") or previous.get("state") in {"merged", "queued", "merge_unknown"}:
      raise HTTPException(409, "An existing merge attempt must be reconciled before any repair.")
    attempts = previous.get("repair_attempts", [])
    if any(r.get("state") in {"pushing", "push_unknown"} for r in attempts):
      raise HTTPException(409, "An earlier push has an unclear public outcome. Reconcile it; do not prepare another repair.")
    reviews.require_public_transition_clear(db, row, target)
    reviews.require_repair_round_available(row, attempts)
    if previous.get("checkout") and previous["checkout"].get("initial_head_sha") == target["head_sha"]:
      return {"checkout": previous["checkout"], "run": _run_view(db, row)}
    cwd = Path(get_settings().data_dir) / "platform"
    async with _github_connection_transaction():
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed. Approve a new selection.")
      # Scope was frozen during admission, never supplied by an editing agent.
      allowed = target.get("allowed_files")
      if not allowed:
        raise HTTPException(409, "This grant has no frozen repair file scope. Approve a fresh selection.")
      try:
        checkout = await asyncio.to_thread(repairs.prepare_checkout, _gh, cwd, row, target, allowed)
      except HTTPException as exc:
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "needs_you", "summary": str(exc.detail)})
        raise
      _parent(db, row, principal)
    reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "repairing",
      "checkout": checkout, "findings": body.findings, "head_sha": target["head_sha"]})
    return {"checkout": checkout, "run": _run_view(db, row)}


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/repairs",
             dependencies=[Depends(reject_cross_site)])
async def publish_repair(app_id: int | None, run_id: str, body: RepairPublish,
                         db: Session = Depends(get_db),
                         principal: Principal = Depends(get_agent_run_principal)):
  from app import contribution_review_repairs as repairs
  row = _row(db, app_id, run_id, principal)
  _repair_allowed(row)
  _parent(db, row, principal)
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    original = next((t for t in row.targets_json if reviews.key(t) == reviews.key(body.model_dump())), None)
    if original is None:
      raise HTTPException(409, "This PR is not named in the takeover grant.")
    previous = row.outcomes_json.get(reviews.key(original), {})
    attempts = list(previous.get("repair_attempts", []))
    pending = next((r for r in attempts if r.get("from_sha") == body.head_sha), None)
    cwd = Path(get_settings().data_dir) / "platform"
    if pending:
      # Read-only reconciliation, including after a lost push response. Never
      # re-push even if the server process died after persisting the attempt.
      if pending["state"] == "pushed":
        return {"run": _run_view(db, row)}
      candidate = {**original, "head_sha": pending["head_sha"], "base_sha": pending["base_sha"]}
      try:
        await asyncio.to_thread(reviews.current_pull, _gh, cwd, candidate)
      except Exception:
        return {"run": _run_view(db, row), "blocked": "The earlier push is not confirmed. It was not repeated."}
      _parent(db, row, principal)
      attempts = [{**r, "state": "pushed"} if r is pending else r for r in attempts]
      reviews.save_outcome(db, row, reviews.key(original), {**previous,
        "repair_attempts": attempts, "successor": {"head_sha": pending["head_sha"], "base_sha": pending["base_sha"]},
        "head_sha": pending["head_sha"], "state": "reviewing"})
      return {"run": _run_view(db, row)}
    _require_ready_attempt_resolved(previous)
    target = _target(row, body)
    if body.tests_passed is not True:
      raise HTTPException(422, "Failed or unknown tests block public repair publication.")
    if not body.summary.strip() or not body.tests.strip():
      raise HTTPException(422, "Concrete repair and test evidence are required.")
    checkout = previous.get("checkout")
    if not checkout or checkout.get("initial_head_sha") != target["head_sha"]:
      raise HTTPException(409, "Prepare this exact head in its dedicated server checkout first.")
    reviews.require_repair_round_available(row, attempts)
    async with _github_connection_transaction():
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed. No push started.")
      current_base = await asyncio.to_thread(reviews.base_head, _gh, cwd, target["repo"], target["base_ref"])
      try:
        validation = await asyncio.to_thread(repairs.validate_repair, _gh, cwd, row, target,
                                             checkout, target["allowed_files"])
      except HTTPException as exc:
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "needs_you", "summary": str(exc.detail)})
        raise
      if await asyncio.to_thread(reviews.base_head, _gh, cwd, target["repo"], target["base_ref"]) != current_base:
        raise HTTPException(409, "The target base moved during repair validation. Test the new combined result first.")
      validation["base_sha"] = current_base
      _parent(db, row, principal)
      receipt = {**validation, "id": str(uuid.uuid4()), "state": "pushing",
        "from_sha": target["head_sha"], "summary": body.summary, "tests": body.tests,
        "tests_passed": True, "author_chat_id": principal.chat_id, "author_run_id": principal.run_id}
      owned = reviews.arm_repair(db, row, target, previous, receipt, principal)
      if owned:
        return {"run": _run_view(db, row), "blocked": owned}
      db.refresh(row)
      previous = row.outcomes_json[reviews.key(target)]
      attempts = list(previous["repair_attempts"])
      try:
        # Stop and nonce rechecked after claim acquisition commits too.
        _parent(db, row, principal)
        loop = asyncio.get_running_loop()
        async def final_push_guard():
          _parent(db, row, principal)
        def before_push():
          asyncio.run_coroutine_threadsafe(final_push_guard(), loop).result()
        # push_repair confirms the new head from the branch ref. Re-reading the
        # PR here would race GitHub's asynchronous PR head update.
        await asyncio.to_thread(repairs.push_repair, _gh, cwd, row, target, validation,
                                before_push=before_push)
        _parent(db, row, principal)
      except Exception:
        attempts[-1] = {**attempts[-1], "state": "push_unknown"}
        reviews.save_outcome(db, row, reviews.key(target), {**previous,
          "repair_attempts": attempts, "state": "needs_you",
          "summary": "The public push outcome is not confirmed. Reconcile it without repeating the push."})
      else:
        attempts[-1] = {**attempts[-1], "state": "pushed"}
        reviews.save_outcome(db, row, reviews.key(target), {**previous,
          "repair_attempts": attempts, "successor": {"head_sha": validation["head_sha"], "base_sha": validation["base_sha"]},
          "head_sha": validation["head_sha"], "state": "reviewing", "summary": "Repair published; fresh independent full-diff review required."})
    return {"run": _run_view(db, row)}


# Core workflows deliberately use no app identity and no app-install capability.
@router.post("/api/github/review-runs", dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_start_reviews(body: StartReviews, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await start_reviews(None, body, db, principal)


@router.get("/api/github/review-runs")
def core_list_reviews(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return list_reviews(None, db, principal)


@router.get("/api/github/review-runs/{run_id}")
def core_get_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return get_review(None, run_id, db, principal)


@router.get("/api/github/review-presets")
def core_review_presets(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  _authorize_context(db, None, principal)
  return {"presets": presets.presets(), "capabilities": REVIEW_CAPABILITIES}


@router.post("/api/github/review-preview", dependencies=[Depends(reject_cross_site)])
def core_review_preview(body: ReviewPreview, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  _authorize_context(db, None, principal)
  snapshot = _snapshot(db, body, principal)
  return {"options": snapshot, "preview_sha256": _fingerprint(snapshot)}


@router.post("/api/github/review-runs/{run_id}/outcomes", dependencies=[Depends(reject_cross_site)])
async def core_report_outcome(run_id: str, body: ReviewOutcome, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await report_outcome(None, run_id, body, db, principal)


@router.post("/api/github/review-runs/{run_id}/independent-reviews", dependencies=[Depends(reject_cross_site)])
async def core_independent_review(run_id: str, body: ReviewOutcome, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await independent_review(None, run_id, body, db, principal)


@router.post("/api/github/review-runs/{run_id}/repair-checkout", dependencies=[Depends(reject_cross_site)])
async def core_repair_checkout(run_id: str, body: RepairCheckout, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await repair_checkout(None, run_id, body, db, principal)


@router.post("/api/github/review-runs/{run_id}/repairs", dependencies=[Depends(reject_cross_site)])
async def core_publish_repair(run_id: str, body: RepairPublish, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await publish_repair(None, run_id, body, db, principal)


@router.post("/api/github/review-runs/{run_id}/ready", dependencies=[Depends(reject_cross_site)])
async def core_mark_draft_ready(run_id: str, body: DraftReady, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await mark_draft_ready(None, run_id, body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/stop",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def stop_review(app_id: int | None, run_id: str, db: Session = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
  from app.chat import stop_chat_for
  row = _row(db, app_id, run_id, principal)
  # An app may stop only the dedicated app-created review chat, never the
  # owner's chat where a conversational approval happened to be recorded.
  chat = db.get(models.Chat, row.chat_id)
  if principal.app_id is not None and (chat is None or chat.created_by_app_id != principal.app_id):
    raise HTTPException(403, "Open the owning conversation to stop this owner-chat workflow.")
  # Stopping a run stops the chat executing it. From that chat's own agent this
  # would silently end the caller's turn mid-command, never what it asked for.
  if principal.chat_id is not None and principal.chat_id == row.chat_id:
    raise HTTPException(409, "This review run belongs to your own chat, so stopping it would stop "
                             "your current turn. A superseded run is bound to its exact head and "
                             "needs no stop; start a fresh run for the new head instead.")
  # Goal holds accept only these actors; anything else reads as an unexplained interruption.
  actor = ("owner" if is_owner_input_principal(principal)
           else "agent" if principal.run_id is not None else "unknown")
  stopped, _ = await stop_chat_for(row.chat_id, db=db, actor=actor,
                                   actor_id=principal.run_id or principal.chat_id)
  db.refresh(row)
  return {"stopped": stopped, "run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/stop",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_stop_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await stop_review(None, run_id, db, principal)


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/reviewers",
             dependencies=[Depends(reject_cross_site)])
async def start_independent_reviewer(app_id: int | None, run_id: str, body: PullIdentity,
                                     db: Session = Depends(get_db),
                                     principal: Principal = Depends(get_agent_run_principal)):
  from app.delegations import (DelegationIntent, create_or_attach_delegation,
    ensure_delegation_started, serialize_delegation, parent_root_run_id)
  row = _row(db, app_id, run_id, principal)
  _parent(db, row, principal)
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    async with AsyncExitStack() as admission:
      if app_id is not None:
        await admission.enter_async_context(chat_queue.get_transition_lock(f"app-lifecycle:{app_id}"))
      await admission.enter_async_context(chat_queue.get_transition_lock(row.chat_id))
      db.refresh(row)
      _parent(db, row, principal)
      target = _target(row, body)
      previous = row.outcomes_json.get(reviews.key(target), {})
      frozen = row.options_json or {}
      if not frozen.get("model"):
        raise HTTPException(409, "A fresh selection with run-frozen prompt/model is required.")
      prompt = reviews.independent_brief(row, target)
      task_key = "review-" + hashlib.sha256(f"{row.id}:{reviews.key(target)}:{target['head_sha']}:{target['base_sha']}".encode()).hexdigest()
      existing = db.query(models.Delegation).filter_by(parent_chat_id=row.chat_id, task_key=task_key).first()
      if existing:
        # Never revive a stopped child when observing a saved step, even across
        # parent turns with a fresh logical root id.
        _assert_independent_reviewer(row, existing, target)
        if not db.query(models.ChatRun.id).filter_by(chat_id=existing.child_chat_id).first():
          await ensure_delegation_started(db, existing, start_turn=start_programmatic_chat_turn)
          _parent(db, row, principal)
        return {"delegation": serialize_delegation(db, existing), "run": _run_view(db, row)}
      root_id = parent_root_run_id(db, row.chat_id, require_active=True)
      if not root_id:
        raise HTTPException(409, "The owning review stopped before reviewer admission.")
      intent = DelegationIntent(app_id=app_id, parent_chat_id=row.chat_id,
        parent_root_run_id=root_id, task_key=task_key, prompt=prompt,
        provider=frozen["provider"], model=frozen["model"], effort=frozen.get("reasoning_effort"),
        cwd=str(Path(get_settings().data_dir)), notify_parent_on_complete=True)
      def register_reviewer(admitted):
        reviews.write_outcome(db, row, reviews.key(target), {**previous,
          "state": "reviewing", "head_sha": target["head_sha"],
          "reviewer_steps": [*previous.get("reviewer_steps", []), {
            "delegation_id": admitted.id, "head_sha": target["head_sha"], "base_sha": target["base_sha"],
            "prompt_sha256": admitted.prompt_sha256, "provider": admitted.provider, "model": admitted.model,
            "effort": admitted.effort}]})
      child, _ = create_or_attach_delegation(db, intent, admit_child=register_reviewer)
      db.refresh(row)
      _assert_independent_reviewer(row, child, target)
      _parent(db, row, principal)
      await ensure_delegation_started(db, child, prompt, start_turn=start_programmatic_chat_turn)
      _parent(db, row, principal)
      return {"delegation": serialize_delegation(db, child), "run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/reviewers", dependencies=[Depends(reject_cross_site)])
async def core_start_independent_reviewer(run_id: str, body: PullIdentity, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await start_independent_reviewer(None, run_id, body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/observe",
             dependencies=[Depends(reject_cross_site)])
async def observe_review(app_id: int | None, run_id: str, db: Session = Depends(get_db),
                          principal: Principal = Depends(get_principal)):
  """Read-only GitHub reconciliation: never starts agents or public actions.

  The existing durable Wait owner may call this after resumption. It also
  settles lost receipts while a chat is stopped without reviving that chat.
  """
  row = _row(db, app_id, run_id, principal)
  cwd = Path(get_settings().data_dir) / "platform"
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    for original in row.targets_json:
      previous = row.outcomes_json.get(reviews.key(original), {})
      if not previous:
        continue
      ready_attempt = previous.get("ready_attempt")
      if ready_attempt and ready_attempt.get("state") != "ready":
        target = reviews.effective_target(row, original)
        try:
          live_repo, live = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
        except Exception:
          live = None
        if (live and reviews.merge_permission(live_repo) and live.get("state") == "open"
            and live.get("draft") is False):
          try:
            await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
          except HTTPException:
            pass
          else:
            actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")
            if str(actor.get("id") or "") == row.github_actor_id:
              _assert_app_current(db, row.app_id, row.app_nonce)
              reviews.save_outcome(db, row, reviews.key(original), {**previous,
                "ready_attempt": {**ready_attempt, "state": "ready"}})
              previous = row.outcomes_json[reviews.key(original)]
      attempts = list(previous.get("repair_attempts", []))
      pending = next((a for a in attempts if a.get("state") in {"pushing", "push_unknown"}), None)
      if pending:
        candidate = {**original, "head_sha": pending["head_sha"], "base_sha": pending["base_sha"]}
        try:
          await asyncio.to_thread(reviews.current_pull, _gh, cwd, candidate)
        except Exception:
          continue
        _assert_app_current(db, row.app_id, row.app_nonce)
        attempts = [{**a, "state": "pushed"} if a is pending else a for a in attempts]
        reviews.save_outcome(db, row, reviews.key(original), {**previous,
          "repair_attempts": attempts, "successor": {"head_sha": pending["head_sha"], "base_sha": pending["base_sha"]},
          "head_sha": pending["head_sha"], "state": "reviewing",
          "summary": "The exact earlier repair push is confirmed; fresh independent review is still required."})
        previous = row.outcomes_json[reviews.key(original)]
      target = reviews.effective_target(row, original)
      attempted = previous.get("merge_attempted") is True or previous.get("state") in {"merging", "merge_unknown", "queued", "merged"}
      try:
        _repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)
        if attempted and pull.get("merged"):
          outcome = {**previous, "state": "merged", "merge_sha": pull.get("merge_commit_sha")}
        elif attempted:
          checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)
          entry = reviews.queue_entry(checks, target)
          outcome = ({**previous, "state": "queued", "queue_entry_id": entry["id"]} if entry else
            {**previous, "state": "merge_unknown", "summary": "The exact earlier merge/queue attempt remains unclear. It was not repeated."})
        elif previous.get("state") == "all_clear":
          await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)
          continue
        else:
          continue
      except HTTPException as exc:
        outcome = {**previous, "state": "needs_you", "summary": str(exc.detail), "merge_attempted": attempted}
      _assert_app_current(db, row.app_id, row.app_nonce)
      if outcome != previous:
        reviews.save_outcome(db, row, reviews.key(original), outcome)
    return {"run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/observe", dependencies=[Depends(reject_cross_site)])
async def core_observe_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await observe_review(None, run_id, db, principal)
