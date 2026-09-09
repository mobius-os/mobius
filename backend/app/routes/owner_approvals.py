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


async def save_owner_question(
  chat_id: str, payload: dict, principal: Principal, db: Session, *,
  secure_request: dict | None = None, identity_payload: dict | None = None,
  approval_work_key: str | None = None,
  approval_summary: str | None = None,
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
  if questions.has_quiet_options(payload):
    for question in payload.get("questions", []):
      for index, option in enumerate(question.get("options", [])):
        option["id"] = str(index)
  async with chat_queue.get_transition_lock(chat_id):
    async with chat_queue.get_lock(chat_id):
      chat = get_active_chat_for_principal(db, chat_id, principal)
      db.refresh(chat)
      run = db.get(models.ChatRun, principal.run_id)
      sink = get_active_sink(chat_id)
      if (run is None or run.status != "running" or sink is None
          or sink.run_token != principal.run_id):
        raise HTTPException(status_code=409, detail="This agent turn is no longer current.")
      for message in reversed(chat.messages or []):
        for block in message.get("blocks") or []:
          if block.get("type") == "question" and block.get("question_id") == question_id:
            if block.get("answers"):
              state = "answered"
            elif chat.pending_question_id == question_id:
              state = "waiting_for_owner"
            else:
              raise HTTPException(status_code=409, detail="This approval is no longer open.")
            return _receipt(state, question_id)
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
        }, **({"secure_request": secure_request} if secure_request is not None else {}))
      except Exception as exc:
        raise HTTPException(
          status_code=503, detail="Could not save the approval card; retry the same request.",
        ) from exc
      publish_owner_input_changed(chat_id, "question", question_id=question_id)
      return _receipt("waiting_for_owner", question_id)


def _receipt(state: str, question_id: str) -> dict:
  return {
    "state": state,
    "question_id": question_id,
    "next_action": (
      "End this turn now without further text or tools. This receipt is not approval and not an answer. The owner's answer "
      "is saved and normally resumes the chat; explicit close choices need no reply. Do not poll or "
      "wait on a process, and do not perform the proposed action yet."
    ),
  }
