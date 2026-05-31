"""Shared resource-access helpers for route handlers.

Centralizes the `db.query(Chat).filter(Chat.id == ..., Chat.deleted_at
IS NULL).first()` pattern that multiple route files copy. A single
implementation means a future correctness fix (e.g. tightening the
soft-delete check) propagates everywhere instead of needing N edits.

Scope is intentionally narrow — ACTIVE chat reads only. Routes whose
lookup intentionally diverges from the soft-delete filter (the
delete flow at `routes/chats.py:376` queries by id without the
filter because it is actively setting `deleted_at`; the recover
flow at `routes/chats.py:392-395` queries with the INVERSE filter)
stay inline. This module is not the place to capture both behaviors
behind a flag — a flag would just push the special-case detail to
every caller.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.deps import Principal


def get_active_chat_for_principal(
  db: Session, chat_id: str, principal: Principal,
) -> models.Chat:
  """Fetches an active chat the principal may DRIVE, else 404/403.

  The actor gate for the app-attributed chat contract (design §1):
    - Owner tokens may drive ANY active chat (the column is an actor
      tag, not a fence against the owner).
    - An app token may drive ONLY a chat it created — i.e.
      `chat.created_by_app_id == principal.app_id`. Sending to or
      streaming a foreign chat (owner-created or another app's) is 403.

  This is the enforceable boundary that lets an app open and converse in
  its own chat without holding the keys to the owner's whole history.
  Reuse it everywhere an app-driven mutation touches a chat — don't
  re-derive the `created_by_app_id` comparison inline.

  Raises:
    HTTPException: 404 when the chat is missing/soft-deleted (same shape
      the owner sees, so an app can't probe existence of chats it can't
      reach); 403 when an app token targets a chat it doesn't own.
  """
  chat = get_active_chat_or_404(db, chat_id)
  if principal.app_id is None:
    return chat  # owner drives anything
  if chat.created_by_app_id != principal.app_id:
    raise HTTPException(
      status_code=403,
      detail="This chat is not owned by your app.",
    )
  return chat


def get_active_chat_or_404(
  db: Session, chat_id: str,
) -> models.Chat:
  """Fetches a non-soft-deleted Chat by id, raising 404 otherwise.

  Sync (not async) because the underlying SQLAlchemy `Session` is
  sync — there is no I/O await to surface here, and a sync helper
  is callable from both sync and async route handlers (most chat
  routes are sync `def`; a few like `send_message` are `async def`).

  The Chat model has no `owner_id` column (single-owner installation;
  see `models.py:24-50`), so owner-scoping is not this helper's job —
  it happens upstream via `deps.get_current_owner` on the route.

  Args:
    db: SQLAlchemy session.
    chat_id: The chat id (string primary key).

  Returns:
    The matching Chat row.

  Raises:
    HTTPException: 404 when no row matches OR the row is soft-deleted.
  """
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None:
    raise HTTPException(status_code=404, detail="Chat not found.")
  return chat
