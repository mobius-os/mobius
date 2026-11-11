"""Owner-selected PR review runs; exact public merge is a separate DB grant."""
import asyncio
import hashlib
import json
import re
import subprocess
import logging
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Text, cast, or_
from sqlalchemy.orm import Session, load_only

from app import chat_queue, chat_writer, contribution_review_runs as reviews, models, owner_grants, providers, transcript_rows, settled
from app import contribution_review_presets as presets
from app import contribution_review_public as public
from app import contribution_review_repair_actions as repair_actions
from app.contribution_review_public_actions import post_public_review
from app.contribution_review_public import EVIDENCE_MAX_CHARS
from app.contribution_errors import ContributionSubmitError
from app import contribution_direct_merge as direct_merge
from app import contribution_pr_facts as pr_facts
from app import contribution_pull_status as pull_status
from app import contribution_review_merge_actions as merge_actions
from app import contribution_review_verdicts as verdicts
from app.chat_start import start_programmatic_chat_turn
from app.chat_visibility import coerce_agent_settings
from app.config import get_settings
from app.contribution_autopilot import resolve_round_choice
from app.contribution_review_effects import DispatchResult
from app.database import get_db
from app.deps import (Principal, get_agent_run_principal, get_principal, is_owner_input_principal,
                      reject_cross_site, require_account_owner, require_account_owner_control,
                      require_nondelegated_owner_or_app_control)
from app.github_contributions import _validate_submit_app
from app.github_contribution_git import _gh
from app.github_connection import _github_connection_transaction

router = APIRouter(tags=["contribution-reviews"])
_log = logging.getLogger(__name__)
APP_PREFIX = "/api/github/contributions"
SCOPE = {"correctness", "maintainability", "simplicity", "tests", "security_privacy", "technical_debt"}
# request_review_now: Contribute's Request review button. Retire the key once the Store's minimum Möbius includes
# the release that added it, because the app then no longer needs to ask whether this platform has the route.
# owner_confirm: the shell can confirm an action for an app (window.mobius.ownerGrant). The older owner_grant
# key is deliberately never advertised: released Contribute reads it as proof of the owner's tap and turns Post on.
REVIEW_CAPABILITIES = {"post_review": True, "post_review_merge": True, "propose_verdict": True,
                       "review_fix_merge": True, "draft_takeover": True, "merge_now": True,
                       "push_approval": True, "comment_verdict": True, "agent_submits_verdict": True,
                       "mark_ready_now": True, "back_to_draft_now": True, "close_now": True,
                       "request_review_now": True, "request_rereview_now": True, "ask_approval_now": True,
                       "pr_machine": True, "owner_confirm": True}
PUBLISHING_MODES = {"review", "review_merge"}
TAKEOVER_SCOPE = "named_pr_repairs_and_reviewed_successors"
REVIEW_STOP_KEY = "_review_stop"
READY_FOR_YOUR_MERGE = ("Ready for your Merge: this version is all clear. Nothing was merged or marked "
                        "ready for review by the agent; if it is still a draft, open it for review first.")
READY_HEAD_MOVED = "The head changed; GitHub's ready event cannot identify the reviewed head. No action was repeated."
READY_WINDOW_OPEN = ("A request from that attempt might still reach GitHub, so its receipt keeps "
  "fencing new public actions until an hour after it began. Observe again then.")


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
  propose_verdict: bool | None = None
  # The owner's own choice, made only when starting a Review: its verdict goes out without another tap.
  agent_submits_verdict: bool | None = None


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
  # Why the owner takes over someone else's PR; frozen into the grant and shown in its brief.
  takeover_reason: str | None = Field(default=None, min_length=1, max_length=500)
  items: list[SelectedPR] = Field(min_length=1, max_length=20)
  chat_approval: ChatApproval | None = None

  @field_validator("takeover_reason")
  @classmethod
  def meaningful_reason(cls, value):
    if value is not None and not value.strip():
      raise ValueError("Say why you are taking over this PR.")
    return value.strip() if value is not None else None


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


def _reject_takeover_options(mode, options):
  """Only a takeover uses fix_prompt and max_rounds.

  review/review_merge still accept main's no-op values (the Contribute app
  always sends max_rounds: null); _options_input never freezes them there.
  """
  if mode != "review_fix_merge" and (options.fix_prompt not in (None, presets.FIX_PROMPT)
                                     or options.max_rounds is not None):
    raise HTTPException(422, "fix_prompt and max_rounds are not supported outside review_fix_merge mode")


def _options_input(body):
  if body.options is None:
    return None
  options = body.options.model_dump(exclude_none=True, exclude_unset=True)
  mode = getattr(body, "mode", "review")
  _reject_takeover_options(mode, body.options)
  if options.get("autopilot") is True:
    raise HTTPException(422, "autopilot is not supported by this review-only workflow")
  selected = {key: options[key] for key in presets.PROMPT_KEYS if key in options}
  if mode == "review_fix_merge":
    if "fix_prompt" in options:
      selected["fix_prompt"] = options["fix_prompt"]
    if "max_rounds" in body.options.model_fields_set:
      selected["max_rounds"] = body.options.max_rounds
  for flag in ("post_review", "propose_verdict", "agent_submits_verdict"):
    if options.get(flag) is True:
      selected[flag] = True
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


# The PR machine action each admission is, by mode and the owner's role on the PR.
# Review and merge on the owner's own PR has no action: the author never reviews in public.
_ADMISSION_ACTIONS = {("review", "author"): "check_my_pr", ("review", "reviewer"): "review",
                      ("review_merge", "reviewer"): "review",
                      ("review_fix_merge", "author"): "finish_it", ("review_fix_merge", "reviewer"): "take_over"}
# The one comment admitting a takeover of someone else's PR posts from the owner's
# account, with their own reason; the app's confirm sheet shows these exact words.
TAKEOVER_NOTICE_KEY = "_takeover_notice"
TAKEOVER_ACTOR_CHANGED = ("The connected GitHub account changed after you approved this, so nothing was posted. "
                          "Reconnect the account you approved and start it again.")
TAKEOVER_NOTICE_UNCONFIRMED = {"code": "takeover_comment_unconfirmed", "message": (
  "GitHub may have posted the takeover comment. Check the pull request: it is never sent twice, "
  "and nothing was started.")}


def takeover_comment(reason: str) -> str:
  return f"Taking this PR over: {reason}"


OWN_PR_REVIEW_MERGE = ("Review and merge is for someone else's PR. Check your own PR instead, and merge "
                       "it yourself once someone else approves it.")
TAKEOVER_REASON_NEEDED = "Say why you are taking over someone else's PR. The reason goes with the takeover."


def _require_new_grant_shape(body):
  """Rules a new run must meet; repeating an admitted request still returns its saved run."""
  options = body.options or ReviewOptions()
  if body.mode == "review_merge" and options.propose_verdict is not True:
    raise HTTPException(422, "Review and merge needs verdict proposals (propose_verdict): it merges only "
                             "after you submit the approval yourself.")
  if body.takeover_reason is not None and body.mode != "review_fix_merge":
    raise HTTPException(422, "A takeover reason belongs to a takeover (review_fix_merge) only.")


async def _admission_facts(cwd, target, actor: dict) -> dict:
  """The PR machine's facts for one selected PR, read now (its job is checked at admission)."""
  repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
  return await asyncio.to_thread(pr_facts.read_facts, _gh, cwd, target, repo, pull,  # unsettled: read-only (GitHub reviews and checks)
                                 actor_id=actor["id"], actor_login=actor.get("login") or "", job=None)


def _admission_action(body, facts) -> str:
  """The machine action this run takes on one PR, given the owner's role there."""
  if body.mode == "review_merge" and facts["role"] == "author":
    raise HTTPException(409, OWN_PR_REVIEW_MERGE)
  action = _ADMISSION_ACTIONS[(body.mode, facts["role"])]
  if action == "take_over" and not body.takeover_reason:
    raise HTTPException(422, TAKEOVER_REASON_NEEDED)
  return action


def _require_no_other_agent(db, owner_id, app_id, admitted):
  """One agent job per PR: the caller holds the owner's admission lock through the run's insert."""
  for target, facts, action in admitted:
    job = _pr_job(db, owner_id, target["repo"], target["number"], app_id=app_id)
    pr_facts.require_allowed({**facts, "job": job}, action)


def _takeover_notices(body, admitted) -> dict:
  """The comment each taken-over PR receives, armed with the run so a lost answer is never resent."""
  return {reviews.key(target): {"state": "posting", "body": takeover_comment(body.takeover_reason),
                                "armed_at": reviews.utc_now_iso()}
          for target, _facts, action in admitted if action == "take_over"}


async def _admit_run(db, body, principal, app_id, app_nonce, snapshot, actor, targets,
                     admitted) -> models.ContributionReviewRun:
  """Insert the run under the owner's admission lock, after its last one-job-per-PR check."""
  async with chat_queue.get_transition_lock(f"review-pr-admission:{principal.owner.id}"):
    db.rollback()
    _require_no_other_agent(db, principal.owner.id, app_id, admitted)
    # Consent in chat stays in that chat. An app selection without a source
    # conversation still receives its own durable review conversation.
    approval = _chat_approval(db, body, principal)
    _assert_app_current(db, app_id, app_nonce)
    if approval:
      chat_id = principal.chat_id
      targets = [{**target, "approval": approval} for target in targets]
    else:
      chat_id = _new_review_chat(db, snapshot["choice"], app_id)
    notices = _takeover_notices(body, admitted)
    row = models.ContributionReviewRun(id=str(uuid.uuid4()), app_id=app_id,
      owner_id=principal.owner.id, request_id=body.request_id, mode=body.mode,
      targets_json=targets, outcomes_json={TAKEOVER_NOTICE_KEY: notices} if notices else {}, chat_id=chat_id,
      github_actor_id=str(actor["id"]),
      app_nonce=app_nonce, options_json={**snapshot, "input_options": _options_input(body) or {},
        "confirmation_scope": body.confirmation_scope, "takeover_reason": body.takeover_reason,
        "preview_sha256": _fingerprint(snapshot),
        "input_agent": body.agent.model_dump() if body.agent else None})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _new_review_chat(db, choice, app_id) -> str:
  chat = chat_writer.create_chat(id=str(uuid.uuid4()), title="Review selected contributions",
                                 provider=choice["provider"], created_by_app_id=app_id,
                                 agent_settings_json={key: value for key, value in choice.items()
                                                      if key != "provider"})
  db.add(chat)
  return chat.id


def _save_notice(db, row, item_key, notice) -> None:
  reviews.save_effect_outcome(db, row, TAKEOVER_NOTICE_KEY, lambda current: {**current, item_key: notice})


def _require_takeover_announced(row) -> None:
  """A takeover starts only once GitHub confirmed its comment; a lost answer is never resent."""
  for notice in ((row.outcomes_json or {}).get(TAKEOVER_NOTICE_KEY) or {}).values():
    if notice.get("state") == "not_dispatched":
      raise HTTPException(409, f"The takeover comment was not posted, so nothing was started. {notice.get('summary', '')}".strip())
    if notice.get("state") != "posted":
      raise HTTPException(502, TAKEOVER_NOTICE_UNCONFIRMED)


def _takeover_refusal(db, row, actor) -> str | None:
  """Why the comment must not go out now, or None: the connected account is not the one that approved
  the run, the owner stopped it, or Contribute's permission was withdrawn since admission."""
  if str(actor.get("id") or "") != row.github_actor_id:
    return TAKEOVER_ACTOR_CHANGED
  try:
    _owner_decision_authority(db, row)
  except HTTPException as exc:
    return str(exc.detail)
  return None


async def _announce_one(db, row, cwd, item_key, notice, target, caller) -> str:
  """One takeover comment, from its last proof to its saved receipt; returns the receipt's state.

  The proof and the dispatch happen under the connection lock and the run's admission lock, which Stop
  also takes to save its flag, so a Stop is either seen here or comes after the comment.
  """
  async with _github_connection_transaction():
    # The connection lock keeps the account still but does not say whose it is.
    actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub observation; no mutation)
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      refusal = _takeover_refusal(db, row, actor)
      if refusal is None and caller.cancelling():
        refusal = "The request was cancelled before the comment began."
      if refusal is not None:
        result = DispatchResult.not_dispatched(refusal)
      else:
        result = await settled.run_settled(public.post_issue_comment, _gh, cwd, target, notice["body"])
  fields = {"state": {"confirmed": "posted", "unknown": "unknown", "not_dispatched": "not_dispatched"}[result.state],
            **(result.receipt or {}), **({"summary": result.reason} if result.reason else {})}
  _save_notice(db, row, item_key, {**notice, **fields})
  if result.state != "confirmed":
    reviews.save_outcome(db, row, REVIEW_STOP_KEY, {"state": "requested", "actor": "platform",
                                                     "reason": "The takeover comment was not confirmed."})
  return fields["state"]


async def _announce_takeover(db, row, cwd) -> None:
  """Post each armed takeover comment once. Unless GitHub confirms it, the run is stopped
  (it holds nothing) and the start is refused."""
  caller = asyncio.current_task()
  for item_key, notice in list(((row.outcomes_json or {}).get(TAKEOVER_NOTICE_KEY) or {}).items()):
    target = next(t for t in row.targets_json if reviews.key(t) == item_key)
    # Dispatch and its receipt are one unit: a cancelled request still records what GitHub answered
    # instead of stranding the notice as "posting", which would refuse the same start for good.
    state = await settled.settle(_announce_one(db, row, cwd, item_key, notice, target, caller),
                                 what="Takeover comment and its receipt")
    if state != "posted":
      _require_takeover_announced(row)


@router.post(APP_PREFIX + "/{app_id}/review-runs", dependencies=[Depends(reject_cross_site),
             Depends(require_account_owner_control)])
async def start_reviews(app_id: int | None, body: StartReviews,
                        db: Session = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
  approval = _chat_approval(db, body, principal)
  app_nonce = _authorize_context(db, app_id, principal)
  _assert_app_current(db, app_id, app_nonce)
  if body.mode == "review_fix_merge" and body.confirmation_scope != TAKEOVER_SCOPE:
    raise HTTPException(422, "Confirm scoped repairs to the named PR and freshly reviewed successors before takeover.")
  if body.options is not None and body.options.post_review and body.mode not in PUBLISHING_MODES:
    raise HTTPException(422, "Posting a review on GitHub is available for Review only and Review and merge.")
  if body.options is not None and body.options.propose_verdict and not body.options.post_review:
    raise HTTPException(422, "Proposing an approve or request-changes verdict needs posting allowed.")
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
            or row.options_json.get("takeover_reason") != body.takeover_reason
            or (body.preview_sha256 and row.options_json.get("preview_sha256") != body.preview_sha256)))):
        raise HTTPException(409, "This request already approves a different selection.")
      if approval and row.chat_id != principal.chat_id:
        raise HTTPException(409, {"message": "This selection already belongs to another review conversation. Follow it instead of starting again.",
                                  "chat_id": row.chat_id, "run_id": row.id})
    else:
      if body.options is not None:
        _reject_takeover_options(body.mode, body.options)
      _require_new_grant_shape(body)
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
      admitted = []
      for target in targets:
        facts = await _admission_facts(cwd, target, actor)
        action = _admission_action(body, facts)
        pr_facts.require_allowed(facts, action)
        target["role"] = facts["role"]
        admitted.append((target, facts, action))
      if body.mode == "review_fix_merge":
        for target in targets:
          target["allowed_files"] = await asyncio.to_thread(reviews.repair_file_scope, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
          await asyncio.to_thread(reviews.assert_current_base, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
      row = await _admit_run(db, body, principal, app_id, app_nonce, snapshot, actor, targets, admitted)
      await _announce_takeover(db, row, cwd)

    # Any durable first turn owns recovery; clicking twice never starts a second
    # review or revives a stopped run. An admission failure retries the empty chat.
    if not db.query(models.ChatRun).filter_by(chat_id=row.chat_id).first():
      _require_takeover_announced(row)
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
  return {"runs": [_list_run_view(db, row, app_id) for row in rows],
          "respond_chats": _respond_chats(db, app_id) if app_id is not None else []}


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
    posting = body.publish and _posts_review(row) and not _proposes_verdict(row)
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


def _posts_review(row) -> bool:
  return row.mode in PUBLISHING_MODES and (row.options_json or {}).get("post_review") is True


def _proposes_verdict(row) -> bool:
  return _posts_review(row) and (row.options_json or {}).get("propose_verdict") is True


def _agent_submits_verdict(row) -> bool:
  """The owner chose up front that this Review sends its verdict without another tap; a merge never does."""
  return (row.mode == "review" and _proposes_verdict(row)
          and (row.options_json or {}).get("agent_submits_verdict") is True)


async def _submit_agent_verdict(db, row, item_key, target, principal, cwd):
  """Send the run's own proposal now, under its frozen grant; a refusal leaves it for the owner."""
  proposal = row.outcomes_json[item_key]["verdict_proposal"]
  try:
    receipt = await verdicts.submit(db, row, item_key, target, proposal["id"], gh=_gh, cwd=cwd,
                                    authorize=lambda: _parent(db, row, principal))
  except HTTPException as exc:
    db.refresh(row)
    return {"run": _run_view(db, row), "blocked": f"{exc.detail} It waits for the owner in Contribute."}
  db.refresh(row)
  return {"run": _run_view(db, row), "verdict": receipt}


def _merge_continues(row, item) -> bool:
  """Frozen evidence may still reach the merge once its public step is posted.

  Only a merge grant whose comment or owner-submitted APPROVE is confirmed,
  and whose merge never started, continues; every other retry is read-only.
  """
  return (row.mode == "review_merge" and (item.get("frozen_evidence") or item).get("state") == "all_clear"
          and not _merge_attempted(item) and (item.get("public_review") or item.get("verdict_proposal"))
          and reviews.public_merge_blocker(row, item) is None)


def _merge_attempted(outcome) -> bool:
  return reviews.merge_pending(outcome)


async def _observe_merge(previous, target, pull, cwd):
  """The merge fact GitHub shows now, as fields for settle_merge_observation."""
  return await asyncio.to_thread(reviews.reconcile_merge, _gh, cwd, target, previous, pull)  # unsettled: read-only (GitHub merge and queue proof)


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
    if public.require_same_evidence(previous, body.model_dump()) and not _merge_continues(row, previous):
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
    if body.publish and not _posts_review(row):
      raise HTTPException(403, "This grant does not authorize a public review.")
    if row.mode == "review_merge" and _posts_review(row) and body.state == "all_clear" and not body.publish:
      raise HTTPException(422, "This grant posts its review before merging. Send the all-clear outcome with publish:true.")
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
      reviews.settle_merge_observation(db, row, item_key, previous,
                                       await _observe_merge(previous, target, pull, cwd))
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
      # A takeover never merges: its all-clear waits for the owner's own Merge tap. It is
      # not offered past a push or merge whose outcome is still unconfirmed on this PR.
      reviews.require_public_transition_clear(db, row, target)
      reviews.save_outcome(db, row, item_key, {**outcome, "summary": f"{body.summary}\n\n{READY_FOR_YOUR_MERGE}"},
                           expected=previous)
      return {"run": _run_view(db, row)}
    if body.publish and verdicts.own_pull(row, pull):
      # The owner never reviews their own PR in public: GitHub refuses a verdict, and a plain
      # comment from themselves is not a review. The result stays private; they fix or set it aside.
      _parent(db, row, principal)
      reviews.save_outcome(db, row, item_key, outcome, expected=previous)
      return {"run": _run_view(db, row)}
    if body.publish and _proposes_verdict(row):
      if not previous.get("verdict_proposal"):
        # Proposing posts nothing and freezes the evidence the owner will read.
        async with chat_queue.get_transition_lock(f"review-admission:{run_id}"):
          _parent(db, row, principal)
          reviews.save_outcome(db, row, item_key, {**outcome, "frozen_evidence": public.evidence(outcome),
            "verdict_proposal": verdicts.propose(outcome, target)}, expected=previous)
        if _agent_submits_verdict(row):
          return await _submit_agent_verdict(db, row, item_key, target, principal, cwd)
        return {"run": _run_view(db, row)}
    elif (body.publish and _posts_review(row)
        and (previous.get("public_review") or {}).get("state") not in {"posting", "posted", "unknown"}):
      outcome = await post_public_review(db, row, item_key, target, outcome, principal, cwd,
        gh=_gh, authorize=lambda: _parent(db, row, principal))
      if row.mode == "review_merge" and body.state == "all_clear":
        reviews.save_effect_outcome(db, row, item_key, outcome)
        db.refresh(row)
        outcome = {**outcome, **{k: v for k, v in row.outcomes_json.get(item_key, {}).items()
                                 if k in ("public_review", "verdict_proposal")}}
    # A private review remains true of its pinned base after that base moves.
    # Only an actual merge candidate requires current-base proof.
    if body.state == "all_clear" and row.mode == "review_merge":
      blocker = reviews.public_merge_blocker(row, outcome)
      if blocker:
        if not outcome.get("public_review") and not outcome.get("verdict_proposal"):
          raise HTTPException(409, blocker)
        return {"run": _run_view(db, row)}
      _require_ready_attempt_resolved(previous, target)
      await merge_actions.merge_reviewed_target(db, row, target, outcome, repo, pull, principal,
        gh=_gh, cwd=cwd, authorize=lambda: _parent(db, row, principal),
        pr_job=partial(_pr_job_held, db, row.owner_id, target["repo"], target["number"],
                       exclude_run_id=row.id, app_id=row.app_id))
      return {"run": _run_view(db, row)}
    if outcome.get("merge_attempted") or outcome.get("public_review"):
      reviews.save_effect_outcome(db, row, item_key, outcome)
    else:
      _parent(db, row, principal)
      reviews.save_outcome(db, row, item_key, outcome, expected=previous)
    return {"run": _run_view(db, row)}


class ReviewPreview(BaseModel):
  mode: Literal["review", "review_merge", "review_fix_merge"] = "review"
  agent: ReviewAgentChoice | None = None
  options: ReviewOptions | None = None


def _presets_view():
  # One editor serves every advertised mode. The plain modes accept the
  # takeover defaults unchanged, so the editor gets them all.
  return {"presets": presets.presets(mode="review_fix_merge"), "capabilities": REVIEW_CAPABILITIES}


@router.get(APP_PREFIX + "/{app_id}/review-presets")
def review_presets(app_id: int, db: Session = Depends(get_db),
                   principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  return _presets_view()


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
  if (row.outcomes_json or {}).get(TAKEOVER_NOTICE_KEY):
    value["takeover_notice"] = row.outcomes_json[TAKEOVER_NOTICE_KEY]
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
  elif value["state"] == "reviewing" and last and last.status == "completed" and not _has_next_move(db, chat):
    value.update(state="needs_you", execution_state="interrupted", summary=(
      "Nothing is working on this review any more: its reviewer stopped before it reported, "
      "for example when Möbius restarted. Open the review conversation to continue it, or start the review again."))
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


# Item states that leave a PR to the owner or to GitHub, not to the run's agent
# (the same split as Contribute's reviewItemProgress). An attempt still on its
# way to GitHub is not an agent's job either: its receipt fences what follows.
_SETTLED_ITEMS = {"all_clear", "merged", "queued"}
_OWNER_ITEMS = {"needs_you", "merge_unknown", "ready_unknown", "ready_unattributable",
                "verdict_pending", "verdict_unknown"}
_SENDING_ITEMS = {"merging", "pushing", "marking_ready", "verdict_posting"}
_HALTED_RUNS = {"stopped", "failed", "interrupted"}


def _job_on_pull(view, repo, number) -> dict | None:
  """The PR machine's job fact one run holds on a PR: running, needs_answer, stalled, or none."""
  item = next((i for i in view["items"]
               if i["repo"].lower() == repo.lower() and i["number"] == number), None)
  if item is None or item.get("state") in _SETTLED_ITEMS:
    return None
  execution = view.get("execution_state")
  if execution == "awaiting_owner":
    return {"state": "needs_answer", "run_id": view["id"]}
  if execution in _HALTED_RUNS:
    return {"state": "stalled", "run_id": view["id"]}
  if item.get("state") in _OWNER_ITEMS | _SENDING_ITEMS:
    return None
  return {"state": "running", "run_id": view["id"]}


# How long an admitted run may wait for its first turn before it stops holding its PRs.
# The run row is committed before that turn starts, so a younger run without one still
# holds them: a second admission must never slip in between.
FIRST_TURN_GRACE_SECONDS = 120


def _never_started(db, row) -> bool:
  """A run whose conversation never got a turn, well after admission: its start failed."""
  from app.timeutil import now_naive_utc
  if db.query(models.ChatRun.id).filter_by(chat_id=row.chat_id).first():
    return False
  return (now_naive_utc() - row.created_at).total_seconds() > FIRST_TURN_GRACE_SECONDS


def _run_job(db, row, repo, number) -> dict | None:
  job = _job_on_pull(_run_view(db, row), repo, number)
  if job and job["state"] == "running" and _never_started(db, row):
    return {**job, "state": "stalled"}
  return job


def _pr_job(db, owner_id, repo, number, *, exclude_run_id=None, app_id=None) -> dict | None:
  """The agent job another review run or a Respond chat holds on this PR, a live one before a stalled one.

  One agent job per PR: the machine refuses new jobs, merges and status
  changes while a live one holds the PR. A stalled job holds nothing.
  """
  jobs = [_run_job(db, row, repo, number)
          for row in reviews.runs_naming_pull(db, owner_id, repo, number) if row.id != exclude_run_id]
  jobs = [job for job in jobs if job]
  live = next((job for job in jobs if job["state"] != "stalled"), None)
  return live or _respond_job(db, app_id, repo, number) or (jobs[0] if jobs else None)


@asynccontextmanager
async def _pr_job_held(db, owner_id, repo, number, *, exclude_run_id=None, app_id=None):
  """Hold the owner's PR-job admission and hand over a lookup of the PR's job as it is now.

  A status change or merge asks the PR machine after waiting for the GitHub connection, so a job fact
  read when the request began proves nothing then. Under admission's own lock no review run can enter
  between the caller's last look and its write. A Respond chat starts through the ordinary app-chat
  start, which this lock does not guard: every look reads the chats afresh, and only a chat started
  after the last look escapes it.
  """
  async with chat_queue.get_transition_lock(f"review-pr-admission:{owner_id}"):
    db.rollback()  # as admission does: read the job as it is now, not as this request's snapshot saw it
    yield lambda: _pr_job(db, owner_id, repo, number, exclude_run_id=exclude_run_id, app_id=app_id)


# Contribute starts "Respond to feedback" as an ordinary app chat under this scope. No run row
# tracks it, so the chat itself is the job: it holds its PR for as long as it can still move.
_RESPOND_SCOPE = re.compile(r"contribute-address:(.+)#([0-9]+)")


def _respond_pull(chat) -> tuple[str, int] | None:
  """The (lowercased repo, number) a Respond chat answers feedback on, from its app chat scope."""
  scope = coerce_agent_settings(chat.agent_settings_json).get("chat_scope")
  found = _RESPOND_SCOPE.fullmatch(scope.strip()) if isinstance(scope, str) else None
  return (found[1].lower(), int(found[2])) if found else None


def _respond_chats(db, app_id, *, pull=None) -> list[dict]:
  """The app's live Respond chats as {repo, number, chat_id, state}; `pull` narrows them to one PR.

  Live means the chat still has a next move; `needs_answer` means that move is the
  owner's (a card or secure input waiting), else `running`. Only the scope column is
  read until a chat matches, because a handoff costs several queries.
  """
  chats = db.query(models.Chat).options(load_only(
    models.Chat.id, models.Chat.agent_settings_json, models.Chat.pending_question_id)).filter(
    models.Chat.deleted_at.is_(None), models.Chat.created_by_app_id == app_id,
    cast(models.Chat.agent_settings_json, Text).like("%contribute-address:%")).all()
  found = []
  for chat in chats:
    target = _respond_pull(chat)
    if target is None or pull not in (None, target):
      continue
    kind = _chat_handoff(db, chat)["kind"]
    if kind != "none":
      found.append({"repo": target[0], "number": target[1], "chat_id": chat.id,
                    "state": "needs_answer" if kind == "owner_input" else "running"})
  return found


def _respond_job(db, app_id, repo, number) -> dict | None:
  """The job a live Respond chat holds on this PR. Only an app has Respond chats; core routes have none."""
  if app_id is None:
    return None
  chat = next(iter(_respond_chats(db, app_id, pull=(repo.lower(), number))), None)
  return {"kind": "respond", "state": chat["state"], "chat_id": chat["chat_id"]} if chat else None


def _has_next_move(db, chat) -> bool:
  """Whether anything will still move the owning chat: a turn, an armed Wait, a
  self-resuming helper, a parked continuation or an owner card.

  A review ends its own turn once its independent reviewer starts and sleeps
  until that helper reports, so the chat's last run reads "completed" for the
  whole review. A helper killed with the process (an unplanned restart stays
  manual by design) leaves the chat with no next move and the run "reviewing"
  for good. The same handoff projection the chat list shows says so honestly.
  """
  return _chat_handoff(db, chat)["kind"] != "none"


def _chat_handoff(db, chat) -> dict:
  """The chat list's handoff projection for one chat: kind "none" when nothing will move it again."""
  from app import secure_inputs
  from app.chat import continuation_handoff_for_chat, is_chat_running
  from app.chat_handoffs import project_handoff
  from app.chat_waits import outstanding_waits_for_chat, serialize_wait
  from app.delegations import serialize_background_helpers
  from app.goal_plans import presented_goal
  if chat is None:
    return {"kind": "none", "reason": None}
  return project_handoff(
    owner_input=bool(chat.pending_question_id) or chat.id in secure_inputs.pending_chat_ids(),
    running=is_chat_running(chat.id),
    waits=[serialize_wait(wait, db=db) for wait in outstanding_waits_for_chat(db, chat.id)],
    helper_count=serialize_background_helpers(db, chat.id)["count"],
    park=continuation_handoff_for_chat(db, chat.id),
    goal=presented_goal(db, chat.id),
  )


def _parent(db, row, principal):
  if principal.delegation_id is not None or principal.chat_id != row.chat_id:
    raise HTTPException(403, "Only this review's owning conversation may report an outcome or merge.")
  # Its posts, repairs, merges and readiness act as the owner on GitHub, so a
  # turn a guest started in that shared conversation cannot drive them.
  require_account_owner(principal)
  _assert_execution_live(db, row, principal)
  frozen = row.options_json or {}
  chat = db.query(models.Chat).populate_existing().filter_by(id=row.chat_id).first()
  if not _frozen_model_matches(frozen, chat):
    raise HTTPException(409, "The owning conversation's model choice changed. This frozen workflow cannot continue under a different choice.")


@router.post("/api/github/review-runs", dependencies=[Depends(reject_cross_site), Depends(require_account_owner_control)])
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
  return _presets_view()


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


class VerdictDecision(PullIdentity):
  proposal_id: str = Field(pattern=r"^[0-9a-f]{32}$")
  decision: Literal["submit", "discard", "dismiss", "continue_merge"]
  # Submit the proposal's findings as a plain comment instead of its approve or request-changes.
  event: Literal["COMMENT"] | None = None

  @model_validator(mode="after")
  def comment_only_when_submitting(self):
    if self.event is not None and self.decision != "submit":
      raise ValueError("Only a submitted verdict can be sent as a comment.")
    return self


VERDICT_CHANGED = "This verdict no longer matches the reviewed version. Refresh before deciding."


def _require_owner_hands(principal: Principal, refusal: str) -> None:
  """A public action only the owner's own tap may take: an agent bearer, and the app token one minted, are refused."""
  if (principal.chat_id is not None or principal.run_id is not None or principal.delegation_id is not None
      or principal.via_agent_chat is not None):
    raise HTTPException(403, refusal)


def _owner_decision_authority(db, row):
  """Re-prove the run is live for an owner decision; the agent is not involved."""
  db.refresh(row)
  if _review_stop_requested(row):
    raise HTTPException(409, "This review was stopped. Nothing was sent to GitHub.")
  _assert_app_current(db, row.app_id, row.app_nonce)


async def _resume_run(db, row, content: str) -> bool:
  """Wake the run's own conversation, under its frozen model, with the owner's decision.

  A run stopped meanwhile stays stopped: a decision never revives it.
  """
  db.refresh(row)
  chat = db.get(models.Chat, row.chat_id)
  if _review_stop_requested(row) or not chat or not _frozen_model_matches(row.options_json or {}, chat):
    return False
  return await start_programmatic_chat_turn(chat_id=chat.id, title=chat.title, provider=chat.provider,
    initiated_by_app_id=row.app_id, content=content)


def _merge_resume_note(item) -> str:
  return (f"The owner submitted the APPROVE verdict for {item['repo']}#{item['number']} at {item['head_sha'][:12]}. "
          "Re-send that item's identical all_clear outcome once so the guarded merge can run. "
          "Do not change its evidence or post anything else.")


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/verdict",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def decide_verdict(app_id: int | None, run_id: str, body: VerdictDecision,
                         db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  """The owner's own submit or discard of one exact proposed verdict, dismissal of an unconfirmed one,
  or continuing the merge of an approval GitHub already holds."""
  _require_owner_hands(principal, "Only you can submit, discard or dismiss a verdict. An agent cannot decide its own proposal.")
  owner_grants.consume_owner_grant(db, principal, app_id, "verdict", body, run_id=run_id)
  row = _row(db, app_id, run_id, principal)
  item_key = reviews.key(body.model_dump())
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    if body.decision == "dismiss":
      db.refresh(row)  # a dismissal sends nothing, so even a stopped review may be dismissed
    else:
      _owner_decision_authority(db, row)
    original = next((t for t in row.targets_json if reviews.key(t) == item_key), None)
    target = reviews.effective_target(row, original) if original else None
    item = (row.outcomes_json or {}).get(item_key) or {}
    proposal = item.get("verdict_proposal") or {}
    if (target is None or target["head_sha"] != body.head_sha or proposal.get("id") != body.proposal_id
        or proposal.get("head_sha") != body.head_sha):
      raise HTTPException(409, VERDICT_CHANGED)
    if body.decision in {"discard", "dismiss"}:
      (verdicts.discard if body.decision == "discard" else verdicts.dismiss)(db, row, item_key, body.proposal_id)
      return {"run": _run_view(db, row)}
    if body.decision == "continue_merge":
      # The owner's own tap re-arranges the guarded merge of an approval GitHub already holds.
      if not reviews.approved_merge_waiting(row, item):
        raise HTTPException(409, "No confirmed approval is waiting for its merge. Refresh to see the current state.")
      resumed = await _resume_run(db, row, _merge_resume_note({**original, "head_sha": target["head_sha"]}))
      db.refresh(row)
      return {"run": _run_view(db, row), "merge_resumed": resumed}
    cwd = Path(get_settings().data_dir) / "platform"
    receipt = await verdicts.submit(db, row, item_key, target, body.proposal_id, gh=_gh, cwd=cwd,
                                    authorize=lambda: _owner_decision_authority(db, row), event=body.event)
    resumed = None
    if (receipt.get("state") == "posted" and receipt.get("event") == "APPROVE"
        and row.mode == "review_merge" and not _merge_attempted(row.outcomes_json[item_key])):
      resumed = await _resume_run(db, row, _merge_resume_note({**original, "head_sha": target["head_sha"]}))
    db.refresh(row)
    return {"run": _run_view(db, row), "verdict": receipt, "merge_resumed": resumed}


@router.post("/api/github/review-runs/{run_id}/verdict",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_decide_verdict(run_id: str, body: VerdictDecision, db: Session = Depends(get_db),
                              principal: Principal = Depends(get_principal)):
  return await decide_verdict(None, run_id, body, db, principal)


class DirectMerge(PullIdentity):
  # An irreversible tap: a misspelled field must fail loudly, never be ignored.
  model_config = ConfigDict(extra="forbid")
  method: Literal["squash", "merge", "rebase"] | None = None


@router.post(APP_PREFIX + "/{app_id}/pulls/merge",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def merge_pull_now(app_id: int, body: DirectMerge, db: Session = Depends(get_db),
                         principal: Principal = Depends(get_principal)):
  """The owner's own tap: merge exactly the pull-request head named, now, with no agent run.

  200 {state: merged|queued, merge_sha, method, repo, number, head_sha}; 409 with a
  plain reason when GitHub's live answer or the PR machine's merge guard refuses it
  (the reason is the machine's own words); 502 merge_unconfirmed when the write's
  outcome is unknown. See contribution_direct_merge.
  """
  _require_owner_hands(principal, "Only you can merge a pull request from here. An agent cannot press Merge.")
  owner_grants.consume_owner_grant(db, principal, app_id, "merge", body)
  nonce = _authorize_context(db, app_id, principal)
  cwd = Path(get_settings().data_dir) / "platform"
  return await direct_merge.merge_exact_pull(
    _gh, cwd, body, partial(_assert_app_current, db, app_id, nonce), db, principal.owner.id,
    pr_job=partial(_pr_job_held, db, principal.owner.id, body.repo, body.number, app_id=app_id))


class PullStatusChange(PullIdentity):
  # A public tap Contribute cannot take back: a misspelled field must fail loudly, never be ignored.
  model_config = ConfigDict(extra="forbid")


def _github_logins(value: list[str]) -> list[str]:
  if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login) for login in value):
    raise ValueError("Name each reviewer by their GitHub login.")
  return list(dict.fromkeys(value))


class ReviewerRequest(PullStatusChange):
  reviewers: list[str] = Field(min_length=1, max_length=15)

  @field_validator("reviewers")
  @classmethod
  def github_logins(cls, value):
    return _github_logins(value)


class ApprovalRequest(PullStatusChange):
  people: list[str] = Field(min_length=1, max_length=15)
  note: str = Field(min_length=1, max_length=1000)

  @field_validator("people")
  @classmethod
  def github_logins(cls, value):
    return _github_logins(value)

  @field_validator("note", mode="before")
  @classmethod
  def trimmed(cls, value):
    # The note posted is exactly what was validated: whitespace around it is never part of the length.
    return value.strip() if isinstance(value, str) else value


async def _owner_status_change(app_id, name, body, db, principal):
  """One owner tap on a PR's status: never an agent, and refused when the PR machine says no.

  200 {state, observed_head_sha, repo, number, head_sha}; 409 with the plain
  reason (the machine's own words when it refuses); 404 when GitHub cannot be
  read; 502 {code: <ready|draft|close|reviewers|approval>_unconfirmed, message} when the
  write's outcome is unknown. See contribution_pull_status.
  """
  _require_owner_hands(principal, "Only you can change a pull request's status from here. An agent cannot press it.")
  owner_grants.consume_owner_grant(db, principal, app_id, pull_status.CHANGES[name].machine_action, body)
  nonce = _authorize_context(db, app_id, principal)
  cwd = Path(get_settings().data_dir) / "platform"
  return await pull_status.change_pull_status(
    _gh, cwd, name, body, partial(_assert_app_current, db, app_id, nonce),
    pr_job=partial(_pr_job_held, db, principal.owner.id, body.repo, body.number, app_id=app_id))


@router.post(APP_PREFIX + "/{app_id}/pulls/ready",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def mark_pull_ready_now(app_id: int, body: PullStatusChange, db: Session = Depends(get_db),
                              principal: Principal = Depends(get_principal)):
  """The owner's own Open for review of their draft (the PR machine's open_for_review)."""
  return await _owner_status_change(app_id, "ready", body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/pulls/draft",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def back_to_draft_now(app_id: int, body: PullStatusChange, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_principal)):
  """The owner's own Back to draft of their open pull request (back_to_draft)."""
  return await _owner_status_change(app_id, "draft", body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/pulls/close",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def close_pull_now(app_id: int, body: PullStatusChange, db: Session = Depends(get_db),
                         principal: Principal = Depends(get_principal)):
  """The owner's own Close, unmerged (close): their own PR, or one they can write to."""
  return await _owner_status_change(app_id, "close", body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/pulls/request-review",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def request_review_now(app_id: int, body: ReviewerRequest, db: Session = Depends(get_db),
                             principal: Principal = Depends(get_principal)):
  """The owner's own Request review (request_review): GitHub's review request to people they pick, who need not have reviewed."""
  return await _owner_status_change(app_id, "request-review", body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/pulls/request-reviewers",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def request_rereview_now(app_id: int, body: ReviewerRequest, db: Session = Depends(get_db),
                               principal: Principal = Depends(get_principal)):
  """The owner's own Ask for re-review (ask_rereview), only of people whose review is of an older version."""
  return await _owner_status_change(app_id, "request-reviewers", body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/pulls/ask-approval",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def ask_approval_now(app_id: int, body: ApprovalRequest, db: Session = Depends(get_db),
                           principal: Principal = Depends(get_principal)):
  """The owner's own Ask for approval (ask_approval): one short note on the PR, only to people who looked again."""
  return await _owner_status_change(app_id, "ask-approval", body, db, principal)


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
      verdict = previous.get("verdict_proposal") or {}
      if verdict.get("state") in {"posting", "unknown"}:
        read_at = time.time()
        review, complete = await asyncio.to_thread(public.find_review, _gh, cwd, {**original, "head_sha": verdict["head_sha"]},  # unsettled: read-only (verdict receipt observation)
                                                   verdict, row.github_actor_id, verdict["event"])
        verdicts.settle_observed(db, row, reviews.key(original), verdict, review, complete_scan=complete, read_at=read_at)
        previous = row.outcomes_json.get(reviews.key(original), {})
      pending = next((a for a in previous.get("repair_attempts", [])
                      if a.get("state") in {"pushing", "push_unknown"}), None)
      if pending:
        if not await repair_actions.reconcile_push(db, row, original, pending, gh=_gh, cwd=cwd):
          repair_actions.escalate_unconfirmed_push(db, row, original, pending)
          continue
        previous = row.outcomes_json[reviews.key(original)]
      attempt = previous.get("ready_attempt")
      if attempt and attempt.get("state") in {"attempting", "unknown"}:
        if await _reconcile_ready_attempt(db, row, reviews.key(original), attempt, cwd):
          continue
        previous = row.outcomes_json[reviews.key(original)]
      if not _merge_attempted(previous):
        continue
      try:
        target = reviews.effective_target(row, original)
      except HTTPException:
        continue  # An ambiguous receipt chain needs the owner; observe the others.
      read_at = time.time()
      try:
        _repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
      except HTTPException:
        # The selection no longer matches, but the same PR at a later head can
        # prove the head-pinned attempt can no longer land (see later_pull_fact).
        read_at = time.time()
        pull = await asyncio.to_thread(reviews.selected_pull, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
        fact = pull and await asyncio.to_thread(reviews.later_pull_fact, _gh, cwd, target, previous, pull, read_at)  # unsettled: read-only (GitHub merge queue read)
        if fact:
          reviews.settle_merge_observation(db, row, reviews.key(original), previous, fact)
        continue
      # A PR closed unmerged at the attempted head still matches the selection.
      fact = await asyncio.to_thread(reviews.later_pull_fact, _gh, cwd, target, previous, pull, read_at)  # unsettled: read-only (GitHub merge queue read)
      if fact:
        reviews.settle_merge_observation(db, row, reviews.key(original), previous, fact)
        continue
      try:
        fact = await _observe_merge(previous, target, pull, cwd)
      except HTTPException:
        # A failed read proves no new fact; preserve evidence and attempt fence.
        continue
      reviews.settle_merge_observation(db, row, reviews.key(original), previous, fact)
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
  """A durable reviewer belongs to its spawning work, not today's parent turn.

  A Goal-owned reviewer follows its Goal: a chat Stop holds it. The Goal is
  child.goal_id, or the legacy layout's overloaded parent_root_run_id.
  """
  from app.delegations import delegation_goal_id
  root_id = child.parent_root_run_id
  goal_id = delegation_goal_id(db, child)
  if goal_id is not None:
    goal = db.query(models.ChatGoal).populate_existing().filter_by(id=goal_id).first()
    return (goal is not None and goal.chat_id == child.parent_chat_id and goal.status == "open"
      and db.query(models.ChatRun.id).filter_by(
        chat_id=child.parent_chat_id, goal_id=goal_id).first() is not None)
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
        raise HTTPException(409, "This reviewer delegation already reported on this head and base. "
          "Its verdict stands: each head and base admits one independent reviewer, and /reviewers "
          "returns this same one. To continue, publish a new repair (a new head), observe a "
          "target-base advance, or ask the owner for a new grant.")
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
  if row.mode != "review_fix_merge" or (row.options_json or {}).get("confirmation_scope") != TAKEOVER_SCOPE:
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
    _require_ready_attempt_resolved(previous, target)
    attempts = previous.get("repair_attempts", [])
    if any(r.get("state") in {"pushing", "push_unknown"} for r in attempts):
      raise HTTPException(409, "An earlier push has an unclear public outcome. Reconcile it; do not prepare another repair.")
    reviews.require_public_transition_clear(db, row, target, repair=True)
    reviews.require_repair_round_available(row, attempts)
    cwd = Path(get_settings().data_dir) / "platform"
    async with _github_connection_transaction():
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub observation; no mutation)
      if str(actor.get("id") or "") != row.github_actor_id:
        raise HTTPException(409, "The connected GitHub actor changed. Approve a new selection.")
      await asyncio.to_thread(reviews.require_repairable_merge, _gh, cwd, target, previous)  # unsettled: read-only (exact saved queue removal)
      _parent(db, row, principal)
      if (previous.get("checkout") and previous["checkout"].get("initial_head_sha") == target["head_sha"]
          and repairs.checkout_present(previous["checkout"].get("checkout"))):
        checkout = {**previous["checkout"], "target_base_sha": await settled.run_settled(
          repairs.fetch_target_base, _gh, cwd, Path(previous["checkout"]["checkout"]), target)}
        _parent(db, row, principal)
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "checkout": checkout},
                             expected=previous)
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
        reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "repairing",
          "checkout": checkout, "findings": body.findings, "head_sha": target["head_sha"]},
          expected=previous)
      except HTTPException:
        # This newly created path has never owned a public receipt.
        await settled.run_settled(repairs.discard_checkout, checkout.get("checkout"))
        raise
    return {"checkout": checkout, "run": _run_view(db, row)}


AWAITING_PUSH_DECISION = ("This repair is waiting for the owner's push decision in Contribute. "
                          "End your turn; this chat is resumed when they decide.")
PUSH_DECISION_SUMMARY = "A repair is ready for you. Read it, then push it or discard it."


async def _validated_repair(db, row, principal, target, previous, cwd) -> dict:
  """Derive the repair from its server checkout and today's GitHub; this only reads and checks.

  Returns the validation (candidate head, diff hash, files) on the live base it was proved against.
  """
  from app import contribution_review_repairs as repairs
  actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub observation; no mutation)
  if str(actor.get("id") or "") != row.github_actor_id:
    raise HTTPException(409, "The connected GitHub actor changed. No push started.")
  await asyncio.to_thread(reviews.require_repairable_merge, _gh, cwd, target, previous)  # unsettled: read-only (exact saved queue removal)
  principal.authorize()
  current_base = await asyncio.to_thread(reviews.base_head, _gh, cwd, target["repo"], target["base_ref"])  # unsettled: read-only (GitHub observation; no mutation)
  await asyncio.to_thread(reviews.assert_approved_base_ancestry, _gh, cwd, target, current_base)  # unsettled: read-only (GitHub ancestry proof; no mutation)
  try:
    validation = await settled.run_settled(repairs.validate_repair, _gh, cwd, row, target,
                                           previous.get("checkout"), target["allowed_files"], base_sha=current_base)
  except HTTPException as exc:
    reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "needs_you", "summary": str(exc.detail)})
    raise
  if await asyncio.to_thread(reviews.base_head, _gh, cwd, target["repo"], target["base_ref"]) != current_base:  # unsettled: read-only (GitHub observation; no mutation)
    raise HTTPException(409, "The target base moved during repair validation. Test the new combined result first.")
  if previous.get("queue_removal") and validation["head_sha"] == target["head_sha"]:
    raise HTTPException(409, "A removed queue head cannot be retried. Prepare a new repaired successor.")
  return {**validation, "base_sha": current_base}


def _require_repair_evidence(body, previous, target):
  if body.tests_passed is not True:
    raise HTTPException(422, "Failed or unknown tests block public repair publication.")
  if not body.summary.strip() or not body.tests.strip():
    raise HTTPException(422, "Concrete repair and test evidence are required.")
  checkout = previous.get("checkout")
  if not checkout or checkout.get("initial_head_sha") != target["head_sha"]:
    raise HTTPException(409, "Prepare this exact head in its dedicated server checkout first.")


async def _earlier_repair_answer(db, row, original, attempts, head_sha, cwd) -> dict | None:
  """A repair of this head already pushed or waiting for the owner: reconcile or wait, never repeat."""
  pending = next((r for r in attempts if r.get("from_sha") == head_sha
                  and r.get("state") in {"pushing", "push_unknown", "pushed"}), None)
  if pending:
    confirmed = await repair_actions.reconcile_push(db, row, original, pending, gh=_gh, cwd=cwd)
    return {"run": _run_view(db, row), **({} if confirmed else {
      "blocked": "The earlier push is not confirmed. It was not repeated."})}
  if any(r.get("from_sha") == head_sha and r.get("state") == reviews.REPAIR_PROPOSED for r in attempts):
    return {"run": _run_view(db, row), "blocked": AWAITING_PUSH_DECISION}
  return None


class _RunAuthority:
  """Who may continue a repair step: the run's own live agent, or the owner's own tap."""

  def __init__(self, check, run_id, chat_id=None):
    self.authorize, self.run_id, self.chat_id = check, run_id, chat_id


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/repairs",
             dependencies=[Depends(reject_cross_site)])
async def publish_repair(app_id: int | None, run_id: str, body: RepairPublish,
                         db: Session = Depends(get_db),
                         principal: Principal = Depends(get_agent_run_principal)):
  """The agent's repair of one exact head, saved as a proposal for the owner; nothing is pushed.

  The owner pushes or discards it through /repairs/decision. A push for this head
  that already started is only reconciled here, never repeated.
  """
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
    cwd = Path(get_settings().data_dir) / "platform"
    earlier = await _earlier_repair_answer(db, row, original, attempts, body.head_sha, cwd)
    if earlier is not None:
      return earlier
    target = _target(row, body)
    _require_ready_attempt_resolved(previous, target)
    _require_repair_evidence(body, previous, target)
    reviews.require_repair_round_available(row, attempts)
    reviews.require_public_transition_clear(db, row, target, repair=True)
    agent = _RunAuthority(lambda: _parent(db, row, principal), principal.run_id, principal.chat_id)
    async with _github_connection_transaction():
      validation = await _validated_repair(db, row, agent, target, previous, cwd)
    proposal = {**validation, "id": str(uuid.uuid4()), "state": reviews.REPAIR_PROPOSED,
      "from_sha": target["head_sha"], "summary": body.summary, "tests": body.tests, "tests_passed": True,
      "author_chat_id": principal.chat_id, "author_run_id": principal.run_id, "proposed_at": reviews.utc_now_iso()}
    _parent(db, row, principal)
    reviews.save_outcome(db, row, reviews.key(target), {**previous, "state": "needs_you",
      "summary": PUSH_DECISION_SUMMARY, "repair_attempts": [*attempts, proposal]}, expected=previous)
    return {"run": _run_view(db, row), "proposed": proposal}


class RepairDecision(PullIdentity):
  # A push the owner cannot take back: a misspelled field must fail loudly, never be ignored.
  model_config = ConfigDict(extra="forbid")
  attempt_id: str = Field(min_length=1, max_length=64)
  decision: Literal["push", "discard"]


def _proposed_repair(row, body):
  """The exact undecided proposal on the PR's current version, or a 409."""
  original = next((t for t in row.targets_json if reviews.key(t) == reviews.key(body.model_dump())), None)
  target = reviews.effective_target(row, original) if original else None
  item = (row.outcomes_json or {}).get(reviews.key(original), {}) if original else {}
  attempt = next((a for a in item.get("repair_attempts", []) if a.get("id") == body.attempt_id), None)
  if (target is None or attempt is None or attempt.get("state") != reviews.REPAIR_PROPOSED
      or attempt.get("from_sha") != body.head_sha or target["head_sha"] != body.head_sha):
    raise HTTPException(409, "This repair was already decided or no longer matches the PR's version. "
                             "Refresh before deciding.")
  return original, target, attempt


def _discard_repair(db, row, original, attempt):
  previous = row.outcomes_json[reviews.key(original)]
  attempts = [{**a, "state": "discarded", "discarded_at": reviews.utc_now_iso()} if a.get("id") == attempt["id"] else a
              for a in previous.get("repair_attempts", [])]
  reviews.save_outcome(db, row, reviews.key(original), {**previous, "state": "needs_you",
    "summary": "You discarded the proposed repair. Nothing was pushed.", "repair_attempts": attempts},
    expected=previous)


async def _push_repair_once(db, row, target, attempt, cwd) -> str | None:
  """The owner's push of one exact proposal: re-proved now, then the one guarded push.

  Returns why it was not started, if a saved attempt already owns it. Its
  result (pushed, push_unknown, or released) is saved on the attempt itself.
  """
  previous = row.outcomes_json[reviews.key(target)]
  owner = _RunAuthority(lambda: _owner_decision_authority(db, row), attempt.get("author_run_id"), row.chat_id)
  async with _github_connection_transaction():
    repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation; no mutation)
    facts = await asyncio.to_thread(pr_facts.read_facts, _gh, cwd, target, repo, pull,  # unsettled: read-only (GitHub reviews and checks)
      actor_id=row.github_actor_id,
      job=_pr_job(db, row.owner_id, target["repo"], target["number"], exclude_run_id=row.id, app_id=row.app_id))
    pr_facts.require_allowed(facts, "finish_it" if facts["role"] == "author" else "take_over")
    validation = await _validated_repair(db, row, owner, target, previous, cwd)
    if (validation["head_sha"], validation["diff_sha256"], validation["base_sha"]) != (
        attempt["head_sha"], attempt["diff_sha256"], attempt["base_sha"]):
      raise HTTPException(409, "The repair or its target branch changed since it was proposed. Nothing was pushed. "
                               "Discard it; the agent proposes the new combined result.")
    receipt = {**attempt, **validation, "state": "pushing", "pushed_by": "owner", "decided_at": reviews.utc_now_iso()}
    return await repair_actions.publish_attempt(db, row, target, validation, receipt, owner,
      gh=_gh, cwd=cwd, authorize=owner.authorize)


def _repair_resume_note(target, attempt) -> str:
  pr = f"{target['repo']}#{target['number']}"
  return {
    "pushed": (f"The owner pushed your repair of {pr}; its head is now {attempt['head_sha'][:12]}. Start a fresh "
               "independent review of that exact head through /reviewers before reporting. Never merge or mark it "
               "ready for review: those are the owner's own taps."),
    "push_unknown": (f"The owner pushed your repair of {pr}, but GitHub did not confirm it. Do not push again: "
                     "POST /observe to reconcile it read-only, and continue only from a confirmed head."),
    "discarded": (f"The owner discarded your proposed repair of {pr} at {attempt['from_sha'][:12]}. Do not push "
                  "it. Ask them what they want instead, or end here."),
  }.get(attempt.get("state"), f"The owner's push of your repair of {pr} was refused before anything reached "
                              "GitHub. Read the item's summary, then propose again only if it still applies.")


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/repairs/decision",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def decide_repair(app_id: int | None, run_id: str, body: RepairDecision,
                        db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  """The owner's own push or discard of one exact repair the run proposed.

  200 {run, repair, resumed, blocked?}: `repair` is the attempt as saved now
  (pushed, push_unknown, discarded, or absent when a pre-dispatch refusal
  released it), `resumed` whether the run's conversation was woken with the
  decision. 403 for any agent bearer; 409 when the proposal was already
  decided, no longer matches the PR's version, or the PR machine refuses.
  """
  _require_owner_hands(principal, "Only you can push or discard a repair. An agent cannot approve its own push.")
  owner_grants.consume_owner_grant(db, principal, app_id, "repair", body, run_id=run_id)
  row = _row(db, app_id, run_id, principal)
  _repair_allowed(row)
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    _owner_decision_authority(db, row)
    original, target, attempt = _proposed_repair(row, body)
    blocked = None
    if body.decision == "discard":
      _discard_repair(db, row, original, attempt)
    else:
      blocked = await _push_repair_once(db, row, target, attempt, Path(get_settings().data_dir) / "platform")
    db.refresh(row)
    item = row.outcomes_json[reviews.key(original)]
    saved = next((a for a in item.get("repair_attempts", []) if a.get("id") == attempt["id"]), None)
    if saved is None:
      # A refusal proved before the push released the attempt; its reason is the item's summary.
      blocked = blocked or item.get("summary")
    resumed = await _resume_run(db, row, _repair_resume_note(target, saved or attempt))
    db.refresh(row)
    return {"run": _run_view(db, row), "repair": saved, "resumed": resumed, **({"blocked": blocked} if blocked else {})}


@router.post("/api/github/review-runs/{run_id}/repairs/decision",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_decide_repair(run_id: str, body: RepairDecision, db: Session = Depends(get_db),
                             principal: Principal = Depends(get_principal)):
  return await decide_repair(None, run_id, body, db, principal)


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
      # Registration resets the item to reviewing. That would hide a live merge
      # from /observe and the cross-grant fence, or mask an unclear push.
      if _merge_attempted(previous) or any(a.get("state") in {"pushing", "push_unknown"}
                                           for a in previous.get("repair_attempts", [])):
        raise HTTPException(409, "An earlier merge or push for this item must be reconciled before a new reviewer starts.")
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


def _require_ready_attempt_resolved(previous, target):
  # A retired (unattributable) receipt no longer fences other heads, but the
  # readiness of its own head stays unknown.
  attempt = previous.get("ready_attempt") or {}
  if reviews.ready_pending(previous) or (
      attempt.get("state") == "unattributable" and attempt.get("head_sha") == target["head_sha"]):
    raise HTTPException(409, "The draft readiness attempt is uncertain. Reconcile it read-only before any new public action.")


async def _confirm_ready_attempt(target, cwd) -> str | None:
  """Observe only PR identity, the saved head and non-draft state.

  A ReadyForReviewEvent has no head SHA, so a moved head cannot prove that the
  event belongs to this attempt. Never use today's actor or rights to judge a
  past GitHub effect.
  """
  try:
    _repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation)
  except HTTPException:
    # The selection no longer matches, but the same PR still proves its own
    # head and draft state.
    pull = await asyncio.to_thread(reviews.selected_pull, _gh, cwd, target)  # unsettled: read-only (GitHub observation)
    if pull is None:
      return "The earlier readiness attempt could not be read or tied to this pull request; it was not repeated."
  except (ContributionSubmitError, subprocess.SubprocessError, OSError, json.JSONDecodeError):
    return "The earlier readiness attempt is uncertain; it was not repeated."
  head = pull.get("head")
  head_sha = head.get("sha") if isinstance(head, dict) else None
  if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
    return "The earlier readiness attempt has no trustworthy live head; it was not repeated."
  if head_sha != target["head_sha"]:
    return READY_HEAD_MOVED
  if pull.get("draft") is not False:
    return f"The earlier readiness attempt is not confirmed; it was not repeated. {reviews.READY_UNKNOWN_RECOVERY}"
  return None


async def _reconcile_ready_attempt(db, row, item_key, attempt, cwd) -> str | None:
  """Settle one saved readiness attempt read-only at its own saved head.

  Returns why it stays unconfirmed, or None once it is confirmed ready.
  """
  original = next(t for t in row.targets_json if reviews.key(t) == item_key)
  read_at = time.time()
  blocked = await _confirm_ready_attempt({**original, "head_sha": attempt["head_sha"]}, cwd)
  if blocked is None:
    if attempt.get("state") != "ready":
      reviews.save_ready_attempt(db, row, item_key, {**attempt, "state": "ready"})
    return None
  if attempt.get("state") == "attempting":
    # A leftover "attempting" receipt (crash or lost response) that cannot be
    # confirmed must surface to the owner instead of silently blocking.
    reviews.save_ready_attempt(db, row, item_key, {**attempt, "state": "unknown"})
  elif attempt.get("state") == "unknown" and blocked == READY_HEAD_MOVED:
    # GitHub's mark-ready mutation names no head: a request still in flight
    # (an orphaned gh child after a crash) would mark whatever head is live.
    # Keep the fence until every request for this attempt has been evaluated
    # or dropped; the old-head receipt and audit always remain.
    if not reviews.dispatch_window_closed(attempt.get("armed_at"), read_at):
      return f"{blocked} {READY_WINDOW_OPEN}"
    reviews.save_ready_attempt(db, row, item_key, {**attempt, "state": "unattributable"})
  return blocked


