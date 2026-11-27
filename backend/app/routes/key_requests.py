"""An agent's key request, the one owner ask that saves its own card mid-turn.

Everything else an agent needs from the owner is recorded with `owner_input`
and shown when the turn ends by itself. A key request is the exception: the
key vault records the request, and when the owner must answer, the ask is
saved here as a barring key card (`Chat.pending_question_id`) that ends the
turn. Its receipt names the card; the run's sink (`ends_turn`) confirms that
this exact turn saved it before Claude's turn-end hook or Codex's `turn-end`
rendezvous cuts the turn. The answer is checked against the vault
(`key_request_cards.check_answer`) and resumes the agent.
"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import chat_queue, key_request_cards, key_vault, models, questions, secure_inputs
from app import transcript_rows
from app.chat_event_sink import get_active_sink
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, reject_cross_site
from app.owner_input import publish_owner_input_changed
from app.resource_access import get_active_chat_for_principal

router = APIRouter(prefix="/api/chats", tags=["key-requests"])

# The turn's closing message travels with the card that ends the turn, so it
# reaches the owner as reliably as the card itself. It is not card identity:
# an identical retry with reworded prose addresses the same saved card, and the
# first saved message is kept, so a retry can never revise what the owner read.
CLOSING_MESSAGE_MAX_CHARS = 20_000
ClosingMessage = Annotated[str | None, Field(max_length=CLOSING_MESSAGE_MAX_CHARS)]


class KeyRequest(BaseModel):
  """An agent's ask to use a key on sites; the vault validates the fields."""

  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
  key: str = Field(min_length=1, max_length=64)
  # The vault keeps at most this much of a reason (MAX_REASON).
  reason: str = Field(min_length=1, max_length=300)
  sites: list[dict] = Field(default_factory=list, max_length=20)
  inject: dict | None = None
  # The parts a new key is pasted as (names, labels, secrecy; never values).
  parts: list[dict] | None = Field(default=None, max_length=8)
  message: ClosingMessage = None


def _live_chat_title(db: Session, chat_id: str) -> str | None:
  """``chat_id``'s title as an agent is told it, or None when that chat is deleted."""
  row = db.query(models.Chat.title).filter(
    models.Chat.id == chat_id, models.Chat.deleted_at.is_(None),
  ).first()
  return None if row is None else (row[0] or "Untitled chat")


@router.post("/{chat_id}/key-request", dependencies=[Depends(reject_cross_site)])
async def request_key(
  chat_id: str,
  body: KeyRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Ask the vault; when the owner must answer, save the ask as this turn's card.

  A key the agent may already use where it asked answers at once, with no
  card, and the turn continues.
  """
  from app.routes.keys import agent_asks

  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  ask = body.model_dump(exclude={"message"}, exclude_none=True)

  answer = await agent_asks(ask, principal)
  if not key_request_cards.needs_owner(answer):
    return {"state": "answered_without_card", "answer": answer}
  waiting = await key_request_cards.waiting_elsewhere(answer, chat_id)
  if waiting is not None:
    # One request per key speaks for every agent; it is answered on the card
    # in the chat that asked, if that chat still shows one, or in Settings ›
    # Keys, which lists every waiting request.
    holder = waiting.get("chat_id")
    title = (_live_chat_title(db, holder)
             if holder and key_request_cards.holds_open_card(db, holder, waiting["id"]) else None)
    return {
      "state": "answered_without_card", "answer": answer,
      "waiting_chat_id": holder if title is not None else None,
      "note": key_request_cards.elsewhere_note(title),
    }
  try:
    receipt = await _save_key_card(
      chat_id, key_request_cards.card_payload(answer),
      principal, db, closing_message=body.message,
    )
  except Exception:
    # The turn stopped, or the card could not be saved: unless this chat
    # still shows a card for it, withdraw this chat's request, so the owner
    # isn't asked for something no agent is waiting on.
    db.rollback()
    try:
      stored = await key_request_cards.stored_request(answer)
    except HTTPException:
      stored = None  # the vault is down: the request shows in Settings › Keys
    if (stored is not None and stored.get("chat_id") == chat_id
        and not key_request_cards.holds_open_card(db, chat_id, answer["id"])):
      await key_vault.withdraw_request(answer["id"], stored.get("ticket"))
    raise
  return {**receipt, "answer": answer}


async def _save_key_card(
  chat_id: str, payload: dict, principal: Principal, db: Session, *,
  closing_message: str | None = None,
) -> dict:
  """Save the key card before its receipt, once per identical ask in a turn."""
  # An identical retry in the same physical turn addresses the same card,
  # including when the first HTTP response was lost after commit.
  question_id = str(uuid5(NAMESPACE_URL, json.dumps(
    [chat_id, principal.run_id, payload], sort_keys=True,
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
      for message in reversed(transcript_rows.history(chat)):
        for block in message.get("blocks") or []:
          if block.get("type") == "question" and block.get("question_id") == question_id:
            if block.get("answers"):
              state = "answered"
            elif chat.pending_question_id == question_id:
              state = "waiting_for_owner"
            else:
              raise HTTPException(status_code=409, detail="This key request is no longer open.")
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
          **({"message": closing_message} if closing_message else {}),
        })
      except Exception as exc:
        raise HTTPException(
          status_code=503, detail="Could not save the key card; retry the same request.",
        ) from exc
      # The key card bars the chat, and saving it retired any open list card.
      publish_owner_input_changed(
        chat_id, "question", question_id=question_id, request_card_id=None,
      )
      return _receipt("waiting_for_owner", question_id)


def _receipt(state: str, question_id: str) -> dict:
  return {
    "state": state,
    "question_id": question_id,
    "next_action": (
      "The turn is over: the key card was saved and the response is cut "
      "here, so nothing further can be delivered. This receipt is not an "
      "answer and grants no key. The owner's answer is saved and resumes the "
      "chat as Allow or Not now. Do not poll or wait on a process."
    ),
  }
