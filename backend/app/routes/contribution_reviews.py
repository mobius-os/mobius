"""Owner-selected PR review runs; exact public merge is a separate DB grant."""
import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import chat_queue, chat_writer, contribution_review_runs as reviews, models, providers, transcript_rows, settled
from app import contribution_review_presets as presets
from app import contribution_review_public as public
from app import contribution_review_repair_actions as repair_actions
from app.contribution_review_public_actions import post_public_review
from app.contribution_review_public import EVIDENCE_MAX_CHARS
from app import contribution_review_merge_actions as merge_actions
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
_log = logging.getLogger(__name__)
APP_PREFIX = "/api/github/contributions"
SCOPE = {"correctness", "maintainability", "simplicity", "tests", "security_privacy", "technical_debt"}
REVIEW_CAPABILITIES = {"post_review": True, "review_fix_merge": True}
REVIEW_STOP_KEY = "_review_stop"


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
  confirmation_scope: Literal["named_pr_repairs_and_reviewed_successors"] | None = None
  items: list[SelectedPR] = Field(min_length=1, max_length=20)
  chat_approval: ChatApproval | None = None


class ReviewOutcome(PullIdentity):
  publish: bool = Field(default=False, strict=True)
  state: Literal["all_clear", "needs_you"]
  summary: str = Field(min_length=1, max_length=EVIDENCE_MAX_CHARS)
  scope: list[str] = Field(default_factory=list, max_length=6)
  tests: str = Field(default="", max_length=EVIDENCE_MAX_CHARS)
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
  db.refresh(row)
  if _review_stop_requested(row):
    raise HTTPException(409, "This review was stopped. No new action was started.")
  active = db.query(models.ChatRun.id).filter_by(
    id=principal.run_id, chat_id=row.chat_id, status="running",
  ).first()
  if not active:
    raise HTTPException(409, "This review stopped or its authorization changed. No merge was started.")
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


def _options_input(body):
  if body.options is None:
    return None
  options = body.options.model_dump(exclude_none=True, exclude_unset=True)
  mode = getattr(body, "mode", "review")
  if mode != "review_fix_merge" and ("fix_prompt" in body.options.model_fields_set
                                     or "max_rounds" in body.options.model_fields_set):
    raise HTTPException(422, "fix_prompt and max_rounds are not supported outside review_fix_merge mode")
  if options.get("autopilot") is True:
    raise HTTPException(422, "autopilot is not supported by this review-only workflow")
  selected = {key: options[key] for key in presets.PROMPT_KEYS if key in options}
  if mode == "review_fix_merge":
    if "fix_prompt" in options:
      selected["fix_prompt"] = options["fix_prompt"]
    if "max_rounds" in body.options.model_fields_set:
      selected["max_rounds"] = body.options.max_rounds
  if options.get("post_review") is True:
    selected["post_review"] = True
  return selected



def _saved_options_input(row):
  """Compare legacy no-op defaults without rewriting frozen consent.

  These fields are unused only in review/review_merge. A takeover grant may
  enforce them, so its exact option comparison must remain untouched.
  """
  saved = row.options_json.get("input_options")
  if row.mode not in {"review", "review_merge"} or not isinstance(saved, dict):
    return saved
  normalized = dict(saved)
  for key, default in (("max_rounds", None), ("autopilot", False),
                       ("fix_prompt", presets.FIX_PROMPT)):
    if key in normalized and type(normalized[key]) is type(default) and normalized[key] == default:
      del normalized[key]
  return normalized


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
      if not actual.get("model"):
        raise HTTPException(409, "The owning chat has no snapshotted model. Select one before previewing this review.")
      if requested and any(actual.get(k) != choice.get(k) for k in ("provider", "model", "effort")):
        raise HTTPException(409, "The owning chat is executing a different model. Preview its actual run choice.")
      choice = {k: actual[k] for k in ("provider", "model", "effort") if k in actual}
    else:
      raise HTTPException(409, "The owning chat is unavailable. No review was started.")
  try:
    return presets.resolve_options(db, _options_input(body), choice=choice,
                                   mode=getattr(body, "mode", "review"))
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
    raise HTTPException(403, "Explicit approval in the owning chat is required. Otherwise use an owner-approved selection flow.")
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
  if body.mode == "review_fix_merge" and body.confirmation_scope != "named_pr_repairs_and_reviewed_successors":
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
            _saved_options_input(row) != (_options_input(body) or {})
            or row.options_json.get("input_agent") != (body.agent.model_dump() if body.agent else None)
            or row.options_json.get("confirmation_scope") != body.confirmation_scope
            or (body.preview_sha256 and row.options_json.get("preview_sha256") != body.preview_sha256)))):
        raise HTTPException(409, "This request already approves a different selection.")
      if approval and row.chat_id != principal.chat_id:
        raise HTTPException(409, {"message": "This selection already belongs to another review conversation. Follow it instead of starting again.",
                                  "chat_id": row.chat_id, "run_id": row.id})
    else:
      if body.options is not None and body.mode != "review_fix_merge" and (
          body.options.fix_prompt is not None or "max_rounds" in body.options.model_fields_set):
        raise HTTPException(422, "fix_prompt and max_rounds require review_fix_merge mode.")
      if body.mode == "review_fix_merge" and not body.preview_sha256:
        raise HTTPException(422, "Preview and confirm the exact frozen takeover prompts/model before admission.")
      snapshot = _snapshot(db, body, principal)
      if body.preview_sha256 and body.preview_sha256 != _fingerprint(snapshot):
        raise HTTPException(409, "The resolved prompts or model changed. Preview and confirm again.")
      cwd = Path(get_settings().data_dir) / "platform"
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub observation; no mutation)
      if not actor.get("id"):
        raise HTTPException(409, "Connect GitHub before starting this review.")
      targets = [await asyncio.to_thread(reviews.inspect_target, _gh, cwd, item, body.mode)  # unsettled: read-only (GitHub observation; no mutation)
                 for item in selection]
      if body.mode == "review_fix_merge":
        for target in targets:
          target["allowed_files"] = await asyncio.to_thread(reviews.repair_file_scope, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
          await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
          await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
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
      if not _frozen_model_matches(frozen, chat):
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
  return {"runs": [_list_run_view(db, row, app_id) for row in rows]}


def _list_run_view(db, row, app_id):
  """Keep owning clients compatible; bound only privacy-projected navigation."""
  value = _run_view(db, row, context_app_id=app_id)
  if app_id is None or row.app_id == app_id:
    return value
  fields = ("repo", "number", "head_sha", "base_ref", "base_sha", "state", "url", "title",
            "review_chat_id", "review_run_id", "merge_sha", "queue_entry_id",
            "successor", "approved_head_sha", "approved_base_sha", "current_base_sha")
  items = []
  for item in value["items"]:
    brief = {key: item[key] for key in fields if key in item}
    if item.get("summary"):
      summary = item["summary"]
      if len(summary) > 240:
        omission = "… open this run for the full findings"
        brief.update(summary=summary[:240-len(omission)] + omission, summary_truncated=True)
      else:
        brief["summary"] = summary
    if item.get("public_review"):
      receipt = item["public_review"]
      brief["public_review"] = {key: receipt[key] for key in ("state", "url", "id") if key in receipt}
    items.append(brief)
  value["items"] = items
  if "options" in value:
    value["options"] = {key: val for key, val in (value["options"] or {}).items()
      if key in {"provider", "model", "reasoning_effort", "post_review", "confirmation_scope"}}
  return value


def _reported_outcome(previous, body, row, principal):
  """Build private review evidence once; callers add only transition facts."""
  return {**previous, "state": body.state, "summary": body.summary, "scope": body.scope,
    "tests": body.tests, "tests_passed": body.tests_passed,
    "reviewed_base_sha": body.reviewed_base_sha, "head_sha": body.head_sha,
    "review_chat_id": row.chat_id, "review_run_id": principal.run_id}


async def _record_failed_live_read(db, row, item_key, body, principal, error):
  """A read failure cannot erase a receipt another worker armed during I/O."""
  async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
    _parent(db, row, principal)  # Refresh before considering any private evidence.
    current = (row.outcomes_json or {}).get(item_key, {})
    if ((current.get("public_review") or {}).get("state") in {"posting", "posted", "unknown"}
        or _merge_attempted(current)):
      return
    posting = body.publish and row.mode == "review" and (row.options_json or {}).get("post_review") is True
    outcome = _reported_outcome(current, body, row, principal)
    outcome.update(state=body.state if posting else "needs_you", live_blocker=str(error.detail))
    if posting:
      outcome["public_review"] = {"state": "not_dispatched", "summary": str(error.detail)}
    reviews.save_outcome(db, row, item_key, outcome)


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


def _frozen_model_matches(frozen, chat) -> bool:
  if not frozen.get("model"):
    return True
  actual = coerce_agent_settings(chat.agent_settings_json) if chat else {}
  return bool(chat and chat.provider == frozen.get("provider")
              and actual.get("model") == frozen["model"]
              and actual.get("effort") == frozen.get("reasoning_effort"))


def _merge_attempted(outcome) -> bool:
  return outcome.get("merge_attempted") is True or outcome.get("state") in {"merging", "merge_unknown", "queued", "merged"}


async def _reconcile_merge(previous, target, pull, cwd):
  if pull.get("merged"):
    return {**previous, "state": "merged", "merge_sha": pull.get("merge_commit_sha")}
  checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
  entry = reviews.queue_entry(checks, target)
  if entry:
    return {**previous, "state": "queued", "queue_entry_id": entry["id"]}
  return {**previous, "state": "merge_unknown", "summary":
    "The earlier merge or queue request is not confirmed. Check GitHub; it was not repeated."}


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
    if public.require_same_evidence(previous, body.model_dump()):
      return {"run": _run_view(db, row)}
    attempted = _merge_attempted(previous)
    if previous.get("state") == "merged":
      return {"run": _run_view(db, row)}
    if row.mode == "review" and previous.get("state") == "all_clear":
      if not (row.options_json or {}).get("post_review"):
        return {"run": _run_view(db, row)}
      # Failed-live-read reporting also writes evidence without arming a post.
      # Preserve this precheck as well as the concurrent writer-fenced check.
      if public.evidence(previous) != public.evidence(body.model_dump()):
        raise HTTPException(409, "Retry the public review with its recorded evidence unchanged.")
    if body.state == "all_clear" and (set(body.scope) != SCOPE or not body.tests.strip() or body.tests_passed is False):
      raise HTTPException(422, "Record the complete review scope and test evidence before all clear.")
    if body.publish and not (row.mode == "review" and (row.options_json or {}).get("post_review") is True):
      raise HTTPException(403, "This grant does not authorize a public review.")
    if row.mode == "review" and not attempted and not body.publish:
      # Private findings are facts about the selected head, not the live PR.
      # A moved or temporarily unreadable PR must not replace their evidence.
      async with chat_queue.get_transition_lock(f"review-admission:{run_id}"):
        _parent(db, row, principal)
        current = (row.outcomes_json or {}).get(item_key, {})
        if public.require_same_evidence(current, body.model_dump()):
          return {"run": _run_view(db, row)}
        if current != previous:
          raise HTTPException(409, "This review changed during private reporting. Refresh before continuing.")
        reviews.save_outcome(db, row, item_key, _reported_outcome(current, body, row, principal))
      return {"run": _run_view(db, row)}
    cwd = Path(get_settings().data_dir) / "platform"
    try:
      repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation)
    except HTTPException as exc:
      await _record_failed_live_read(db, row, item_key, body, principal, exc)
      return {"run": _run_view(db, row)}
    outcome = {**_reported_outcome(previous, body, row, principal),
               "independent_receipt_id": body.independent_receipt_id}
    if attempted:
      outcome = await _reconcile_merge(previous, target, pull, cwd)
      reviews.save_effect_outcome(db, row, item_key, outcome)
      return {"run": _run_view(db, row)}
    if body.state == "all_clear" and row.mode == "review_fix_merge":
      try:
        await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)  # unsettled: read-only (GitHub base proof)
      except HTTPException as exc:
        try:
          if await _record_base_advance(db, row, original, previous, principal, cwd):
            return {"run": _run_view(db, row)}
        except HTTPException as observation_error:
          _parent(db, row, principal)
          exc = observation_error
        db.refresh(row)
        if (row.outcomes_json or {}).get(item_key, {}) != previous:
          raise HTTPException(409, "This review changed during base observation. Refresh before continuing.")
        reviews.save_outcome(db, row, item_key, {**previous, "state": "needs_you",
          "head_sha": target["head_sha"], "summary": str(exc.detail)})
        return {"run": _run_view(db, row)}
      _parent(db, row, principal)
      reviews.require_independent_clear(row, target, body)
    # A private review remains true of its pinned base after that base moves.
    # Only an actual merge candidate requires current-base proof.
    if body.state == "all_clear" and row.mode in {"review_merge", "review_fix_merge"}:
      await merge_actions.merge_reviewed_target(db, row, target, outcome, repo, pull, principal,
        gh=_gh, cwd=cwd, authorize=lambda: _parent(db, row, principal))
      return {"run": _run_view(db, row)}
    if (body.publish and row.mode == "review" and (row.options_json or {}).get("post_review") is True
        and outcome.get("state") in {"all_clear", "needs_you"}
        and (previous.get("public_review") or {}).get("state") not in {"posting", "posted", "unknown"}):
      outcome = await post_public_review(db, row, item_key, target, outcome, principal, cwd,
        gh=_gh, authorize=lambda: _parent(db, row, principal))
    if not outcome.get("merge_attempted") and not outcome.get("public_review"):
      _parent(db, row, principal)
    if outcome.get("merge_attempted") or outcome.get("public_review"):
      reviews.save_effect_outcome(db, row, item_key, outcome)
    else:
      reviews.save_outcome(db, row, item_key, outcome)
    return {"run": _run_view(db, row)}


class ReviewPreview(BaseModel):
  mode: Literal["review", "review_merge", "review_fix_merge"] = "review"
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
  foreign_core = app_id is not None and row.app_id is None
  return {"run": _run_view(db, row, context_app_id=app_id),
          "brief": None if foreign_core else reviews.brief(row)}


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
  if value["state"] != "complete" and not _frozen_model_matches(frozen, chat):
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
  if value["state"] != "complete" and _review_stop_requested(row):
    value.update(state="needs_you", execution_state="stopped",
                 summary="This review was stopped. No new evidence or public action is admitted.")
  if context_app_id is not None and row.app_id is None:
    # Read-through is navigation only. Another app's GitHub capability does
    # not grant access to core prompts, approval words or private findings.
    items = []
    for item in value["items"]:
      public = item.get("public_review") or {}
      projected = {key: item[key] for key in ("repo", "number", "head_sha", "base_ref", "base_sha", "state") if key in item}
      if public:
        projected["public_review"] = {key: public[key] for key in ("state", "url") if key in public}
      items.append(projected)
    value = {key: value[key] for key in ("id", "chat_id", "state", "execution_state", "mode", "created_at") if key in value} | {
      "app_id": None, "can_stop": False, "post_review": bool((row.options_json or {}).get("post_review")),
      "details_in_owning_chat": True, "items": items,
    }
  return value


def _review_stop_requested(row):
  return (row.outcomes_json or {}).get(REVIEW_STOP_KEY, {}).get("state") == "requested"


def _parent(db, row, principal):
  if principal.delegation_id is not None or principal.chat_id != row.chat_id:
    raise HTTPException(403, "Only this review's owning conversation may report an outcome or merge.")
  _assert_execution_live(db, row, principal)
  frozen = row.options_json or {}
  chat = db.query(models.Chat).populate_existing().filter_by(id=row.chat_id).first()
  if not _frozen_model_matches(frozen, chat):
    raise HTTPException(409, "The owning conversation's model choice changed. This frozen workflow cannot continue under a different choice.")


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
  # Persist the run-level revocation before awaiting either the parent Stop or
  # child cancellation. A completed parent has no running root to mark stopped,
  # and child cancellation may wait or time out without latching cancelled_at.
  async with chat_queue.get_transition_lock(f"review-admission:{run_id}"):
    db.refresh(row)
    if not _review_stop_requested(row):
      reviews.save_outcome(db, row, REVIEW_STOP_KEY, {"state": "requested", "actor": actor})
  parent_stopped, _ = await stop_chat_for(row.chat_id, db=db, actor=actor,
                                          actor_id=principal.run_id or principal.chat_id)
  from app.delegations import ACTIVE_DELEGATION_STATUSES, derived_status
  from app.routes.delegations import cancel_active_for_parent
  await cancel_active_for_parent(db, row.chat_id)
  remaining = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == row.chat_id,
    models.Delegation.cancelled_at.is_(None),
  ).all()
  children_stopped = not any(derived_status(db, child, load_result=False)[0]
                             in ACTIVE_DELEGATION_STATUSES for child in remaining)
  # Sweep even when cancellation has not settled. The receipt classifier
  # protects active editors and uncertain effects; a later Stop/prepare can
  # collect what was still active at this boundary.
  if row.mode == "review_fix_merge":
    try:
      async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
        db.refresh(row)
        await settled.run_settled(repair_actions.sweep_checkout_receipts_once)
    except Exception:
      _log.exception("repair checkout GC failed after Stop for review run %s", run_id)
  db.refresh(row)
  return {"stopped": parent_stopped and children_stopped, "run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/stop",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_stop_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await stop_review(None, run_id, db, principal)


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/observe",
             dependencies=[Depends(reject_cross_site)])
async def observe_review(app_id: int | None, run_id: str, db: Session = Depends(get_db),
                          principal: Principal = Depends(get_principal)):
  """Read-only GitHub reconciliation: never starts agents or public actions.

  The existing durable Wait owner may call this after resumption. It also
  settles lost receipts while a chat is stopped without reviving that chat.
  """
  if app_id is not None:
    _authorize_context(db, app_id, principal)
    foreign_core = db.query(models.ContributionReviewRun.id).filter_by(
      id=run_id, app_id=None, owner_id=principal.owner.id).first()
    if foreign_core:
      raise HTTPException(403, {"code": "details_in_owning_chat",
        "message": "Open the owning conversation to observe this core review run."})
  row = _row(db, app_id, run_id, principal)
  cwd = Path(get_settings().data_dir) / "platform"
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    for original in row.targets_json:
      previous = row.outcomes_json.get(reviews.key(original), {})
      receipt = previous.get("public_review") or {}
      if receipt.get("state") in {"posting", "unknown"}:
        observed = await asyncio.to_thread(public.reconcile_review, _gh, cwd, original, receipt, row.github_actor_id)  # unsettled: read-only (public review receipt observation)
        reviews.save_effect_outcome(db, row, reviews.key(original), {**previous, "public_review": observed})
        continue
      target = reviews.effective_target(row, original)
      pending = next((a for a in previous.get("repair_attempts", [])
                      if a.get("state") in {"pushing", "push_unknown"}), None)
      if pending:
        if not await repair_actions.reconcile_push(db, row, original, previous, pending, gh=_gh, cwd=cwd):
          repair_actions.escalate_unconfirmed_push(db, row, original, previous, pending)
          continue
        previous = row.outcomes_json[reviews.key(original)]
      target = reviews.effective_target(row, original)
      attempted = _merge_attempted(previous)

      if not attempted:
        continue
      try:
        _repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
        outcome = await _reconcile_merge(previous, target, pull, cwd)
      except HTTPException:
        # A failed read proves no new fact; historical review evidence and the
        # exact public-attempt fence both remain intact.
        continue
      if outcome != previous:
        reviews.save_effect_outcome(db, row, reviews.key(original), outcome)
    return {"run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/observe", dependencies=[Depends(reject_cross_site)])
async def core_observe_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await observe_review(None, run_id, db, principal)


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


def _reviewer_root_allows_evidence(db, child):
  """A durable reviewer belongs to its spawning work, not today's parent turn."""
  root_id = child.parent_root_run_id
  goal = db.query(models.ChatGoal).populate_existing().filter_by(id=root_id).first()
  if goal is not None:
    return (goal.chat_id == child.parent_chat_id and goal.status == "open"
      and db.query(models.ChatRun.id).filter_by(
        chat_id=child.parent_chat_id, goal_id=root_id).first() is not None)
  root = db.query(models.ChatRun).populate_existing().filter_by(
    id=root_id, chat_id=child.parent_chat_id).first()
  if root is None:
    return False
  # A successful parent turn may finish while its child is still working.
  # Stop, including a stopped physical continuation of this root, revokes it.
  stopped = db.query(models.ChatRun.id).filter(
    models.ChatRun.chat_id == child.parent_chat_id,
    or_(models.ChatRun.id == root_id, models.ChatRun.root_run_id == root_id),
    models.ChatRun.status == "stopped",
  ).first()
  return stopped is None


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
    if _review_stop_requested(row):
      raise HTTPException(409, "This review was stopped. No new evidence admitted.")
    target = _target(row, body)
    _assert_independent_reviewer(row, child, target)
    if body.reviewed_base_sha != target["base_sha"]:
      raise HTTPException(409, "Review evidence must name the exact target base.")
    if body.state == "all_clear" and (set(body.scope) != SCOPE or body.tests_passed is not True or not body.tests.strip()):
      raise HTTPException(422, "Independent all-clear requires full-diff scope and passing test evidence.")
    cwd = Path(get_settings().data_dir) / "platform"
    await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
    await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
    # Serialize the final admission with Stop, not the remote preflight. A
    # reviewer can finish after a successful parent turn, but not after Stop.
    async with chat_queue.get_transition_lock(row.chat_id):
      _assert_app_current(db, row.app_id, row.app_nonce)
      db.refresh(child)
      if child.cancelled_at is not None:
        raise HTTPException(409, "This independent reviewer was cancelled during preflight.")
      if child.child_chat_id != principal.chat_id:
        raise HTTPException(409, "The independent reviewer conversation changed during preflight.")
      db.refresh(row)
      if _review_stop_requested(row):
        raise HTTPException(409, "This review was stopped during preflight. No new evidence admitted.")
      _assert_independent_reviewer(row, child, _target(row, body))
      active = db.query(models.ChatRun.id).filter_by(id=principal.run_id,
        chat_id=principal.chat_id, status="running").first()
      if not active or not _reviewer_root_allows_evidence(db, child):
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
      if any(r.get("delegation_id") == child.id and r.get("head_sha") == body.head_sha
             and r.get("base_sha") == body.reviewed_base_sha for r in receipts):
        raise HTTPException(409, "This reviewer delegation already reported on this head and base. Start a fresh independent reviewer.")
      receipts.append(receipt)
      reviews.save_outcome(db, row, reviews.key(target), {**previous,
        "state": previous.get("state", "reviewing"), "independent_reviews": receipts})
      return {"independent_receipt_id": receipt["id"], "run": _run_view(db, row)}


class RepairCheckout(PullIdentity):
  findings: str = Field(min_length=1, max_length=EVIDENCE_MAX_CHARS)


class RepairPublish(PullIdentity):
  summary: str = Field(min_length=1, max_length=EVIDENCE_MAX_CHARS)
  tests: str = Field(min_length=1, max_length=EVIDENCE_MAX_CHARS)
  tests_passed: bool


def _repair_allowed(row):
  if row.mode != "review_fix_merge" or (row.options_json or {}).get("confirmation_scope") != "named_pr_repairs_and_reviewed_successors":
    raise HTTPException(403, "This mode does not authorize public repairs. Confirm scoped takeover separately.")


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
    try:
      await settled.run_settled(repair_actions.sweep_checkout_receipts_once)
    except Exception:
      _log.exception("repair checkout GC failed before checkout preparation for review run %s", run_id)
    target = _target(row, body)
    previous = row.outcomes_json.get(reviews.key(target), {})
    if previous.get("merge_attempted") or previous.get("state") in {"merged", "queued", "merge_unknown"}:
      raise HTTPException(409, "An existing merge attempt must be reconciled before any repair.")
    attempts = previous.get("repair_attempts", [])
    if any(r.get("state") in {"pushing", "push_unknown"} for r in attempts):
      raise HTTPException(409, "An earlier push has an unclear public outcome. Reconcile it; do not prepare another repair.")
    reviews.require_public_transition_clear(db, row, target)
    reviews.require_repair_round_available(row, attempts)
    cwd = Path(get_settings().data_dir) / "platform"
    async with _github_connection_transaction():
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub observation; no mutation)
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed. Approve a new selection.")
      if (previous.get("checkout") and previous["checkout"].get("initial_head_sha") == target["head_sha"]
          and repairs.checkout_present(previous["checkout"].get("checkout"))):
        checkout = {**previous["checkout"], "target_base_sha": await settled.run_settled(
          repairs.fetch_target_base, _gh, cwd, Path(previous["checkout"]["checkout"]), target)}
        _parent(db, row, principal)
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "checkout": checkout})
        return {"checkout": checkout, "run": _run_view(db, row)}
      # Scope was frozen during admission, never supplied by an editing agent.
      allowed = target.get("allowed_files")
      if not allowed:
        raise HTTPException(409, "This grant has no frozen repair file scope. Approve a fresh selection.")
      try:
        checkout = await settled.run_settled(repairs.prepare_checkout, _gh, cwd, row, target, allowed)
      except HTTPException as exc:
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "needs_you", "summary": str(exc.detail)})
        raise
      try:
        _parent(db, row, principal)
      except HTTPException:
        # This newly created path has never owned a public receipt.
        await settled.run_settled(repairs.discard_checkout, checkout.get("checkout"))
        raise
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
    pending = next((r for r in attempts if r.get("from_sha") == body.head_sha
                    and r.get("state") in {"pushing", "push_unknown", "pushed"}), None)
    cwd = Path(get_settings().data_dir) / "platform"
    if pending:
      confirmed = await repair_actions.reconcile_push(db, row, original, previous, pending, gh=_gh, cwd=cwd)
      return {"run": _run_view(db, row), **({} if confirmed else {
        "blocked": "The earlier push is not confirmed. It was not repeated."})}
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
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub observation; no mutation)
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed. No push started.")
      current_base = await asyncio.to_thread(reviews.base_head, _gh, cwd, target["repo"], target["base_ref"])  # unsettled: read-only (GitHub observation; no mutation)
      await asyncio.to_thread(reviews.assert_approved_base_ancestry, _gh, cwd, target, current_base)  # unsettled: read-only (GitHub ancestry proof; no mutation)
      try:
        validation = await settled.run_settled(repairs.validate_repair, _gh, cwd, row, target,
                                             checkout, target["allowed_files"], base_sha=current_base)
      except HTTPException as exc:
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "needs_you", "summary": str(exc.detail)})
        raise
      if await asyncio.to_thread(reviews.base_head, _gh, cwd, target["repo"], target["base_ref"]) != current_base:  # unsettled: read-only (GitHub observation; no mutation)
        raise HTTPException(409, "The target base moved during repair validation. Test the new combined result first.")
      validation["base_sha"] = current_base
      receipt = {**validation, "id": str(uuid.uuid4()), "state": "pushing",
        "from_sha": target["head_sha"], "summary": body.summary, "tests": body.tests,
        "tests_passed": True, "author_chat_id": principal.chat_id, "author_run_id": principal.run_id}
      owned = await repair_actions.publish_attempt(db, row, target, validation, receipt, principal,
        gh=_gh, cwd=cwd, authorize=lambda: _parent(db, row, principal))
      if owned:
        return {"run": _run_view(db, row), "blocked": owned}
    return {"run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/independent-reviews", dependencies=[Depends(reject_cross_site)])
async def core_independent_review(run_id: str, body: ReviewOutcome, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await independent_review(None, run_id, body, db, principal)


@router.post("/api/github/review-runs/{run_id}/repair-checkout", dependencies=[Depends(reject_cross_site)])
async def core_repair_checkout(run_id: str, body: RepairCheckout, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await repair_checkout(None, run_id, body, db, principal)


@router.post("/api/github/review-runs/{run_id}/repairs", dependencies=[Depends(reject_cross_site)])
async def core_publish_repair(run_id: str, body: RepairPublish, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await publish_repair(None, run_id, body, db, principal)


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
    db.refresh(row)
    original = next((t for t in row.targets_json if reviews.key(t) == reviews.key(body.model_dump())), None)
    if (original is not None and row.mode == "review_fix_merge"
        and reviews.effective_target(row, original)["head_sha"] == body.head_sha):
      cwd = Path(get_settings().data_dir) / "platform"
      await _record_base_advance(db, row, original,
        (row.outcomes_json or {}).get(reviews.key(original), {}), principal, cwd)
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
      parent_run = db.get(models.ChatRun, principal.run_id)
      intent = DelegationIntent(app_id=app_id, parent_chat_id=row.chat_id,
        parent_root_run_id=root_id, goal_id=parent_run.goal_id,
        task_key=task_key, prompt=prompt,
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


async def _record_base_advance(db, row, original, previous, principal, cwd) -> bool:
  """Observe and persist a target-branch advance; False when none applies."""
  advanced = await asyncio.to_thread(reviews.observe_base_advance, _gh, cwd, row, original, previous)  # unsettled: read-only (GitHub API reads)
  if advanced is None:
    return False
  _save_base_advance(db, row, original, previous, advanced, principal)
  return True


def _save_base_advance(db, row, original, previous, advanced, principal):
  """Persist an observed advance only for the still-live owning run.

  Callers hold this run's review-outcome lock. The live run, app nonce and
  frozen model are rechecked after GitHub I/O, so Stop or revocation wins and
  is never converted into an ordinary blocked item.
  """
  _parent(db, row, principal)
  db.refresh(row)
  if (row.outcomes_json or {}).get(reviews.key(original), {}) != previous:
    raise HTTPException(409, "This review changed. Refresh before continuing.")
  reviews.save_outcome(db, row, reviews.key(original), advanced)
