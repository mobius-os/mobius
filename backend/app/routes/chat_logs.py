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
  - Recoverable deleted chats require the explicit
    `summary_with_deleted` tier and an `include_deleted=true` request.
  - Every readable tier serves the same structurally redacted summary view.

Read-only. No mutation endpoints. Every app-initiated read is written to
the activity log (which app, which scope, when) so the owner can audit
who looked at what — closing the B↔C loop in the design.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app import activity, chat_log_redaction as redact, models
from app.chat_transcript import materialized_messages
from app.database import get_db
from app.deps import Principal, get_principal, require_app_permission
from app.resource_access import get_active_chat_or_404
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat-logs", tags=["chat-logs"])

# Page-size ceiling for the list view. Bounds the response and the
# redaction work per request; the caller pages with `cursor`.
_MAX_LIMIT = 100


def _gate_summary(
  principal: Principal,
  db: Session,
  *,
  include_deleted: bool = False,
) -> None:
  """Require the live app permission for the requested lifecycle scope."""
  require_app_permission(principal, "chat_log_access", "summary", db)
  if include_deleted:
    require_app_permission(
      principal, "chat_log_access", "summary_with_deleted", db,
    )


def _iso(dt) -> str | None:
  return dt.isoformat() if dt else None


@router.get("")
def list_chat_logs(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
  limit: int = Query(default=20, ge=1, le=_MAX_LIMIT),
  cursor: int = Query(default=0, ge=0),
  before_recency: datetime | None = Query(default=None),
  before_id: str | None = Query(default=None),
  include_deleted: bool = Query(default=False),
):
  """Paginated list of chats as redacted summaries.

  Each entry: id, scrubbed title, created_at, updated_at, message_count
  (post-redaction visible count), and a short redacted excerpt. Ordered
  newest-activity first. Legacy callers may page with the 0-based `cursor`.
  Durable consumers should instead pass the returned `next_before` pair as
  `before_recency` + `before_id`; that keyset cannot skip a chat merely because
  a newer chat arrived between requests.

  Soft-deleted chats remain excluded by default. A caller with the explicit
  higher tier may request chats still inside their seven-day recovery window.
  """
  _gate_summary(principal, db, include_deleted=include_deleted)

  # Recency means "last real activity", same as the owner's drawer
  # (routes/chats.py). updated_at also moves on non-activity writes —
  # a backfill migration once bumped it for 312 historical chats and
  # scrambled this listing — so activity_at leads and updated_at is
  # only the fallback for rows that predate the column.
  # id breaks timestamp ties so offset pagination is deterministic —
  # equal keys with unspecified order can duplicate or drop rows
  # across pages.
  activity_recency = func.coalesce(
    models.Chat.activity_at, models.Chat.updated_at,
  )
  # Deletion is itself the lifecycle event a durable consumer must discover.
  # Without it, deleting an old already-processed chat would leave it behind
  # the caller's keyset marker forever.
  recency = (
    func.coalesce(models.Chat.deleted_at, activity_recency)
    if include_deleted
    else activity_recency
  )
  if (before_recency is None) != (before_id is None):
    raise HTTPException(
      status_code=422,
      detail="before_recency and before_id must be supplied together",
    )
  if before_recency is not None and cursor:
    raise HTTPException(
      status_code=422,
      detail="cursor cannot be combined with keyset pagination",
    )
  if before_recency is not None and before_recency.tzinfo is not None:
    before_recency = before_recency.astimezone(UTC).replace(tzinfo=None)

  base = db.query(models.Chat)
  if include_deleted:
    cutoff = now_naive_utc() - SOFT_DELETE_TTL
    base = base.filter(or_(
      models.Chat.deleted_at.is_(None),
      models.Chat.deleted_at >= cutoff,
    ))
  else:
    base = base.filter(models.Chat.deleted_at.is_(None))
  if before_recency is not None and before_id is not None:
    base = base.filter(or_(
      recency < before_recency,
      and_(recency == before_recency, models.Chat.id < before_id),
    ))
  base = base.order_by(recency.desc(), models.Chat.id.desc())
  if before_recency is None:
    base = base.offset(cursor)
  rows = base.limit(limit + 1).all()
  has_more = len(rows) > limit
  rows = rows[:limit]

  items = []
  for c in rows:
    msgs = materialized_messages(c)
    items.append({
      "id": c.id,
      # Title is derived from the first user message → scrub it like
      # any other surviving text (design §2 explicitly calls this out).
      "title": redact.scrub_secrets(c.title or ""),
      "created_at": _iso(c.created_at),
      "updated_at": _iso(c.updated_at),
      "recency_at": _iso(
        c.deleted_at if include_deleted and c.deleted_at
        else c.activity_at or c.updated_at
      ),
      "deleted_at": _iso(c.deleted_at),
      "message_count": redact.count_visible_messages(msgs),
      "excerpt": redact.excerpt_for_chat(msgs),
    })

  if principal.app_id is not None:
    activity.log_event(
      "chat_log_read",
      app_id=principal.app_id,
      scope="list",
      count=len(items),
      include_deleted=include_deleted,
      asserted=False,  # platform-authored audit event, not app-asserted
    )

  next_cursor = (
    cursor + limit if has_more and before_recency is None else None
  )
  next_before = None
  if has_more and rows:
    last = rows[-1]
    next_before = {
      # Must be the exact expression used by ORDER BY above. A deletion can
      # make an old chat newest; returning its old activity timestamp here
      # would jump the following request past every row between those dates.
      "recency_at": _iso(
        last.deleted_at if include_deleted and last.deleted_at
        else last.activity_at or last.updated_at
      ),
      "id": last.id,
    }
  return {
    "items": items,
    "next_cursor": next_cursor,
    "next_before": next_before,
  }


@router.get("/{chat_id}")
def get_chat_log(
  chat_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
  include_deleted: bool = Query(default=False),
):
  """One chat as a redacted summary: whitelisted {role, text} messages.

  Newest-`MAX_MESSAGES_PER_CHAT` slice, each text truncated and
  secret-scrubbed (chat_log_redaction.redact_messages). Tool / thinking /
  question / error blocks, attachments, hidden + pending messages, and
  the fs-path augmentation are all stripped server-side. Soft-deleted chats
  require both the higher permission and an explicit request, and remain
  readable only during their recovery window.
  """
  _gate_summary(principal, db, include_deleted=include_deleted)

  if include_deleted:
    cutoff = now_naive_utc() - SOFT_DELETE_TTL
    chat = db.query(models.Chat).filter(
      models.Chat.id == chat_id,
      or_(
        models.Chat.deleted_at.is_(None),
        models.Chat.deleted_at >= cutoff,
      ),
    ).first()
    if chat is None:
      raise HTTPException(status_code=404, detail="Chat not found")
  else:
    chat = get_active_chat_or_404(db, chat_id)
  messages = redact.redact_messages(materialized_messages(chat))

  if principal.app_id is not None:
    activity.log_event(
      "chat_log_read",
      app_id=principal.app_id,
      scope="chat",
      chat_id=chat_id,
      count=len(messages),
      include_deleted=include_deleted,
      asserted=False,
    )

  return {
    "id": chat.id,
    "title": redact.scrub_secrets(chat.title or ""),
    "created_at": _iso(chat.created_at),
    "updated_at": _iso(chat.updated_at),
    "deleted_at": _iso(chat.deleted_at),
    "tier": "summary",
    "messages": messages,
  }
