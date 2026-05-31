"""Gated, redacted chat-log read API for mini-apps (capability B).

Design §2. A permission-gated, `summary`-only surface that hands an app
a SERVER-SIDE structurally redacted view of the owner's chats. Distinct
from `/api/chats/*`, which stays owner-only and returns raw rows: this
surface is the ONE place an app token may read other chats, and it never
returns the raw transcript.

Gating (design §2):
  - Owner tokens always pass (the permission map governs apps).
  - App tokens need `App.chat_log_access >= 'summary'`, read from the
    App row at request time via `deps.require_app_permission` — flipping
    the column revokes on the next request, no JWT rotation.
  - `full` is reserved but rejected here until a concrete consumer +
    louder consent lands.

Read-only. No mutation endpoints. Every app-initiated read is written to
the activity log (which app, which scope, when) so the owner can audit
who looked at what — closing the B↔C loop in the design.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import activity, chat_log_redaction as redact, models
from app.database import get_db
from app.deps import Principal, get_principal, require_app_permission
from app.resource_access import get_active_chat_or_404

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat-logs", tags=["chat-logs"])

# Page-size ceiling for the list view. Bounds the response and the
# redaction work per request; the caller pages with `cursor`.
_MAX_LIMIT = 100


def _gate_summary(principal: Principal, db: Session) -> None:
  """Require chat_log_access>='summary' for app callers; owner passes.

  `full` is deferred: an app that somehow carries chat_log_access='full'
  is treated as having (at least) summary here — the route never serves
  an un-redacted tier, so 'full' grants nothing extra today. We assert
  the summary floor and stop.
  """
  require_app_permission(principal, "chat_log_access", "summary", db)


def _iso(dt) -> str | None:
  return dt.isoformat() if dt else None


@router.get("")
def list_chat_logs(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
  limit: int = Query(default=20, ge=1, le=_MAX_LIMIT),
  cursor: int = Query(default=0, ge=0),
):
  """Paginated list of chats as redacted summaries.

  Each entry: id, scrubbed title, created_at, updated_at, message_count
  (post-redaction visible count), and a short redacted excerpt. Ordered
  newest-updated first. `cursor` is a 0-based offset into that ordering;
  the response returns `next_cursor` (or null when exhausted).

  Soft-deleted chats are excluded — an app browsing logs should see the
  same active set the owner does, not deleted history.
  """
  _gate_summary(principal, db)

  base = (
    db.query(models.Chat)
    .filter(models.Chat.deleted_at.is_(None))
    .order_by(models.Chat.updated_at.desc())
  )
  rows = base.offset(cursor).limit(limit + 1).all()
  has_more = len(rows) > limit
  rows = rows[:limit]

  items = []
  for c in rows:
    msgs = c.messages or []
    items.append({
      "id": c.id,
      # Title is derived from the first user message → scrub it like
      # any other surviving text (design §2 explicitly calls this out).
      "title": redact.scrub_secrets(c.title or ""),
      "created_at": _iso(c.created_at),
      "updated_at": _iso(c.updated_at),
      "message_count": redact.count_visible_messages(msgs),
      "excerpt": redact.excerpt_for_chat(msgs),
    })

  if principal.app_id is not None:
    activity.log_event(
      "chat_log_read",
      app_id=principal.app_id,
      scope="list",
      count=len(items),
      asserted=False,  # platform-authored audit event, not app-asserted
    )

  next_cursor = cursor + limit if has_more else None
  return {"items": items, "next_cursor": next_cursor}


@router.get("/{chat_id}")
def get_chat_log(
  chat_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """One chat as a redacted summary: whitelisted {role, text} messages.

  Newest-`MAX_MESSAGES_PER_CHAT` slice, each text truncated and
  secret-scrubbed (chat_log_redaction.redact_messages). Tool / thinking /
  question / error blocks, attachments, hidden + pending messages, and
  the fs-path augmentation are all stripped server-side. 404 on a missing
  or soft-deleted chat — same surface the owner sees.
  """
  _gate_summary(principal, db)

  chat = get_active_chat_or_404(db, chat_id)
  messages = redact.redact_messages(chat.messages or [])

  if principal.app_id is not None:
    activity.log_event(
      "chat_log_read",
      app_id=principal.app_id,
      scope="chat",
      chat_id=chat_id,
      count=len(messages),
      asserted=False,
    )

  return {
    "id": chat.id,
    "title": redact.scrub_secrets(chat.title or ""),
    "created_at": _iso(chat.created_at),
    "updated_at": _iso(chat.updated_at),
    "tier": "summary",
    "messages": messages,
  }
