"""Run-bound discovery and mailbox API for the provider-neutral peer network."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.agent_coordination import (
  MAX_PEERS,
  MESSAGE_KINDS,
  agent_network_snapshot,
  embedded_chat_snapshot,
  model_message,
  owner_chat_snapshot,
  owner_project_snapshot,
  run_started_at,
  scope_for_chat,
  send_agent_message,
  visible_peer_messages,
  wake_idle_recipients,
)
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


def _agent_scope(
  db: Session, principal: Principal,
):
  if principal.chat_id is None or principal.run_id is None:
    raise HTTPException(403, "Current chat-bound agent run required.")
  scope = scope_for_chat(db, principal.chat_id, principal.run_id)
  if scope is None:
    raise HTTPException(409, "This agent does not have a coordination scope.")
  return scope


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


@router.get("/messages")
def current_messages(
  after: str | None = Query(default=None, max_length=64),
  limit: int = Query(default=50, ge=1, le=100),
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Inbox notes after ``after``, or since this run started when no cursor is
  given; earlier backlog reaches the agent through turn context instead."""
  scope = _agent_scope(db, principal)
  created_after = None
  if after is None:
    created_after = run_started_at(db, principal.chat_id, principal.run_id)
    if created_after is None:
      raise HTTPException(409, "Current agent run is unavailable.")
  try:
    items = visible_peer_messages(
      db, scope,
      chat_id=principal.chat_id,
      after_id=after,
      limit=limit,
      inbox_only=True,
      created_after=created_after,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  return {
    "scope": {"kind": scope.kind, "id": scope.id},
    "messages": [model_message(item) for item in items],
    "cursor": items[-1]["id"] if items else after,
  }


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
      body=body.body,
      send_id=body.send_id,
    )
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc
  # The note is durable either way; a direct ask additionally wakes an idle
  # recipient with unfinished Goal work so the handoff does not wait for the
  # owner to notice. Broadcasts and plain notes never start a turn.
  woken = [] if body.broadcast else await wake_idle_recipients(
    recipients=body.recipients, kind=body.kind,
    sender_chat_id=principal.chat_id,
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
    "woken": woken,
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
