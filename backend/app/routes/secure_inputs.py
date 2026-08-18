"""Trusted transient-value inputs for sealed local execution."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import models, secure_inputs
from app.broadcast import get_broadcast
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site


router = APIRouter(prefix="/api/secure-inputs", tags=["secure-inputs"])


async def _json_object(request: Request) -> dict[str, Any]:
  """Parse a secret-bearing body without validation errors echoing its input."""
  try:
    payload = await request.json()
  except Exception as exc:
    raise HTTPException(400, detail="Invalid secure input submission.") from exc
  if not isinstance(payload, dict):
    raise HTTPException(400, detail="Invalid secure input submission.")
  return payload


def _active_owner_chat(db: Session, chat_id: str) -> models.Chat:
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None:
    raise HTTPException(404, detail="Chat not found.")
  if chat.created_by_app_id is not None:
    raise HTTPException(
      403, detail="Secure input is available only in owner chats.",
    )
  return chat


def _authorized_request(request_id: str, capability: Any):
  pending = secure_inputs.authorize(request_id, capability)
  if pending is None:
    raise HTTPException(404, detail="Secure input request not found.")
  return pending


@router.post("/{chat_id}", dependencies=[Depends(reject_cross_site)])
async def create_secure_input(
  chat_id: str,
  request: Request,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Create a bounded card for a running owner chat."""
  _active_owner_chat(db, chat_id)
  bc = get_broadcast(chat_id)
  if bc is None or not bc.running:
    raise HTTPException(409, detail="This chat is not running.")

  payload = await _json_object(request)
  try:
    title, description, mode, fields = secure_inputs.validate_request_spec(
      title=payload.get("title"),
      description=payload.get("description", ""),
      mode=payload.get("mode", "sealed"),
      fields=payload.get("fields"),
    )
  except ValueError as exc:
    payload.clear()
    raise HTTPException(400, detail=str(exc)) from exc
  payload.clear()
  try:
    pending, capability = secure_inputs.create_request(
      chat_id=chat_id,
      mode=mode,
      title=title,
      description=description,
      fields=fields,
    )
    secure_inputs.publish_request(pending)
  except ValueError as exc:
    raise HTTPException(409, detail=str(exc)) from exc
  except RuntimeError as exc:
    raise HTTPException(503, detail=str(exc)) from exc

  return {
    "request_id": pending.request_id,
    "capability": capability,
  }


@router.post(
  "/{chat_id}/{request_id}/submit",
  dependencies=[Depends(reject_cross_site)],
)
async def submit_secure_input(
  chat_id: str,
  request_id: str,
  request: Request,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Move submitted fields into process memory without logging or persistence."""
  _active_owner_chat(db, chat_id)
  pending = secure_inputs.get_request(request_id)
  if pending is None or pending.chat_id != chat_id:
    raise HTTPException(404, detail="Secure input request not found.")
  if pending.status != "pending":
    raise HTTPException(409, detail="Secure input request is no longer open.")

  payload = await _json_object(request)
  values_payload = payload.pop("fields", None)
  reveal_confirmed = payload.pop("reveal_confirmed", False) is True
  payload.clear()
  if pending.mode == "reveal" and not reveal_confirmed:
    if isinstance(values_payload, dict):
      values_payload.clear()
    raise HTTPException(
      400,
      detail="Confirm that these values may be sent to the AI provider.",
    )
  try:
    values = secure_inputs.validate_submitted_values(pending, values_payload)
  except ValueError as exc:
    if isinstance(values_payload, dict):
      values_payload.clear()
    raise HTTPException(400, detail=str(exc)) from exc
  if isinstance(values_payload, dict):
    values_payload.clear()
  secure_inputs.fill_request(pending, values)
  return {"status": "filled"}


@router.post("/{request_id}/wait")
async def wait_for_secure_input(request_id: str, request: Request):
  """Long-poll state with a one-way capability; returns no submitted values."""
  payload = await _json_object(request)
  capability = payload.pop("capability", None)
  payload.clear()
  pending = _authorized_request(request_id, capability)
  if pending.status == "pending":
    try:
      await asyncio.wait_for(pending.event.wait(), timeout=25)
    except TimeoutError:
      pass
  return {
    "status": pending.status,
    "result": pending.result if pending.status not in {"pending", "filled"} else None,
  }


@router.post("/{request_id}/consume")
async def consume_secure_input(request_id: str, request: Request):
  """Return values exactly once to the local helper holding the capability."""
  payload = await _json_object(request)
  capability = payload.pop("capability", None)
  payload.clear()
  pending = _authorized_request(request_id, capability)
  try:
    values = secure_inputs.consume_request(pending)
  except ValueError as exc:
    raise HTTPException(409, detail=str(exc)) from exc
  # Deliberate narrow reveal: this response is capability-authenticated, read
  # only by the local helper, and never logged. The registry already dropped
  # its reference; the helper clears its decoded mapping after use.
  return {"fields": values, "mode": pending.mode}


@router.post("/{request_id}/settle")
async def settle_secure_input(request_id: str, request: Request):
  """Record only a bounded non-secret consumer outcome."""
  payload = await _json_object(request)
  capability = payload.pop("capability", None)
  ok = payload.pop("ok", False) is True
  message = payload.pop("message", "Secure input complete.")
  payload.clear()
  pending = _authorized_request(request_id, capability)
  if pending.status != "consuming":
    raise HTTPException(409, detail="Secure input is not being consumed.")
  secure_inputs.settle_request(pending, ok=ok, message=message)
  return {"status": pending.status}


@router.post("/{request_id}/cancel")
async def cancel_secure_input(request_id: str, request: Request):
  """Cancel using the one-way capability when the local helper exits."""
  payload = await _json_object(request)
  capability = payload.pop("capability", None)
  payload.clear()
  pending = _authorized_request(request_id, capability)
  secure_inputs.cancel_request(pending)
  return {"status": pending.status}
