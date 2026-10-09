"""Durable delegation submit/attach, status, history, and cancellation API."""

from __future__ import annotations

import re
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

from app import models, providers, transcript_rows
from app.chat_start import start_programmatic_chat_turn
from app.database import get_db
from app.config import get_settings
from app.delegations import (
  ACTIVE_DELEGATION_STATUSES,
  AWAITING_INPUT_DELEGATION_STATUSES,
  QuestionRejected,
  ask_parent_question,
  DelegationIntent,
  cancel_delegation_execution,
  claim_inline_delegation_observation,
  create_or_attach_delegation,
  derived_status,
  delegation_goal_id,
  delegation_source_work_filter,
  ensure_delegation_started,
  normalize_cwd,
  parent_root_run_id,
  delegation_source_work_id,
  _parent_wake_continuation_root,
  parent_wake_blocker,
  publish_parent_waiting_changed,
  record_result_read_by_parent,
  retry_limit_park,
  serialize_delegation,
  serialize_delegation_list,
)
from app.deps import Principal, get_delegation_principal, reject_cross_site
from app.resource_access import get_active_chat_or_404


router = APIRouter(prefix="/api/delegations", tags=["delegations"])
_TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DelegationSubmit(BaseModel):
  app_id: int | None = Field(default=None, gt=0)
  parent_chat_id: str = Field(min_length=1, max_length=64)
  task_key: str = Field(min_length=1, max_length=128)
  prompt: str = Field(min_length=1, max_length=200_000)
  provider: str
  model: str | None = Field(default=None, max_length=256)
  effort: str | None = Field(default=None, max_length=32)
  # An older app may send this field. Reject read, never silently promote it.
  scope: Literal["write"] | None = None
  cwd: str | None = Field(default=None, max_length=1024)
  # The parent Goal plan task this helper works on; omitted means the plan's
  # single running task, if there is exactly one.
  plan_task: str | None = Field(default=None, min_length=1, max_length=128)
  # Wake the parent chat with the result when the child settles. Defaults on for
  # the owner-agent subagent path; a pure-poll caller can pass False.
  notify_parent_on_complete: bool = True

  @field_validator("task_key")
  @classmethod
  def _valid_task_key(cls, value: str) -> str:
    value = value.strip()
    if not _TASK_KEY_RE.fullmatch(value):
      raise ValueError(
        "task_key must start with a letter/number and use only . _ or -"
      )
    return value

  @field_validator("prompt")
  @classmethod
  def _clean_prompt(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("prompt must not be empty")
    return value

  @field_validator("provider")
  @classmethod
  def _valid_provider(cls, value: str) -> str:
    if value not in providers.PROVIDERS:
      raise ValueError("unknown provider")
    return value


def _require_submitter(
  db: Session, principal: Principal, body: DelegationSubmit,
) -> models.Delegation | None:
  if principal.delegation_id is None:
    if principal.scope == "owner" and principal.app_id is None:
      return None
    if principal.scope != "app" or principal.app_id != body.app_id:
      raise HTTPException(
        status_code=403,
        detail="Only the owner agent or an attached delegated agent may submit work.",
      )
    raise HTTPException(
      status_code=403,
      detail="Delegated work must stay under its parent child chat.",
    )

  if principal.chat_id != body.parent_chat_id:
    raise HTTPException(
      status_code=403,
      detail="Delegation token may only create direct children.",
    )
  parent = db.query(models.Delegation).filter(
    models.Delegation.id == principal.delegation_id,
    models.Delegation.child_chat_id == body.parent_chat_id,
    models.Delegation.cancelled_at.is_(None),
  ).first()
  if parent is None:
    raise HTTPException(status_code=403, detail="Delegated work must stay under its parent child chat.")
  if body.app_id is not None and body.app_id != parent.app_id:
    raise HTTPException(status_code=403, detail="Delegated work must keep its parent app owner.")
  if parent.scope != "write" or parent.interrupted_at is not None:
    raise HTTPException(status_code=409, detail="Legacy helper cannot delegate new work.")
  return parent


def _row_for_principal(
  db: Session, delegation_id: str, principal: Principal,
) -> models.Delegation:
  query = db.query(models.Delegation).filter(
    models.Delegation.id == delegation_id,
  )
  if principal.delegation_id is not None:
    query = query.filter(models.Delegation.parent_chat_id == principal.chat_id)
  elif principal.app_id is not None:
    query = query.filter(models.Delegation.app_id == principal.app_id)
  row = query.first()
  if row is None:
    raise HTTPException(status_code=404, detail="Delegation not found.")
  return row


def _require_guest_child_lineage(row: models.Delegation, principal: Principal) -> None:
  """A guest may not start a clean owner or another guest's child run."""
  if principal.browser_grant_id is not None and row.browser_grant_id != principal.browser_grant_id:
    raise HTTPException(status_code=403, detail="This helper belongs to another browser authority.")


async def _ensure_started(
  db: Session, row: models.Delegation, prompt: str,
) -> None:
  await ensure_delegation_started(
    db, row, prompt, start_turn=start_programmatic_chat_turn,
  )


@router.post("", status_code=201, dependencies=[Depends(reject_cross_site)])
async def submit_or_attach(
  body: DelegationSubmit,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Create once per (parent logical run, task key), otherwise attach."""
  parent_delegation = _require_submitter(db, principal, body)
  owner_app_id = parent_delegation.app_id if parent_delegation else body.app_id
  parent = get_active_chat_or_404(db, body.parent_chat_id)
  root_id = parent_root_run_id(db, parent.id, require_active=True)
  active_run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == parent.id,
    models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES),
  ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
  goal_id = active_run.goal_id if active_run is not None else None
  effective_goal_id = (
    (delegation_goal_id(db, parent_delegation) if parent_delegation else None)
    or goal_id
  )
  if root_id is None:
    raise HTTPException(
      status_code=409,
      detail="Delegation requires an active parent chat run.",
    )
  # Existing immutable work keeps its original owner across a platform upgrade.
  # Omission means core ownership only for a new task, not reassignment of a
  # historical app-owned child. The same live-app gate still applies below.
  if body.app_id is None and parent_delegation is None:
    previous = db.query(models.Delegation).filter(
      models.Delegation.parent_chat_id == parent.id,
      or_(models.Delegation.parent_root_run_id == root_id,
          and_(models.Delegation.goal_id.is_(None),
               models.Delegation.parent_root_run_id == effective_goal_id)),
      delegation_source_work_filter(effective_goal_id or root_id),
      models.Delegation.source_work_id.is_(None),
      models.Delegation.task_key == body.task_key,
    ).first()
    if previous is not None:
      owner_app_id = previous.app_id
  if owner_app_id is not None:
    app = db.query(models.App).filter(
      models.App.id == owner_app_id,
      models.App.deleted_at.is_(None),
    ).first()
    if app is None:
      raise HTTPException(status_code=404, detail="Delegation owner app not found.")
  if body.model and providers._model_belongs_to_other_provider(
    body.model, body.provider,
  ):
    raise HTTPException(
      status_code=422,
      detail="The selected model does not belong to that provider.",
    )
  selection = providers.snapshot_chat_agent_settings(
    get_settings().data_dir,
    body.provider,
    model=body.model or providers.DEFAULT_MODELS.get(body.provider),
    effort=body.effort or providers.DEFAULT_EFFORT,
    fallback_model=providers.DEFAULT_MODELS.get(body.provider),
  )
  if selection is None:
    raise HTTPException(
      status_code=422, detail="Delegation requires an explicit model.",
    )
  try:
    requested_cwd = normalize_cwd(body.cwd) if body.cwd is not None else None
  except ValueError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc

  from app import chat_queue
  async with AsyncExitStack() as admission:
    if owner_app_id is not None:
      await admission.enter_async_context(
        chat_queue.get_transition_lock(f"app-lifecycle:{owner_app_id}")
      )
    await admission.enter_async_context(chat_queue.get_transition_lock(parent.id))
    # App/chat deletion uses these same gates. End the authentication/read
    # snapshot and re-establish every admission fact under the locks so a
    # child cannot start after either owner has begun tombstoning.
    db.rollback()
    _require_submitter(db, principal, body)
    if owner_app_id is not None:
      app = db.query(models.App).filter(
        models.App.id == owner_app_id,
        models.App.deleted_at.is_(None),
      ).first()
      if app is None:
        raise HTTPException(
          status_code=404, detail="Delegation owner app not found.",
        )
    parent = get_active_chat_or_404(db, body.parent_chat_id)
    current_root_id = parent_root_run_id(db, parent.id, require_active=True)
    current_run = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == parent.id,
      models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES),
    ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    current_goal_id = current_run.goal_id if current_run is not None else None
    current_effective_goal_id = (
      (delegation_goal_id(db, parent_delegation) if parent_delegation else None)
      or current_goal_id
    )
    if current_root_id is None:
      raise HTTPException(
        status_code=409,
        detail="Delegation requires an active parent chat run.",
      )
    # Ownership and its lifecycle lock were selected for this logical root.
    # Never carry them into a newer run that began while admission waited.
    if current_root_id != root_id or current_effective_goal_id != effective_goal_id:
      raise HTTPException(
        status_code=409,
        detail="The parent chat run changed during delegation admission.",
      )
    existing = db.query(models.Delegation).filter(
      or_(models.Delegation.parent_root_run_id == root_id,
          and_(models.Delegation.goal_id.is_(None),
               models.Delegation.parent_root_run_id == effective_goal_id)),
      delegation_source_work_filter(effective_goal_id or root_id),
      models.Delegation.source_work_id.is_(None),
      models.Delegation.task_key == body.task_key,
    ).first()
    # Omitted cwd means "attach wherever this exact task already runs". This
    # preserves legacy rows created when older helpers materialized their shell
    # cwd, while a new task still gets the platform's stable /data default.
    # An explicit cwd remains immutable and is checked below with every other
    # task-defining field.
    cwd = (
      existing.cwd
      if requested_cwd is None and existing is not None
      else requested_cwd or normalize_cwd(None)
    )
    from app.goal_plans import GoalPlanError, helper_plan_task
    try:
      goal_task_id = helper_plan_task(db, parent.id, body.plan_task)
    except GoalPlanError as exc:
      raise HTTPException(status_code=422, detail=str(exc)) from exc
    intent = DelegationIntent(
      app_id=owner_app_id,
      parent_chat_id=parent.id,
      parent_root_run_id=root_id,
      goal_id=effective_goal_id,
      task_key=body.task_key,
      goal_task_id=goal_task_id,
      prompt=body.prompt,
      provider=body.provider,
      model=selection["model"],
      effort=selection.get("effort"),
      cwd=cwd,
      notify_parent_on_complete=body.notify_parent_on_complete,
      browser_grant_id=principal.browser_grant_id,
    )
    try:
      row, attached = create_or_attach_delegation(db, intent)
    except ValueError as exc:
      raise HTTPException(
        status_code=409,
        detail=(
          "That task key is already attached to different immutable work. "
          "Reuse the original prompt/policy or choose a new task key."
        ),
      ) from exc

    observation_mode = (
      claim_inline_delegation_observation(db, row)
      if attached and not body.notify_parent_on_complete
      else (
        "parent_wake" if row.notify_parent_on_complete else "inline"
      )
    )

    await _ensure_started(db, row, body.prompt)
    from app.goal_plans import publish_plan_for_delegation
    publish_plan_for_delegation(db, row)
    publish_parent_waiting_changed(row.parent_chat_id)
  payload = serialize_delegation(db, row)
  payload["attached"] = attached
  return payload


@router.get("/capabilities")
async def delegation_capabilities(
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Read-only helper preferences and registry, confined to the caller.

  A live Subagents installation is optional configuration, not delegation
  authority. Genuine app-owned children still require their live owner app.
  """
  if principal.delegation_id is not None:
    delegation = db.query(models.Delegation).filter(
      models.Delegation.id == principal.delegation_id,
      models.Delegation.child_chat_id == principal.chat_id,
    ).first()
    if delegation is None:
      raise HTTPException(status_code=403, detail="Delegation token is stale.")
    if delegation.app_id is not None:
      owner_app = db.query(models.App).filter(
        models.App.id == delegation.app_id,
        models.App.deleted_at.is_(None),
      ).first()
      if owner_app is None:
        raise HTTPException(status_code=403, detail="Delegation owner app is unavailable.")
  elif principal.scope != "owner" or principal.app_id is not None:
    raise HTTPException(status_code=403, detail="Owner agent or delegated child required.")
  app = db.query(models.App).filter(
    models.App.slug == "subagents", models.App.deleted_at.is_(None),
  ).first()

  def read_json(name: str) -> dict:
    if app is None:
      return {}
    path = Path(get_settings().data_dir) / "apps" / str(app.id) / name
    try:
      value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      return {}
    return value if isinstance(value, dict) else {}

  connections = {}
  for provider_id, provider in providers.PROVIDERS.items():
    error = provider.check_auth(get_settings().data_dir)
    connections[provider_id] = {
      "configured": error is None,
      "authenticated": error is None,
      "error": error,
    }
  registry = await providers.list_models(get_settings().data_dir)
  models_by_provider = {
    provider_id: [
      {"id": entry["id"], "name": entry["label"]}
      for entry in entries
    ]
    for provider_id, entries in registry.items()
  }
  aliases: dict[str, dict[str, list[str]]] = {}
  if app is not None and app.source_dir:
    try:
      catalog = json.loads((Path(app.source_dir) / "models.json").read_text(encoding="utf-8"))
      for provider_id, spec in (catalog.get("providers") or {}).items():
        aliases[provider_id] = {
          row["id"]: row.get("aliases", [])
          for row in spec.get("models", []) if isinstance(row, dict) and row.get("id")
        }
    except (OSError, ValueError, AttributeError, TypeError):
      pass
  return {
    "app_id": app.id if app is not None else None,
    "config": read_json("config.json"),
    "runtime": read_json("status.json"),
    "connections": connections,
    "models": models_by_provider,
    "aliases": aliases,
    "defaults": providers.DEFAULT_MODELS,
  }


@router.get("")
def list_delegations(
  app_id: int | None = Query(default=None, gt=0),
  parent_chat_id: str | None = Query(default=None, min_length=1, max_length=64),
  limit: int = Query(default=100, ge=1, le=500),
  offset: int = Query(default=0, ge=0),
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  query = db.query(models.Delegation)
  if principal.delegation_id is not None:
    query = query.filter(models.Delegation.parent_chat_id == principal.chat_id)
  elif principal.app_id is not None:
    query = query.filter(models.Delegation.app_id == principal.app_id)
  elif app_id is not None:
    query = query.filter(models.Delegation.app_id == app_id)
  if parent_chat_id is not None:
    query = query.filter(models.Delegation.parent_chat_id == parent_chat_id)
  rows = query.order_by(models.Delegation.created_at.desc()).offset(offset).limit(limit).all()
  return {"items": serialize_delegation_list(db, rows)}


@router.get("/{delegation_id}")
def get_delegation(
  delegation_id: str,
  include_history: bool = Query(default=False),
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  row = _row_for_principal(db, delegation_id, principal)
  payload = serialize_delegation(db, row)
  if include_history:
    child = db.query(models.Chat).filter(models.Chat.id == row.child_chat_id).first()
    payload["history"] = transcript_rows.read_all(db, child) if child is not None else []
  return payload


@router.post(
  "/{delegation_id}/result-read",
  dependencies=[Depends(reject_cross_site)],
)
def read_delegation_result(
  delegation_id: str,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Hand a helper's result to its parent agent and record that it was read.

  The agent-side read behind `list_agents`: a settled result returned here in
  full has reached the parent, so it is marked delivered and later wakes do
  not bring it back. Viewing a helper (GET) records nothing.
  """
  row = _row_for_principal(db, delegation_id, principal)
  if principal.chat_id and principal.chat_id != row.parent_chat_id:
    raise HTTPException(
      status_code=403, detail="Only the helper's parent chat may read its result.",
    )
  payload = serialize_delegation(db, row)
  if (
    payload.get("result")
    and not payload.get("result_truncated")
    and record_result_read_by_parent(db, row)
  ):
    db.commit()
    publish_parent_waiting_changed(row.parent_chat_id)
  return payload


class DelegationRetry(BaseModel):
  run_token: str = Field(min_length=1, max_length=128)


@router.post(
  "/{delegation_id}/retry",
  dependencies=[Depends(reject_cross_site)],
)
async def retry_delegation(
  delegation_id: str,
  body: DelegationRetry,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Try one exact quota-paused child after credits or a manual reset.

  The physical run token is a compare-and-swap boundary: an HTTP replay may
  observe the already-started replacement, but can never spend another retry
  against a newer park.
  """
  row = _row_for_principal(db, delegation_id, principal)
  _require_guest_child_lineage(row, principal)
  started = await retry_limit_park(db, row, run_token=body.run_token)
  db.rollback()
  row = _row_for_principal(db, delegation_id, principal)
  payload = serialize_delegation(db, row)
  payload["retry_started"] = started
  publish_parent_waiting_changed(row.parent_chat_id)
  return payload


@router.post(
  "/{delegation_id}/cancel",
  dependencies=[Depends(reject_cross_site)],
)
async def cancel_delegation(
  delegation_id: str,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  row = _row_for_principal(db, delegation_id, principal)
  status, _, _ = derived_status(db, row, load_result=False)
  if status in ACTIVE_DELEGATION_STATUSES | AWAITING_INPUT_DELEGATION_STATUSES:
    if not await cancel_delegation_execution(row.id):
      raise HTTPException(
        status_code=409,
        detail="The child is still stopping; retry cancellation shortly.",
      )
    db.rollback()
    row = _row_for_principal(db, delegation_id, principal)
  payload = serialize_delegation(db, row)
  from app.goal_plans import publish_plan_for_delegation
  publish_plan_for_delegation(db, row)
  publish_parent_waiting_changed(row.parent_chat_id)
  return payload


class DelegationMessage(BaseModel):
  message: str = Field(min_length=1, max_length=200_000)
  # Correlates an answer with the helper's exact question; required while the
  # helper is waiting, so a retried answer can never become a fresh follow-up.
  question_id: str | None = Field(default=None, min_length=1, max_length=64)

  @field_validator("message")
  @classmethod
  def _clean_message(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("message must not be empty")
    return value


@router.post(
  "/{delegation_id}/messages",
  status_code=202,
  dependencies=[Depends(reject_cross_site)],
)
async def message_delegation(
  delegation_id: str,
  body: DelegationMessage,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Give a settled helper a follow-up turn, or answer its exact question.

  Only the helper's own parent chat may message it. A follow-up is the
  helper's next user turn; its result reaches the parent exactly like the
  first one (live into a running parent turn, or by waking it). That result
  is a new child run, so it is owed without resetting any delivery record.
  A helper that is still working is refused rather than interrupted: the
  parent waits for its result or stops it.

  An answer (``question_id``) resumes the asking root through its one
  reserved run. Repeating the same answer attaches to that run; a different
  answer to an answered question is refused, never run as a second turn.
  While the helper is still running another turn of the asking root, the
  newest question's answer is accepted as that reserved run's queued row and
  starts when the turn ends (``answer_queued``). Every admission fact is reread under the child's transition lock, which
  cancellation and recovery share.
  """
  row = _row_for_principal(db, delegation_id, principal)
  _require_guest_child_lineage(row, principal)
  if principal.chat_id and principal.chat_id != row.parent_chat_id:
    raise HTTPException(
      status_code=403, detail="Only the helper's parent chat may message it.",
    )
  from app import chat_queue
  from app.chat_start import start_programmatic_chat_turn
  from app.delegations import (
    answer_may_queue,
    committed_answer_content,
    helper_answer_content,
    open_questions,
    queue_helper_answer,
    question_view,
    start_helper_answer,
  )
  already_answered = False
  answer_queued = False

  async def start_answer(row, question, content) -> None:
    row.notify_parent_on_complete = True
    db.commit()
    started = await start_helper_answer(
      row, question, content, _transition_lock_held=True,
    )
    # A committed answer whose task creation failed is still delivered:
    # delegation recovery reschedules that exact run.
    db.rollback()
    if not started and committed_answer_content(db, question) != content:
      raise HTTPException(
        status_code=409,
        detail="The helper could not resume with this answer now; retry shortly.",
      )

  goal_id = delegation_goal_id(db, row)
  goal_owner = db.query(models.ChatGoal.chat_id).filter(
    models.ChatGoal.id == goal_id,
  ).scalar() if goal_id is not None else None
  # Goal settlement and parent Stop must not overtake accepted child work.
  # Nested helpers share the coordinator's Goal, so lock ancestors first.
  async with AsyncExitStack() as admission:
    for chat_id in dict.fromkeys((goal_owner, row.parent_chat_id, row.child_chat_id)):
      if chat_id is not None:
        await admission.enter_async_context(chat_queue.get_transition_lock(chat_id))
    db.rollback()
    db.expire_all()
    row = _row_for_principal(db, delegation_id, principal)
    _require_guest_child_lineage(row, principal)
    status, run, _ = derived_status(db, row, load_result=False)
    if status == "cancelled":
      raise HTTPException(status_code=409, detail="This helper was stopped.")
    if status == "interrupted" or row.scope != "write":
      raise HTTPException(status_code=409, detail="This helper cannot resume; start a new helper.")
    awaited = open_questions(db, [(row, run)]).get(row.id)
    if body.question_id is None and awaited is not None:
      raise _question_refusal(
        "question_id_required",
        f"The helper is waiting for an answer to question_id {awaited.id}; "
        "answer it with message_agent(helper, message, question_id).",
        question=question_view(awaited),
      )
    if body.question_id is not None:
      question = db.query(models.DelegationQuestion).filter(
        models.DelegationQuestion.id == body.question_id,
        models.DelegationQuestion.delegation_id == row.id,
      ).first()
      if question is None:
        raise _question_refusal(
          "question_not_found", "This helper has no such question.", 404,
        )
      content = helper_answer_content(question, body.message)
      committed = committed_answer_content(db, question)
      if committed is not None:
        if committed != content:
          raise _question_refusal(
            "question_already_answered",
            "This question was already answered with a different answer; "
            "that answer stands. Send a new instruction after the helper "
            "settles, or stop it.",
          )
        # The same answer again: attach to (or recover) its one run.
        await start_helper_answer(
          row, question, content, _transition_lock_held=True,
        )
        already_answered = True
      elif awaited is not None and awaited.id == question.id:
        await start_answer(row, question, content)
      else:
        # The helper is running another turn of the asking root (e.g. its own
        # child's result woke it). Accept the answer as the reserved run's
        # queued row; that turn's drain starts it the moment it settles, so
        # the question never re-opens and the parent is not woken again.
        if answer_may_queue(db, row, run, question):
          row.notify_parent_on_complete = True
          db.commit()
          answer_queued = await queue_helper_answer(
            row, question, content, observed_run_id=run.id,
          )
        if not answer_queued:
          # The run settled between the reads: the question may now be open.
          db.rollback()
          row = _row_for_principal(db, delegation_id, principal)
          status, run, _ = derived_status(db, row, load_result=False)
          awaited = open_questions(db, [(row, run)]).get(row.id)
          if awaited is None or awaited.id != question.id:
            newer = db.query(models.DelegationQuestion.id).filter(
              models.DelegationQuestion.delegation_id == row.id,
              models.DelegationQuestion.created_at > question.created_at,
            ).first() is not None
            reason = (
              "the helper asked a newer question; answer that one"
              if newer else
              "the helper is running a different turn, so this question "
              "is closed; wait for its result"
              if status in ACTIVE_DELEGATION_STATUSES
              else f"the helper is {status}"
            )
            raise _question_refusal(
              "question_not_open",
              f"Question {question.id} is not awaiting an answer: {reason}. "
              "Nothing was saved.",
            )
          await start_answer(row, question, content)
    else:
      if status in ACTIVE_DELEGATION_STATUSES:
        raise HTTPException(
          status_code=409,
          detail=(
            "The helper is still working. Wait for its result before a follow-up; "
            "for a decision-changing note now, use send_agent_message(recipients, body) "
            "with its peer chat id from list_agent_peers."
          ),
        )
      source_work_id = delegation_source_work_id(row)
      source_root = _parent_wake_continuation_root(db, row.parent_chat_id, source_work_id)
      blocker, _ = parent_wake_blocker(
        db, row.parent_chat_id, source_work_id, source_root,
      )
      if blocker is not None or source_root is None:
        raise HTTPException(status_code=409, detail={
          "code": "followup_owner_unavailable",
          "reason": blocker or "source_missing",
          "message": (
            "This helper's original work can no longer own a follow-up result. "
            "Nothing was started. For new work, use spawn_agent to start a fresh "
            "helper under the current task or Goal; the old helper and its "
            "history stay unchanged. If the original work is on hold, resume "
            "that work only when the owner asks before messaging this helper."
          ),
        })
      row.notify_parent_on_complete = True
      db.commit()
      started = await start_programmatic_chat_turn(
        chat_id=row.child_chat_id,
        title=f"Delegation · {row.task_key}",
        content=body.message,
        provider=row.provider,
        initiated_by_app_id=row.app_id,
      )
      if not started:
        raise HTTPException(
          status_code=409, detail="The helper could not start a follow-up turn now.",
        )
  db.rollback()
  row = _row_for_principal(db, delegation_id, principal)
  publish_parent_waiting_changed(row.parent_chat_id)
  payload = serialize_delegation(db, row, include_result=False)
  if body.question_id is not None:
    payload["question_id"] = body.question_id
    payload["already_answered"] = already_answered
    payload["answer_queued"] = answer_queued
    if answer_queued:
      payload["note"] = (
        "The helper is finishing another turn of the same task. Your answer "
        "is saved and starts as soon as that turn ends; do not resend it."
      )
  return payload



@router.post(
  "/{delegation_id}/questions",
  dependencies=[Depends(reject_cross_site)],
)
def ask_parent(
  delegation_id: str,
  body: DelegationQuestionAsk,
  principal: Principal = Depends(get_delegation_principal),
  db: Session = Depends(get_db),
):
  """Record the calling helper run's one question for its parent.

  Only the helper's own run-bound bearer may ask, once per physical run.
  The receipt is stable for an exact retry. Nothing is delivered until the
  run ends: the settled run is the result the parent is woken with.
  """
  if (
    principal.delegation_id != delegation_id
    or not isinstance(principal.run_id, str) or not principal.run_id
  ):
    raise HTTPException(
      status_code=403, detail="Only the helper's own running turn may ask its parent.",
    )
  row = db.query(models.Delegation).filter(
    models.Delegation.id == delegation_id,
    models.Delegation.child_chat_id == principal.chat_id,
  ).first()
  if row is None:
    raise HTTPException(
      status_code=403, detail="Only the helper's own running turn may ask its parent.",
    )
  try:
    question = ask_parent_question(
      db, row, asking_run_id=principal.run_id,
      question=body.question, options=body.options,
    )
  except QuestionRejected as exc:
    raise _question_refusal(exc.code, str(exc), exc.status) from exc
  receipt = {
    "question_id": question.id,
    "helper_id": row.id,
    "status": "asked",
    "note": (
      "Question recorded for your parent. End your turn now without "
      "further work; its answer resumes this conversation."
    ),
  }
  # Like a confirmed closing save, a recorded question is a turn-ending
  # result: the asking run's own end hook stops the turn on this receipt, so
  # no further model request is made. Only the exact live run can end itself.
  from app.chat_event_sink import get_active_sink
  sink = get_active_sink(row.child_chat_id)
  if sink is not None and sink.run_token == principal.run_id:
    receipt["turn_end_id"] = sink.record_turn_end()
  return receipt


class DelegationQuestionAsk(BaseModel):
  question: str = Field(min_length=1, max_length=200_000)
  options: list[str] = Field(default_factory=list, max_length=16)

  @field_validator("question")
  @classmethod
  def _clean_question(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("question must not be empty")
    return value

  @field_validator("options")
  @classmethod
  def _clean_options(cls, values: list[str]) -> list[str]:
    cleaned = [value.strip() for value in values]
    if any(not value for value in cleaned):
      raise ValueError("options must not be empty")
    if any(len(value) > 200_000 for value in cleaned):
      raise ValueError("options must be at most 200000 characters")
    return cleaned


def _question_refusal(
  code: str, detail: str, status: int = 409, **extra,
) -> HTTPException:
  return HTTPException(
    status_code=status, detail={"code": code, "message": detail, **extra},
  )

async def cancel_active_for_parent(db: Session, parent_chat_id: str) -> list[str]:
  """Cascade an explicit parent Stop without affecting restart draining."""
  rows = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == parent_chat_id,
    models.Delegation.cancelled_at.is_(None),
  ).all()
  cancelled: list[str] = []
  for row in rows:
    status, _, _ = derived_status(db, row, load_result=False)
    if status not in ACTIVE_DELEGATION_STATUSES:
      continue
    if await cancel_delegation_execution(row.id):
      cancelled.append(row.id)
  db.rollback()
  if cancelled:
    publish_parent_waiting_changed(parent_chat_id)
  return cancelled
