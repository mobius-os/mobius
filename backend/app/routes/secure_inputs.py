"""Trusted transient-value inputs for sealed local execution."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import models, secure_inputs
from app.broadcast import get_broadcast
from app.database import get_db
from app.deps import (
  Principal,
  get_agent_run_principal,
  get_current_owner,
  get_current_owner_for_lifecycle_control,
  get_current_owner_for_owner_input,
  reject_cross_site,
)


router = APIRouter(prefix="/api/secure-inputs", tags=["secure-inputs"])


@router.post("/{chat_id}/saved", dependencies=[Depends(reject_cross_site)])
async def create_saved_secure_input(
  chat_id: str, request: Request,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Save a sealed prompt and pre-authored consumer; acknowledge, never wait."""
  from app.saved_secure_inputs import validate_consumer_spec
  from app.routes.owner_approvals import save_owner_question

  payload = await _json_object(request)
  try:
    title, description, mode, fields = secure_inputs.validate_request_spec(
      title=payload.get("title"), description=payload.get("description", ""),
      mode=payload.get("mode", "sealed"), fields=payload.get("fields"),
    )
    if mode != "sealed":
      raise ValueError("Revealing values requires the explicit live reveal flow.")
    spec = validate_consumer_spec(payload)
  except ValueError as exc:
    raise HTTPException(400, detail=str(exc)) from exc
  finally:
    payload.clear()
  card = {
    "questions": [{"id": "secure_input", "header": "Secure input", "question": title, "options": []}],
    "secure_input": {"title": title, "description": description, "mode": mode, "fields": fields},
  }
  receipt = await save_owner_question(
    chat_id, card, principal, db, secure_request=spec,
    identity_payload={"card": card, "consumer": spec},
  )
  return {**receipt, "request_id": receipt["question_id"]}


def _saved_request(db: Session, chat_id: str, request_id: str):
  row = db.get(models.SavedSecureInput, request_id)
  return row if row is not None and row.chat_id == chat_id else None


@router.get("/{chat_id}/{request_id}/saved-state")
async def saved_secure_input_state(
  chat_id: str, request_id: str,
  _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db),
):
  chat = _active_owner_chat(db, chat_id)
  row = _saved_request(db, chat_id, request_id)
  if row is None:
    raise HTTPException(404, detail="Secure input request not found.")
  if row.status in {"pending", "consuming"} and chat.pending_question_id != request_id:
    return {"status": "cancelled", "outcome": "Secure input was cancelled."}
  return {"status": row.status, "outcome": row.outcome}


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
  _: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Create a bounded card for a running owner chat."""
  from app import chat_queue
  async with chat_queue.get_transition_lock(chat_id), chat_queue.get_lock(chat_id):
    db.expire_all()
    chat = _active_owner_chat(db, chat_id)
    if chat.pending_question_id is not None:
      raise HTTPException(409, detail="Answer the open question before requesting secure input.")
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
  _: models.Owner = Depends(get_current_owner_for_owner_input),
  db: Session = Depends(get_db),
):
  """Move submitted fields into process memory without logging or persistence."""
  chat = _active_owner_chat(db, chat_id)
  saved = _saved_request(db, chat_id, request_id)
  if saved is not None:
    from app import saved_secure_inputs
    from app.chat import current_run_generation
    from types import SimpleNamespace
    generation = current_run_generation(chat_id)
    if chat.pending_question_id != request_id:
      if saved.status in {"completed", "failed", "interrupted"}:
        return {"status": saved.status}
      raise HTTPException(410, detail="Secure input request is no longer open.")
    if saved.status != "pending":
      return {"status": saved.status}
    card = next((block.get("secure_input") for message in reversed(chat.messages or [])
                 for block in message.get("blocks") or []
                 if block.get("question_id") == request_id), None)
    if not isinstance(card, dict):
      raise HTTPException(409, detail="Secure input prompt is unavailable.")
    payload = await _json_object(request)
    values_payload = payload.pop("fields", None)
    payload.clear()
    try:
      values = secure_inputs.validate_submitted_values(SimpleNamespace(fields=card["fields"]), values_payload)
    except ValueError as exc:
      raise HTTPException(400, detail=str(exc)) from exc
    finally:
      if isinstance(values_payload, dict):
        values_payload.clear()
    try:
      result = await saved_secure_inputs.submit(chat_id, request_id, values, generation)
    except Exception as exc:
      values.clear()
      raise HTTPException(503, detail="Could not accept secure input; its execution will not be repeated automatically.") from exc
    if result["status"] in {"closed", "cancelled"}:
      values.clear()
      raise HTTPException(410, detail="Secure input request is no longer open.")
    return result
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


@router.post(
  "/{chat_id}/{request_id}/cancel",
  dependencies=[Depends(reject_cross_site)],
)
async def cancel_secure_input_by_owner(
  chat_id: str,
  request_id: str,
  _: models.Owner = Depends(get_current_owner_for_owner_input),
  db: Session = Depends(get_db),
):
  """Let the owner dismiss their own open card without the helper's capability.

  The capability-based cancel is for the local helper on exit; if that process
  dies the card would otherwise sit in the chat's one-per-chat slot with no way
  for the owner to clear it. Owner authentication plus chat ownership stands in
  for the capability here.
  """
  chat = _active_owner_chat(db, chat_id)
  saved = _saved_request(db, chat_id, request_id)
  if saved is not None:
    from app import saved_secure_inputs
    from app.chat import current_run_generation
    if chat.pending_question_id != request_id or saved.status != "pending":
      raise HTTPException(409, detail="Secure input request is no longer open.")
    result = await saved_secure_inputs.finish(
      chat_id, request_id, "cancelled", "cancelled", generation=current_run_generation(chat_id),
    )
    return {"status": result["status"]}
  pending = secure_inputs.get_request(request_id)
  if pending is None or pending.chat_id != chat_id:
    raise HTTPException(404, detail="Secure input request not found.")
  if pending.status not in {"pending", "filled"}:
    raise HTTPException(409, detail="Secure input request is no longer open.")
  secure_inputs.cancel_request(pending)
  return {"status": pending.status}


@router.post("/{request_id}/wait")
async def wait_for_secure_input(request_id: str, request: Request):
  """Wait for owner submission or cancellation; return no submitted values."""
  payload = await _json_object(request)
  capability = payload.pop("capability", None)
  payload.clear()
  pending = _authorized_request(request_id, capability)
  if pending.status == "pending":
    # Like a question card, waiting for a person has no deadline. Only an
    # owner action (including Stop) resolves the pending event.
    await pending.event.wait()
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
