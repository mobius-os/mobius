"""Saved owner approvals, using the ordinary question/answer lifecycle.

Creation acknowledges a committed card, never an approval. The agent ends its
turn after this receipt; the existing answer queue owns the continuation.
There is no human timeout, held request, new table, or approval executor.
"""

from __future__ import annotations

import json
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

router = APIRouter(prefix="/api/chats", tags=["owner-approvals"])


class ApprovalOption(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
  label: str = Field(min_length=1, max_length=100)
  description: str = Field(min_length=1, max_length=500)


class ApprovalRequest(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
  question: str = Field(min_length=1, max_length=2000)
  options: list[ApprovalOption] = Field(min_length=2, max_length=3)

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
  return await save_owner_question(chat_id, body.model_dump(), principal, db)


@router.post("/{chat_id}/approval", dependencies=[Depends(reject_cross_site)])
async def request_approval(
  chat_id: str,
  body: ApprovalRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  return await save_owner_question(chat_id, {
    "questions": [{"id": "approval", "header": "Approval", **body.model_dump()}],
  }, principal, db, identity_payload=body.model_dump())


async def save_owner_question(
  chat_id: str, payload: dict, principal: Principal, db: Session, *,
  secure_request: dict | None = None, identity_payload: dict | None = None,
):
  """One save-before-receipt owner for every terminal question card."""
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  # An identical retry in the same physical turn addresses the same card,
  # including when the first HTTP response was lost after commit.
  question_id = str(uuid5(NAMESPACE_URL, json.dumps(
    [chat_id, principal.run_id, identity_payload if identity_payload is not None else payload], sort_keys=True,
  )))
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
      "is saved and resumes the chat in a subsequent turn; do not poll or "
      "wait on a process, and do not perform the proposed action yet."
    ),
  }
