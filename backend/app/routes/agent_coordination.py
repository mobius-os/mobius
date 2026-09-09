"""Run-bound discovery and mailbox API for the provider-neutral peer network."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.agent_coordination import (
  DELIVERY_INTERRUPT,
  MAX_PEERS,
  MESSAGE_DELIVERIES,
  chat_message_history,
  deliver_peer_recipients,
  MESSAGE_KINDS,
  agent_network_snapshot,
  embedded_chat_snapshot,
  model_message,
  owner_chat_snapshot,
  owner_project_snapshot,
  scope_for_chat,
  send_agent_message,
  send_work_claim_notice,
  visible_peer_messages,
)
from app.agent_work_claims import acknowledge_notice, claim_work, finish_work
from app.database import get_db
from app.deps import (
  Principal,
  get_agent_run_principal,
  get_current_owner,
  get_owner_or_chat_embed_principal,
  reject_cross_site,
  require_chat_embed_operation,
)
from app.resource_access import get_active_chat_for_principal
from app import models


router = APIRouter(prefix="/api/agent-coordination", tags=["agent-coordination"])


class AgentMessageCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  recipients: list[str] = Field(default_factory=list, max_length=24)
  broadcast: bool = False
  kind: str = "note"
  delivery: str = "next_turn"
  body: str = Field(min_length=1, max_length=4000)
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
  summary: str = Field(min_length=1, max_length=500)
  takeover_reason: str | None = Field(default=None, min_length=10, max_length=1000)
  expected_owner_chat_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentWorkClaimFinish(BaseModel):
  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

  work_key: str = Field(min_length=3, max_length=256)
  outcome: str = Field(min_length=1, max_length=1000)
  release: bool = False


def _agent_scope(
  db: Session, principal: Principal,
):
  if principal.chat_id is None or principal.run_id is None:
    raise HTTPException(403, "Current chat-bound agent run required.")
  scope = scope_for_chat(db, principal.chat_id, principal.run_id)
  if scope is None:
    raise HTTPException(409, "This agent does not have a coordination scope.")
  return scope


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
  previous = result.get("previous_owner_chat_id")
  if previous and result.get("notification_pending"):
    send_work_claim_notice(
      db,
      owner_id=principal.owner.id,
      claim_id=result["id"],
      revision=result["revision"],
      sender_chat_id=principal.chat_id,
      recipients=[previous],
      body=(
        f"Work claim {result['work_key']} transferred to {principal.chat_id}: "
        f"{result.get('takeover_reason') or 'ownership changed'}"
      ),
    )
    delivery = await deliver_peer_recipients(
      recipients=[previous], delivery=DELIVERY_INTERRUPT, kind="handoff",
      sender_chat_id=principal.chat_id,
    )
    # Persist the retry latch only after the durable notice has also reached
    # its requested live/wake/queue delivery path. A crash or delivery error
    # before this point leaves notification_pending true, so the exact same
    # transfer request retries idempotently instead of losing the wake.
    acknowledge_notice(
      db, claim_id=result["id"], revision=result["revision"],
      resolve_interests=False,
    )
    result["notification_pending"] = False
    result["steered"] = delivery.steered
    result["woken"] = delivery.woken
    result["queued"] = delivery.queued
  return result


@router.post("/work-claims/finish", dependencies=[Depends(reject_cross_site)])
async def finish_current_work(
  body: AgentWorkClaimFinish,
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
  recipients = finished.interested_chat_ids
  delivery = None
  woken: list[str] = []
  if recipients:
    send_work_claim_notice(
      db,
      owner_id=principal.owner.id,
      claim_id=finished.claim["id"],
      revision=finished.claim["revision"],
      sender_chat_id=principal.chat_id,
      recipients=recipients,
      body=(
        f"Work claim {finished.claim['work_key']} was "
        f"{finished.claim['state']}: {finished.claim.get('outcome') or ''}"
      ),
    )
    delivery = await deliver_peer_recipients(
      recipients=recipients, delivery=DELIVERY_INTERRUPT, kind="handoff",
      sender_chat_id=principal.chat_id,
    )
    woken = delivery.woken
  acknowledge_notice(
    db, claim_id=finished.claim["id"], revision=finished.claim["revision"],
    resolve_interests=True,
  )
  return {
    **finished.claim, "notification_pending": False,
    "notified": recipients, "steered": delivery.steered if recipients else [],
    "woken": woken, "queued": delivery.queued if recipients else [],
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
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  scope = _agent_scope(db, principal)
  try:
    rows = send_agent_message(
      db,
      scope,
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
  # Meaning and delivery are independent. A direct interrupt steers a live
  # recipient or wakes its idle unfinished Goal; next_turn and every broadcast
  # stay quiet so one broad note cannot manufacture a wave of model work.
  delivery = (
    await deliver_peer_recipients(
      recipients=body.recipients, delivery=body.delivery, kind=body.kind,
      sender_chat_id=principal.chat_id,
    )
    if not body.broadcast else None
  )
  # One row is stored per recipient inbox. The sender already knows the note,
  # so return one canonical row plus the exact recipient count and names the
  # owner-visible activity card needs, not the body repeated up to 24 times.
  return {
    "messages": [model_message(row) for row in rows[:1]],
    "recipient_count": len(rows),
    "recipient_names": [
      name for name in dict.fromkeys(row["recipient_name"] for row in rows)
      if name
    ],
    "steered": delivery.steered if delivery else [],
    "woken": delivery.woken if delivery else [],
    "queued": delivery.queued if delivery else [],
  }


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
