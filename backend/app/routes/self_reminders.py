"""Routes for agent self-scheduling (relational check-ins).

The agent enqueues a future check-in tied to a chat; a cron dispatcher
fires the due ones by resuming that chat with the agent's note as
context. This is distinct from app-scoped cron (init-cron-scaffold.sh):
cron runs a mini-app's job script, while a self-reminder posts a hidden
message back into a chat to wake the agent in that session.

Endpoints (all owner-or-service-token, never app-scoped):
  POST   /api/self-reminders            enqueue {chat_id, note, due_*}
  GET    /api/self-reminders[?chat_id=] list pending
  DELETE /api/self-reminders/{id}       cancel one
  POST   /api/self-reminders/dispatch   fire due ones (cron-only, gated)

Auth model matches admin.py: the service token at
/data/service-token.txt and the owner JWT are the same shape, so both
pass `get_current_owner` (which rejects app-scoped tokens). The agent
calls the enqueue/list/cancel endpoints with its own token; the cron
dispatcher calls /dispatch with the service token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models, schemas, self_reminders
from app.database import get_db
from app.deps import Principal, get_current_owner, reject_cross_site
from app.resource_access import get_active_chat_or_404

router = APIRouter(prefix="/api/self-reminders", tags=["self-reminders"])

log = logging.getLogger(__name__)


class EnqueueReminder(BaseModel):
  """Body for POST /api/self-reminders.

  Exactly one of `due_in_seconds` (relative — the natural form for the
  agent) or `due_at` (absolute unix seconds) must be set; the helper
  rejects both/neither. `note` is the context the agent wants its
  future self to see when the check-in fires.
  """
  chat_id: str
  note: str
  due_in_seconds: int | None = None
  due_at: int | None = None


def _reminder_out(record: dict) -> dict:
  """Projects a stored record to the agent-facing response shape.

  Drops nothing today (the record is already the agent's own data), but
  routing through one projection keeps the wire shape stable if the
  on-disk record grows internal fields later.
  """
  return {
    "id": record["id"],
    "chat_id": record["chat_id"],
    "due_at": record["due_at"],
    "note": record["note"],
    "created_at": record["created_at"],
    "status": record["status"],
  }


@router.post("", status_code=201)
def create_reminder(
  body: EnqueueReminder,
  _owner: models.Owner = Depends(get_current_owner),
  _csrf: None = Depends(reject_cross_site),
  db: Session = Depends(get_db),
):
  """Enqueues a future check-in for `chat_id`; returns the stored record.

  Validates that the chat exists and is active so the agent can't
  schedule a check-in into a deleted chat the dispatcher would never be
  able to resume. Caps, horizon, and input validation live in the helper
  and surface as 400 via ReminderError.
  """
  get_active_chat_or_404(db, body.chat_id)
  try:
    record = self_reminders.enqueue(
      body.chat_id,
      body.note,
      due_at=body.due_at,
      due_in_seconds=body.due_in_seconds,
    )
  except self_reminders.ReminderError as exc:
    raise HTTPException(status_code=400, detail=str(exc))
  except OSError as exc:
    log.error("self-reminder enqueue write failed: %s", exc)
    raise HTTPException(
      status_code=500,
      detail="Could not save the reminder; please try again.",
    )
  return _reminder_out(record)


@router.get("")
def list_reminders(
  chat_id: str | None = Query(None, description="Filter to one chat"),
  _owner: models.Owner = Depends(get_current_owner),
):
  """Lists pending reminders, oldest-due first, optionally for one chat.

  The agent reads this to see what it has outstanding (and to pick an id
  to cancel when the cap is full).
  """
  return [_reminder_out(r) for r in self_reminders.list_pending(chat_id)]


@router.delete("/{reminder_id}", status_code=200)
def cancel_reminder(
  reminder_id: str,
  _owner: models.Owner = Depends(get_current_owner),
  _csrf: None = Depends(reject_cross_site),
):
  """Cancels one pending reminder so it never fires.

  404 for an unknown id; 409 if it's already terminal (done/cancelled) —
  cancelling a fired reminder is a no-op the caller should know about.
  """
  try:
    record = self_reminders.cancel(reminder_id)
  except self_reminders.ReminderError as exc:
    msg = str(exc)
    status = 404 if "no reminder" in msg else 409
    raise HTTPException(status_code=status, detail=msg)
  return _reminder_out(record)


async def _send_checkin(*, body, chat_id, principal, db):
  """Resume a chat with a reminder's hidden note via the normal send path.

  A thin module-level seam over `chats_stream.send_message`: the import stays
  lazy (no startup import cycle between the two route modules), and a function
  this module owns is the stable thing the dispatch test patches — robust even
  if a sibling test desyncs cross-module import identity, which patching
  `chats_stream.send_message` by string would be sensitive to.
  """
  from app.routes.chats_stream import send_message

  return await send_message(
    body=body, chat_id=chat_id, principal=principal, db=db,
  )


@router.post("/dispatch")
async def dispatch_due(
  _owner: models.Owner = Depends(get_current_owner),
  _csrf: None = Depends(reject_cross_site),
  db: Session = Depends(get_db),
):
  """Fires every due reminder by resuming its chat, then marks it done.

  Called only by the cron dispatcher with the service token. Gated on
  the owner opt-in sentinel: when dispatch is disabled this returns a
  no-op verdict and posts nothing, so the plumbing ships inert until the
  owner enables it.

  For each due reminder it posts a hidden user message into the chat —
  reusing the same send path the UI uses — so the agent wakes in that
  session with the note as context. A reminder whose chat no longer
  exists (deleted since it was scheduled) is cancelled rather than
  retried forever. mark_done runs only after a successful post, so a
  transient failure leaves the reminder pending for the next tick.
  """
  if not self_reminders.is_dispatcher_enabled():
    return {"enabled": False, "fired": 0, "reminders": []}

  owner = db.query(models.Owner).first()
  if owner is None:
    # No owner means setup never ran — there is no session to resume
    # into. Should be unreachable (the service token is minted at
    # setup) but fail loud rather than build a Principal(owner=None).
    raise HTTPException(status_code=503, detail="No owner configured.")

  due = self_reminders.list_due()
  fired: list[dict] = []
  for rec in due:
    chat_id = rec["chat_id"]
    chat = db.query(models.Chat).filter(
      models.Chat.id == chat_id,
      models.Chat.deleted_at.is_(None),
    ).first()
    if chat is None:
      # The chat was deleted after the reminder was scheduled. There is
      # nothing to resume, so retire the reminder instead of letting the
      # dispatcher trip over it on every tick.
      self_reminders.cancel(rec["id"])
      continue
    principal = Principal(owner=owner, app_id=None)
    body = schemas.SendMessage(
      content=_checkin_prompt(rec["note"]),
      hidden=True,
    )
    try:
      # send_message reuses the queue/turn-start machinery: if a turn is
      # already running it queues, otherwise it starts a fresh turn. The
      # hidden flag keeps the wake-up prompt out of the visible transcript
      # while still feeding the agent the note.
      await _send_checkin(
        body=body, chat_id=chat_id, principal=principal, db=db,
      )
    except Exception as exc:
      # Leave the reminder pending so the next tick retries; a wedged
      # chat shouldn't strand every other due reminder either, so we log
      # and move on rather than aborting the whole dispatch.
      log.warning(
        "self-reminder dispatch failed for chat %s (id %s): %s",
        chat_id, rec["id"], exc,
      )
      continue
    self_reminders.mark_done(rec["id"])
    fired.append(_reminder_out(rec))

  return {"enabled": True, "fired": len(fired), "reminders": fired}


def _checkin_prompt(note: str) -> str:
  """Wraps the agent's note in a short framing so the resumed turn reads
  as a self-scheduled check-in, not a user message.

  The agent set this note earlier in the same chat; restating that
  framing on wake keeps it from mistaking the hidden prompt for fresh
  user input.
  """
  return (
    "[Scheduled self check-in — you asked to revisit this now.]\n"
    f"{note}"
  )
