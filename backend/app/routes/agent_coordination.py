"""Run-bound discovery and mailbox API for the provider-neutral peer network."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.agent_coordination import (
  DELIVERY_INTERRUPT,
  MAX_PEERS,
  MESSAGE_DELIVERIES,
  chat_message_history,
  context_message,
  interrupt_recipients,
  MESSAGE_KINDS,
  agent_network_snapshot,
  embedded_chat_snapshot,
  owner_chat_snapshot,
  owner_project_snapshot,
  classify_undelivered,
  peer_message_page_for_agent,
  peer_message_for_chat,
  send_agent_message,
  deliver_work_claim_revocation,
  visible_peer_messages,
)
from app.agent_work_claims import (
  pending_transfer_notices,
  claim_work,
  finish_work,
  read_claim_text,
)
from app.context_budget import READ_PAGE_BYTES
from app.database import get_db
from app.deps import (
  Principal,
  get_agent_run_principal,
  get_current_owner,
  get_owner_or_chat_embed_principal,
  reject_cross_site,
  require_chat_embed_operation,
)
from app.resource_access import get_active_chat_for_principal, require_active_chat_access
from app import models


router = APIRouter(prefix="/api/agent-coordination", tags=["agent-coordination"])


class AgentMessageCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  recipients: list[str] = Field(default_factory=list)
  broadcast: bool = False
  kind: str = "note"
  delivery: str = "next_turn"
  body: str = Field(min_length=1)
  send_id: str | None = Field(default=None, min_length=1, max_length=64)

  @field_validator("body")
  @classmethod
  def clean_body(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("body must not be blank")
    return value

  @field_validator("kind")
  @classmethod
  def valid_kind(cls, value: str) -> str:
    value = value.strip().lower()
    if value not in MESSAGE_KINDS:
      raise ValueError(f"kind must be one of: {', '.join(sorted(MESSAGE_KINDS))}")
    return value

  @field_validator("delivery")
  @classmethod
  def valid_delivery(cls, value: str) -> str:
    value = value.strip().lower()
    if value not in MESSAGE_DELIVERIES:
      raise ValueError("delivery must be next_turn or interrupt")
    return value


class AgentWorkClaimCreate(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

  work_key: str = Field(min_length=3, max_length=256)
  summary: str = Field(min_length=1)
  # A takeover must explain itself; ten characters rules out "x" or "ok".
  takeover_reason: str | None = Field(default=None, min_length=10)
  expected_owner_chat_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentWorkClaimFinish(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

  work_key: str = Field(min_length=3, max_length=256)
  outcome: str = Field(min_length=1)
  release: bool = False


@router.post("/work-claims", dependencies=[Depends(reject_cross_site)])
async def claim_current_work(
  body: AgentWorkClaimCreate,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  try:
    result = claim_work(
      db,
      owner_id=principal.owner.id,
      chat_id=principal.chat_id,
      run_id=principal.run_id,
      **body.model_dump(),
    )
  except ValueError as exc:
    raise HTTPException(409, str(exc)) from exc
  # Older obligations belong to the sweep, not a refresh/observer's request.
  revision = result.pop("_created_transfer_notice_revision", None)
  if revision is not None:
    try:
      notice = db.get(models.AgentWorkClaimRevocation, (result["id"], revision))
      delivery = await deliver_work_claim_revocation(db, notice)
      result["notification_pending"] = (
        pending_transfer_notices(db, claim_id=result["id"]).first() is not None
      )
      for state in ("steered", "woken", "queued", "unreachable"):
        result[state] = getattr(delivery, state)
    except Exception:
      # Ownership and its obligation already committed. Delivery failure must
      # not turn that accepted transfer into a false failure or lose recovery.
      db.rollback()
      logging.getLogger(__name__).exception("Committed claim revocation awaits recovery")
  return result


async def _deliver_finished_claim_dependencies():
  from app.goal_dependencies import deliver_pending_dependencies
  try:
    await deliver_pending_dependencies()
  except Exception:
    logging.getLogger(__name__).exception("Finished claim dependencies were not nudged")


@router.get("/work-claims/text")
def read_current_work_claim_text(
  work_key: str = Query(min_length=3, max_length=256),
  field: str = Query(),
  digest: str | None = Query(default=None, min_length=1, max_length=64),
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """One work-claim text whole, for an excerpt a claim result or notice named."""
  if principal.chat_id is None:
    raise HTTPException(403, "Current chat-bound agent run required.")
  try:
    text = read_claim_text(
      db, owner_id=principal.owner.id, chat_id=principal.chat_id,
      work_key=work_key, field=field, digest=digest,
    )
  except LookupError as exc:
    raise HTTPException(404, str(exc)) from exc
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  return {"work_key": work_key, "field": field, "text": text}


@router.post("/work-claims/finish", dependencies=[Depends(reject_cross_site)])
async def finish_current_work(
  body: AgentWorkClaimFinish,
  background_tasks: BackgroundTasks,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  try:
    finished = finish_work(
      db,
      owner_id=principal.owner.id,
      chat_id=principal.chat_id,
      work_key=body.work_key,
      outcome=body.outcome,
      release=body.release,
    )
  except ValueError as exc:
    raise HTTPException(409, str(exc)) from exc
  # Source outcome was stamped in the finish transaction. The delivery pass
  # only reduces latency; it never makes the result durable.
  background_tasks.add_task(_deliver_finished_claim_dependencies)
  return {
    **finished,
    "dependency_delivery": "scheduled",
  }


@router.get("/room")
def current_network(
  peer_after: str | None = Query(default=None, max_length=64),
  peer_limit: int = Query(default=MAX_PEERS, ge=1, le=MAX_PEERS),
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  try:
    snapshot = agent_network_snapshot(
      db,
      principal.chat_id,
      principal.run_id,
      peer_limit=peer_limit,
      peer_after=peer_after,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  if snapshot is None:
    raise HTTPException(409, "This agent does not have a coordination scope.")
  return snapshot


@router.post("/messages", dependencies=[Depends(reject_cross_site)])
async def send_current_message(
  body: AgentMessageCreate,
  background_tasks: BackgroundTasks,
  principal: Principal = Depends(get_agent_run_principal),
):
  if principal.chat_id is None or principal.run_id is None:
    raise HTTPException(403, "Current chat-bound agent run required.")
  # Scope resolution, validation, the write and the inbox prune run in a
  # worker with its own session.
  try:
    rows = await asyncio.to_thread(
      send_agent_message,
      owner_id=principal.owner.id,
      sender_chat_id=principal.chat_id,
      sender_run_id=principal.run_id,
      recipients=body.recipients,
      broadcast=body.broadcast,
      kind=body.kind,
      delivery=body.delivery,
      body=body.body,
      send_id=body.send_id,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  if rows is None:
    raise HTTPException(409, "This agent does not have a coordination scope.")
  # Meaning and delivery are independent. A direct interrupt steers a live
  # recipient or wakes its idle unfinished Goal; next_turn and every broadcast
  # stay quiet so one broad note cannot manufacture a wave of model work.
  # The note is stored now and every recipient will read it. An interrupt is
  # attempted for every recipient after this response, one by one, so the
  # send returns at once however many it reaches; whether it lands is not
  # known here (owner input, a held queue or no unfinished Goal can defer it
  # to the recipient's next turn). An exact retry stores nothing new and
  # attempts the interrupt again: a steer carries only undelivered notes and
  # a wake needs an undelivered interrupt, so nothing arrives twice.
  retry = bool(rows and rows[0].get("retry"))
  recipients = [] if body.broadcast else list(dict.fromkeys(body.recipients))
  interrupt = body.delivery == DELIVERY_INTERRUPT
  reachable, reasons = await asyncio.to_thread(
    classify_undelivered, recipients, interrupt=interrupt,
  )
  if interrupt and recipients:
    background_tasks.add_task(
      interrupt_recipients, recipients=recipients, kind=body.kind,
      sender_chat_id=principal.chat_id,
    )
  # One row is stored per recipient inbox. The sender already knows the note,
  # so return one canonical row, its body under the same excerpt rule as turn
  # context, plus the exact recipient count and names the owner-visible
  # activity card needs, not the body repeated per recipient. An excerpted
  # body is marked as the owner views mark it (truncated, body_chars), and
  # send_id lets the owner's timeline join the card to the stored rows.
  receipt = [context_message(row) for row in rows[:1]]
  for shown, stored in zip(receipt, rows):
    shown["send_id"] = stored["send_id"]
    if shown.get("body") != stored.get("body"):
      shown.update(truncated=True, body_chars=len(stored["body"]))
  result = {
    "messages": receipt,
    "recipient_count": len(rows),
    "recipient_names": [
      name for name in dict.fromkeys(row["recipient_name"] for row in rows)
      if name
    ],
    # Requested, not confirmed: the interrupt is attempted after this reply.
    "interrupt_requested": reachable if interrupt else [],
    "queued": [] if interrupt else reachable,
    "unreachable": [chat_id for chat_id in recipients if chat_id in reasons],
  }
  if retry:
    result["retry"] = (
      "This send was already stored; nothing was stored again"
      + (", and its interrupt was attempted again where still undelivered."
         if interrupt else ".")
    )
  if result["unreachable"]:
    # Recorded facts, never a timeout: the sender learns that nothing will
    # read this note soon instead of streaming more notes into a dead chat.
    result["unreachable_reasons"] = {
      chat_id: reasons[chat_id] for chat_id in result["unreachable"]
    }
    result["next_action"] = (
      "The note is stored, but no turn will read it soon at the unreachable "
      "recipients. Do not resend, split, or re-interrupt them; pick a live "
      "recipient or tell the owner."
    )
  return result


# One page of a note, with room in a control-tool result for its footer.
NOTE_PAGE_BYTES = READ_PAGE_BYTES - 400


@router.get("/messages/{message_id}")
def read_current_message(
  message_id: str,
  offset: int = Query(default=0, ge=0),
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """One page of a note this agent's chat sent, received or can see
  broadcast, from a UTF-8 byte offset; next_offset names the next page."""
  if principal.chat_id is None or principal.run_id is None:
    raise HTTPException(403, "Current chat-bound agent run required.")
  require_active_chat_access(db, principal.chat_id, principal)
  try:
    message = peer_message_page_for_agent(
      db, principal.chat_id, message_id, offset=offset, page_bytes=NOTE_PAGE_BYTES,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  if message is None:
    raise HTTPException(404, "Peer note not found for this agent.")
  return message


@router.get("/chats/{chat_id}")
def inspect_chat_room(
  chat_id: str,
  limit: int = Query(default=50, ge=1, le=100),
  peer_after: str | None = Query(default=None, max_length=64),
  peer_limit: int = Query(default=100, ge=1, le=200),
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Observe coordination without granting a browser token agent identity."""
  require_chat_embed_operation(principal, "chat:read")
  get_active_chat_for_principal(db, chat_id, principal)
  try:
    if principal.scope == "owner" and principal.app_id is None:
      snapshot = owner_chat_snapshot(
        db,
        chat_id,
        message_limit=limit,
        peer_limit=peer_limit,
        peer_after=peer_after,
      )
    else:
      # An exact-chat embed sees its visible notes but no unrelated peer roster,
      # goals, helper topology, or sibling-only conversation.
      snapshot = embedded_chat_snapshot(db, chat_id, message_limit=limit)
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  if snapshot is None:
    raise HTTPException(404, "Coordination scope not found.")
  return snapshot


@router.get("/projects/{project_id}")
def inspect_project_room(
  project_id: str,
  limit: int = Query(default=50, ge=1, le=100),
  peer_after: str | None = Query(default=None, max_length=64),
  peer_limit: int = Query(default=100, ge=1, le=200),
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = db.query(models.Project.id).filter(
    models.Project.id == project_id,
    models.Project.deleted_at.is_(None),
  ).first()
  if project is None:
    raise HTTPException(404, "Project not found.")
  try:
    return owner_project_snapshot(
      db,
      project_id,
      message_limit=limit,
      peer_limit=peer_limit,
      peer_after=peer_after,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc


@router.get("/chats/{chat_id}/messages/{message_id}")
def inspect_chat_message(
  chat_id: str,
  message_id: str,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """One whole note, for a view that showed its head marked truncated.

  The same exact-chat visibility as this chat's history.
  """
  require_chat_embed_operation(principal, "chat:read")
  require_active_chat_access(db, chat_id, principal)
  message = peer_message_for_chat(db, chat_id, message_id)
  if message is None:
    raise HTTPException(404, "Peer note not found for this chat.")
  return message


@router.get("/chats/{chat_id}/history")
def inspect_chat_history(
  chat_id: str,
  before: str | None = Query(default=None, max_length=64),
  limit: int = Query(default=50, ge=1, le=100),
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Read only the mail available to this exact chat, across retained history."""
  require_chat_embed_operation(principal, "chat:read")
  get_active_chat_for_principal(db, chat_id, principal)
  try:
    return chat_message_history(db, chat_id, before=before, limit=limit)
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
