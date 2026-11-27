"""Agent edits to its chat's owner-input request list.

Editing the list never ends the turn and never shows the owner anything by
itself: publication belongs to the turn's end. Every response is the complete
open list, so the agent never relies on remembering what it asked.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import models, owner_input_requests as requests
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, refuse_app_agent, reject_cross_site
from app.agent_work_claims import claim_work
from app.resource_access import get_active_chat_for_principal

router = APIRouter(prefix="/api/chats", tags=["owner-input-requests"])


class RequestEdit(BaseModel):
  model_config = ConfigDict(extra="forbid")

  op: Literal["add", "update", "withdraw", "resolve", "list"]
  kind: Literal["question", "approval", "restart", "secret"] | None = None
  key: str | None = Field(default=None, max_length=80)
  id: str | None = Field(default=None, max_length=64)
  header: str | None = None
  prompt: str | None = None
  context: str | None = None
  options: list[dict] | None = None
  multiple: bool | None = None
  # approval: the exact action it authorises, claimed when the request is added
  work_key: str | None = None
  # secret: the sealed card's prompts and its pre-authored local consumer
  title: str | None = None
  description: str | None = None
  fields: list[dict] | None = None
  command: list[str] | None = None
  cwd: str | None = None
  preset: Literal["owner_credentials"] | None = None
  blocks: dict | None = None
  reason: str | None = None
  answer: str | None = None
  source: str | None = None

  def spec(self) -> dict:
    return self.model_dump(
      include={
        "kind", "header", "prompt", "context", "options", "multiple", "work_key",
        "title", "description", "fields", "command", "cwd", "preset",
      },
      exclude_none=True,
    )


@router.post("/{chat_id}/input-requests", dependencies=[Depends(reject_cross_site)])
def edit_input_requests(
  chat_id: str,
  body: RequestEdit,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  spec = _confined_spec(principal, chat_id, body.spec())
  chat = get_active_chat_for_principal(db, chat_id, principal, load_fields=())
  run = db.get(models.ChatRun, principal.run_id)
  if run is None or run.status != "running":
    raise HTTPException(status_code=409, detail="This agent turn is no longer current.")
  ref = body.id or body.key
  try:
    if body.op == "add":
      follower = _add(db, chat, run, principal, body, spec)
      if follower is not None:
        return follower
    elif body.op == "update":
      if not ref:
        raise requests.RequestError("update needs the request's id or key")
      row = requests.find_open(db, chat_id, ref)
      # An update states only what changes; unstated content is kept.
      merged = {**requests.editable_spec(row.spec_json), **spec} if spec else None
      requests.update_request(
        db, chat_id=chat_id, ref=ref, spec=merged, blocks=body.blocks,
      )
    elif body.op == "resolve":
      if not ref:
        raise requests.RequestError("resolve needs the request's id or key")
      requests.resolve_request(
        db, chat_id=chat_id, ref=ref, answer=body.answer or "",
        source=body.source or "", reason=body.reason or "",
      )
    elif body.op == "withdraw":
      if not ref:
        raise requests.RequestError("withdraw needs the request's id or key")
      requests.withdraw_request(
        db, chat_id=chat_id, ref=ref, reason=body.reason or "",
      )
  except requests.RequestError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc
  return {**requests.open_snapshot(db, chat_id), "next_action": _NEXT_ACTION}


_NEXT_ACTION = (
  "Nothing here is answered and the owner has not been interrupted. Keep "
  "doing the work that does not depend on these answers; do not act on "
  "what a request blocks. When nothing else can proceed, end your turn with "
  "your closing message, saying where things stand and what each open "
  "request decides: the open requests are then shown after it, all "
  "together on one card that never holds this chat; the owner settles "
  "each one separately."
)


def _confined_spec(principal: Principal, chat_id: str, spec: dict) -> dict:
  """A confined app agent asks questions and approvals only.

  Its approval's claim stays inside its own app and chat, so it can never
  reserve owner work that another chat is about to do. Secrets and restarts
  are refused here as well as by its key's route allow-list.
  """
  if principal.app_agent is None:
    return spec
  if spec.get("kind") == requests.SECRET:
    refuse_app_agent(principal, "ask for a password or key")
  if spec.get("kind") == requests.RESTART:
    refuse_app_agent(principal, "ask to restart Möbius")
  if isinstance(spec.get("work_key"), str):
    spec = {
      **spec,
      "work_key": f"app:{principal.app_agent.app_id}:chat:{chat_id}:{spec['work_key']}",
    }
  return spec


def _add(db: Session, chat: models.Chat, run: models.ChatRun, principal: Principal,
         body: RequestEdit, raw_spec: dict) -> dict | None:
  """Record one request; for a new approval, claim its exact action first.

  Everything that could refuse the request is checked before the claim, so a
  refused request never takes ownership of an action. A chat that loses the
  claim to a peer (or finds the action completed) records nothing and gets
  the follower result instead.
  """
  key = requests.require_valid_key(body.key)
  spec = requests.normalize_spec(raw_spec)
  kind = requests.request_kind(spec)
  if kind == requests.SECRET and chat.created_by_app_id is not None:
    raise requests.RequestError("secure input is available only in owner chats")
  if kind == requests.RESTART:
    from app import restart_ledger
    if not restart_ledger.current_boot_id():
      raise HTTPException(status_code=409, detail={
        "code": "platform_source_is_not_loaded",
        "message": "Möbius is not running a restart-loadable platform boot.",
      })
  if kind == requests.APPROVAL and requests.open_request_by_key(db, chat.id, key) is None:
    requests.require_can_add(db, chat.id, spec)
    try:
      claim = claim_work(
        db, owner_id=principal.owner.id, chat_id=chat.id, run_id=run.id,
        work_key=spec["work_key"], summary=spec["prompt"][:500],
      )
    except ValueError as exc:
      raise HTTPException(409, str(exc)) from exc
    if claim["state"] == "completed" or claim["owner_chat_id"] != chat.id:
      return {**_approval_follower_result(claim), **requests.open_snapshot(db, chat.id)}
  requests.add_request(
    db, chat_id=chat.id, run_id=run.id, goal_id=run.goal_id,
    key=key, spec=raw_spec, blocks=body.blocks,
  )
  return None


def _approval_follower_result(claim: dict) -> dict:
  """No request: another chat owns this exact action, or it already completed."""
  if claim["state"] == "completed":
    claim["next_action"] = (
      "No approval request was recorded and your turn continues. This exact "
      "action already completed; do not repeat it. Reconcile your Goal with "
      "the recorded outcome."
    )
  else:
    claim["next_action"] = (
      "No approval request was recorded and your turn continues. "
      f"{claim['owner_name']} owns this exact action and its approval. Do "
      "not request approval for or perform it yourself. Continue independent "
      "work and settle the overlapping plan task; your Goal stays yours."
    )
    if claim.get("follow_status") in ("followed", "result_owed"):
      claim["next_action"] += (
        " Your open Goal follows this acquisition and will receive its result."
      )
    else:
      claim["next_action"] += (
        " No Goal follow was created ("
        + str(claim.get("follow_status") or "no_open_top_level_goal")
        + "); tell your coordinator if this work belongs to its Goal."
      )
  return claim
