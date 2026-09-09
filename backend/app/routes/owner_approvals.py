"""Saved owner approvals, using the ordinary question/answer lifecycle.

Creation acknowledges a committed card, never an approval. The agent ends its
turn after this receipt; the existing answer queue owns the continuation.
There is no human timeout, held request, new table, or approval executor.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app import chat_queue, models, questions, secure_inputs
from app.chat_event_sink import get_active_sink
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, reject_cross_site
from app.owner_input import publish_owner_input_changed
from app.resource_access import get_active_chat_for_principal
from app.agent_work_claims import claim_work

router = APIRouter(prefix="/api/chats", tags=["owner-approvals"])


class ApprovalOption(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
  label: str = Field(min_length=1, max_length=100)
  description: str = Field(min_length=1, max_length=500)
  on_answer: Literal["resume", "close"] | None = None


class ApprovalRequest(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
  question: str = Field(min_length=1, max_length=2000)
  options: list[ApprovalOption] = Field(min_length=2, max_length=3)
  work_key: str = Field(min_length=3, max_length=256)

  @model_validator(mode="after")
  def unique_options(self):
    if len({option.label for option in self.options}) != len(self.options):
      raise ValueError("approval options must have distinct labels")
    return self


class QuestionSpec(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
  id: str = Field(min_length=1, max_length=80)
  header: str = Field(min_length=1, max_length=80)
  question: str = Field(min_length=1, max_length=2000)
  options: list[ApprovalOption] = Field(default_factory=list, max_length=3)

  @model_validator(mode="after")
  def unambiguous_quiet_options(self):
    if (any(option.on_answer == "close" for option in self.options)
        and len({option.label for option in self.options}) != len(self.options)):
      raise ValueError("quiet-answer options must have distinct labels")
    return self


class QuestionRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  questions: list[QuestionSpec] = Field(min_length=1, max_length=3)

  @model_validator(mode="after")
  def distinct_questions(self):
    if len({q.id for q in self.questions}) != len(self.questions):
      raise ValueError("question ids must be distinct")
    if len({q.question for q in self.questions}) != len(self.questions):
      raise ValueError("question prompts must be distinct")
    return self


class RestartRequest(BaseModel):
  """The server, not the caller, derives the exact restart action."""

  model_config = ConfigDict(extra="forbid")


@router.post("/{chat_id}/question", dependencies=[Depends(reject_cross_site)])
async def request_question(
  chat_id: str,
  body: QuestionRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  return await save_owner_question(chat_id, body.model_dump(exclude_none=True), principal, db)


@router.post("/{chat_id}/approval", dependencies=[Depends(reject_cross_site)])
async def request_approval(
  chat_id: str,
  body: ApprovalRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  public_body = body.model_dump(exclude={"work_key"}, exclude_none=True)
  return await save_owner_question(chat_id, {
    "questions": [{"id": "approval", "header": "Approval", **public_body}],
    "action_key": body.work_key,
  }, principal, db, identity_payload=body.model_dump(exclude_none=True),
     approval_work_key=body.work_key,
     approval_summary=body.question[:500])


@router.post("/{chat_id}/restart-request", dependencies=[Depends(reject_cross_site)])
async def request_restart(
  chat_id: str,
  _body: RestartRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Save one platform-owned Restart card for current committed source."""
  from app.platform_restart import (
    RestartRequirementError,
    build_restart_requirement,
  )

  try:
    requirement = build_restart_requirement()
  except RestartRequirementError as exc:
    raise HTTPException(
      status_code=409,
      detail={
        "code": str(exc),
        "message": (
          "Möbius could not bind a Restart card to exact committed, "
          "restart-loadable platform source."
        ),
      },
    ) from exc
  action_id = requirement["action_id"]
  not_now_id = str(uuid5(NAMESPACE_URL, f"{action_id}:not-now"))
  restart_now_id = str(uuid5(NAMESPACE_URL, f"{action_id}:restart-now"))
  paths = requirement["paths"]
  path_summary = ", ".join(paths[:3])
  if len(paths) > 3:
    path_summary += f", and {len(paths) - 3} more"
  payload = {
    "questions": [{
      "id": "restart",
      "header": "Restart Möbius",
      "question": (
        f"Restart to load the tested platform changes in {path_summary}? "
        "This interrupts active turns and Möbius may be unavailable for "
        "tens of seconds."
      ),
      "options": [
        {
          "id": not_now_id,
          "label": "Not now",
          "on_answer": "close",
          "description": "Leave these committed changes pending without interruption.",
        },
        {
          "id": restart_now_id,
          "label": "Restart now",
          "on_answer": "close",
          "description": "Drain active work and restart once to load these exact changes.",
        },
      ],
    }],
    "platform_action": {
      "version": 1,
      "type": "restart",
      "action_id": action_id,
      "restart_option_id": restart_now_id,
      "cancel_option_id": not_now_id,
      "requirement": requirement,
      "status": "awaiting_owner",
    },
  }
  return await save_owner_question(
    chat_id,
    payload,
    principal,
    db,
    identity_payload={"type": "request_restart", "requirement": requirement},
    activation_requirement=requirement,
  )


async def save_owner_question(
  chat_id: str, payload: dict, principal: Principal, db: Session, *,
  secure_request: dict | None = None, identity_payload: dict | None = None,
  approval_work_key: str | None = None,
  approval_summary: str | None = None,
  activation_requirement: dict | None = None,
):
  """One save-before-receipt owner for every terminal question card."""
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  # An identical retry in the same physical turn addresses the same card,
  # including when the first HTTP response was lost after commit.
  question_id = str(uuid5(NAMESPACE_URL, json.dumps(
    [chat_id, principal.run_id, identity_payload if identity_payload is not None else payload], sort_keys=True,
  )))
  # Stable identities belong to the saved card, never label inference in the
  # answer route. Keep legacy cards byte-for-byte unchanged.
  payload = deepcopy(payload)
  if activation_requirement is None and questions.has_quiet_options(payload):
    for question in payload.get("questions", []):
      for index, option in enumerate(question.get("options", [])):
        option["id"] = str(index)
  activation_wait = None
  if activation_requirement is not None:
    wait_id = f"activation-{question_id}"
    action = payload.get("platform_action")
    if not isinstance(action, dict):
      raise HTTPException(500, detail="Restart card action metadata is missing.")
    action["wait_id"] = wait_id
    activation_wait = {
      "id": wait_id,
      "description": "Load the exact committed platform changes",
      "condition_owner": "Möbius startup",
      "condition_json": activation_requirement,
      "root_run_id": None,
      "goal_id": None,
      "linked_question_id": question_id,
    }
  async with chat_queue.get_transition_lock(chat_id):
    async with chat_queue.get_lock(chat_id):
      chat = get_active_chat_for_principal(db, chat_id, principal)
      db.refresh(chat)
      run = db.get(models.ChatRun, principal.run_id)
      sink = get_active_sink(chat_id)
      if (run is None or run.status != "running" or sink is None
          or sink.run_token != principal.run_id):
        raise HTTPException(status_code=409, detail="This agent turn is no longer current.")
      if activation_wait is not None:
        activation_wait["created_by_run_id"] = run.id
        activation_wait["root_run_id"] = run.root_run_id or run.id
        activation_wait["goal_id"] = run.goal_id
      for message in reversed(chat.messages or []):
        for block in message.get("blocks") or []:
          if block.get("type") == "question" and block.get("question_id") == question_id:
            if block.get("answers"):
              state = "answered"
            elif chat.pending_question_id == question_id:
              state = "waiting_for_owner"
            else:
              raise HTTPException(status_code=409, detail="This approval is no longer open.")
            return _receipt(
              state, question_id,
              platform_restart=activation_requirement is not None,
            )
      if (chat.pending_question_id is not None or questions.is_waiting(chat_id)
          or secure_inputs.has_open_request(chat_id)):
        raise HTTPException(status_code=409, detail="Answer the open question before asking another.")
      # A rejected/stale card must not mutate workspace ownership. Claim only
      # after the exact run, sink, retry identity, and single-card admission
      # have all passed under the same lifecycle locks.
      if approval_work_key is not None:
        try:
          claim = claim_work(
            db,
            owner_id=principal.owner.id,
            chat_id=principal.chat_id,
            run_id=principal.run_id,
            work_key=approval_work_key,
            summary=approval_summary or payload["questions"][0]["question"][:500],
          )
        except ValueError as exc:
          raise HTTPException(409, str(exc)) from exc
        if claim["state"] == "completed":
          raise HTTPException(
            409,
            "This exact work already completed: "
            f"{claim.get('outcome') or approval_work_key}",
          )
        if claim["owner_chat_id"] != principal.chat_id:
          raise HTTPException(
            409,
            "This approval is already owned by "
            f"{claim['owner_name']} ({claim['owner_chat_id']}); "
            "no duplicate card was created.",
          )
      try:
        await sink.publish_question({
          "type": "question",
          "question_id": question_id,
          "response_mode": "continuation",
          **payload,
        }, **(
          {"secure_request": secure_request}
          if secure_request is not None
          else {"activation_wait": activation_wait}
            if activation_wait is not None
            else {}
        ))
      except Exception as exc:
        raise HTTPException(
          status_code=503, detail="Could not save the approval card; retry the same request.",
        ) from exc
      publish_owner_input_changed(chat_id, "question", question_id=question_id)
      return _receipt(
        "waiting_for_owner", question_id,
        platform_restart=activation_requirement is not None,
      )


def _receipt(
  state: str, question_id: str, *, platform_restart: bool = False,
) -> dict:
  return {
    "state": state,
    "question_id": question_id,
    "next_action": (
      "End this turn now without further text or tools. This receipt is not "
      "approval and not an answer. The platform handles an eventual Restart "
      "now choice and resumes this work only after loaded-source readiness; "
      "do not issue or replay a restart command."
      if platform_restart else
      "End this turn now without further text or tools. This receipt is not approval and not an answer. The owner's answer "
      "is saved and normally resumes the chat; explicit close choices need no reply. Do not poll or "
      "wait on a process, and do not perform the proposed action yet."
    ),
  }
