"""Notification send and history endpoints."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from slowapi import Limiter
from sqlalchemy import JSON, and_, func, or_
from sqlalchemy.orm import Session

from app import activity, models
from app.database import get_db
from app.deps import (
  Principal,
  get_current_owner,
  get_principal,
  get_principal_or_public_service,
  reject_cross_site,
  require_nondelegated_owner_control,
  require_nondelegated_owner_or_app_control,
)
from app.push import notify_owner
from app.schemas import NotificationOut, NotificationSendRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _record_sender_bucket(
  request: Request,
  principal: Principal = Depends(get_principal_or_public_service),
) -> None:
  """Name the rate-limit bucket after the authenticated sender.

  Every app service, app frame, and agent reaches the backend from the same
  local peer, so a peer-address key made them all share one budget: one busy
  app could starve every other sender. slowapi's key function only receives
  the request, and runs after dependencies resolve, so the resolved principal
  hands its identity over through request.state.
  """
  if principal.app_id is not None:
    request.state.notification_sender = f"app:{principal.app_id}"
  elif principal.chat_id is not None:
    request.state.notification_sender = f"agent:{principal.chat_id}"
  else:
    request.state.notification_sender = "owner"


def _sender_bucket(request: Request) -> str:
  return request.state.notification_sender


limiter = Limiter(key_func=_sender_bucket)


def _is_recovery_action(action: object) -> bool:
  return (
    isinstance(action, dict)
    and isinstance(action.get("action"), str)
    and action["action"].startswith("recover_")
  )


def _has_active_undo(actions: object, now: datetime) -> bool:
  """Keep a live Undo receipt; preserve malformed ones rather than risk losing one."""
  if not isinstance(actions, list):
    return False
  for action in actions:
    if not _is_recovery_action(action):
      continue
    if action.get("completed_at"):
      continue
    try:
      expires_at = datetime.fromisoformat(action["expires_at"].replace("Z", "+00:00"))
      if expires_at.tzinfo is None or expires_at.astimezone(UTC) > now:
        return True
    except (KeyError, AttributeError, TypeError, ValueError):
      return True
  return False


@router.post(
  "/send",
  dependencies=[
    Depends(reject_cross_site),
    Depends(_record_sender_bucket),
  ],
)
# Per sender. A chat app sends one notification per inbound message, and a
# lively group conversation bursts well past a handful a minute; tags collapse
# those in the OS tray, so volume here is history rows, not popups. One a
# second sustained is beyond any human conversation yet still caps a runaway
# loop at 60 history rows a minute for that sender alone.
@limiter.limit("60/minute")
def send_notification(
  request: Request,
  body: NotificationSendRequest,
  principal: Principal = Depends(get_principal_or_public_service),
  db: Session = Depends(get_db),
):
  """Send a push notification to all owner subscriptions.

  A public app service may notify the owner of what a visitor sent it.
  """
  require_nondelegated_owner_control(principal)
  actions_list = (
    [a.model_dump(exclude_none=True) for a in body.actions] if body.actions else None
  )
  if any(a.action.startswith("recover_") for a in (body.actions or [])):
    raise HTTPException(
      status_code=403,
      detail="Recovery actions are created by resource deletion endpoints.",
    )
  # An app-scoped caller can't spoof the notification's source: force it to be
  # attributed to the app itself, so a mini-app can't masquerade as the system
  # or another app in a push (a phishing vector). Owner tokens keep full control.
  if principal.app_id is not None:
    source_type, source_id = "app", str(principal.app_id)
  else:
    source_type, source_id = body.source_type, body.source_id
  notification_id = notify_owner(
    db,
    principal.owner.id,
    title=body.title,
    body=body.body,
    source_type=source_type,
    source_id=source_id,
    icon=body.icon,
    target=body.target,
    actions=actions_list,
    tag=body.tag,
  )
  return {"id": notification_id}


@router.get("/unread-count")
def unread_count(
  # Owner-only, matching the list endpoint: the bell badge is the owner's
  # surface and app tokens have no need to observe it.
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Count of notifications not explicitly marked read."""
  n = (
    db.query(func.count(models.Notification.id))
    .filter(
      models.Notification.owner_id == owner.id,
      models.Notification.read_at.is_(None),
    )
    .scalar()
  )
  return {"count": int(n or 0)}


@router.get("/new-count")
def new_count(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Arrivals not yet acknowledged by opening the notification panel."""
  n = db.query(func.count(models.Notification.id)).filter(
    models.Notification.owner_id == owner.id,
    models.Notification.seen_at.is_(None),
  ).scalar()
  return {"count": int(n or 0)}


@router.post(
  "/seen-all",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
def seen_all(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Acknowledge current arrivals while leaving unread rows and history intact."""
  updated = db.query(models.Notification).filter(
    models.Notification.owner_id == owner.id,
    models.Notification.seen_at.is_(None),
  ).update({"seen_at": datetime.now(UTC)}, synchronize_session=False)
  db.commit()
  return {"updated": int(updated)}


@router.post(
  "/read-all",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
def read_all(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Explicitly mark every unread notification read. Idempotent.

  Only rows with read_at NULL are touched, so a notification that commits
  concurrently with this UPDATE simply stays unread and is picked up by the
  next call — no lost-update window.
  """
  updated = (
    db.query(models.Notification)
    .filter(
      models.Notification.owner_id == owner.id,
      models.Notification.read_at.is_(None),
    )
    .update(
      {"read_at": datetime.now(UTC), "seen_at": datetime.now(UTC)},
      synchronize_session=False,
    )
  )
  db.commit()
  return {"updated": int(updated)}


@router.post(
  "/{notification_id}/read",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
def read_notification(
  notification_id: str,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Mark one owner's notification read without removing its history or actions."""
  exists = db.query(models.Notification.id).filter(
    models.Notification.owner_id == owner.id,
    models.Notification.id == notification_id,
  ).first()
  if exists is None:
    raise HTTPException(status_code=404, detail="Notification not found.")
  updated = db.query(models.Notification).filter(
    models.Notification.owner_id == owner.id,
    models.Notification.id == notification_id,
    models.Notification.read_at.is_(None),
  ).update(
    {"read_at": datetime.now(UTC), "seen_at": datetime.now(UTC)},
    synchronize_session=False,
  )
  db.commit()
  return {"updated": int(updated)}


@router.delete(
  "",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
def clear_notifications(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Clear ordinary history without destroying still-usable Undo receipts."""
  started_at = datetime.now(UTC)
  deleted = 0
  cursor = ""
  while True:
    batch = (
      db.query(models.Notification.id, models.Notification.actions, models.Notification.sent_at)
      .filter(
        models.Notification.owner_id == owner.id,
        models.Notification.id > cursor,
        # A notification delivered during this sweep belongs to the next history.
        or_(models.Notification.sent_at.is_(None), models.Notification.sent_at < started_at),
      )
      .order_by(models.Notification.id)
      .limit(500)
      .all()
    )
    if not batch:
      break
    cursor = batch[-1].id
    for row in batch:
      if _has_active_undo(row.actions, started_at):
        continue
      # Match the selected snapshot at the DELETE boundary: a concurrently
      # changed receipt or arrival must not be removed on stale classification.
      deleted += db.query(models.Notification).filter(
        models.Notification.owner_id == owner.id,
        models.Notification.id == row.id,
        models.Notification.actions == row.actions if row.actions is not None
        else or_(models.Notification.actions.is_(None), models.Notification.actions == JSON.NULL),
        models.Notification.sent_at == row.sent_at if row.sent_at is not None
        else models.Notification.sent_at.is_(None),
      ).delete(synchronize_session=False)
  db.commit()
  # Content-free, timestamped activity record for future history-loss diagnosis.
  activity.log_event("notification_history_cleared", deleted=deleted)
  return {"deleted": int(deleted)}


@router.delete(
  "/{notification_id}",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
def dismiss_notification(
  notification_id: str,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Dismiss one notice; chat Undo may be deliberately removed, other recovery stays."""
  notification = (
    db.query(models.Notification)
    .filter(
      models.Notification.owner_id == owner.id,
      models.Notification.id == notification_id,
    )
    .one_or_none()
  )
  if notification is None:
    raise HTTPException(status_code=404, detail="Notification not found.")
  actions = notification.actions if isinstance(notification.actions, list) else []
  if any(
    _is_recovery_action(action) and action["action"] != "recover_chat"
    for action in actions
  ):
    raise HTTPException(
      status_code=409,
      detail="Undo notifications cannot be dismissed individually.",
    )
  db.delete(notification)
  db.commit()
  return {"deleted": 1}


@router.get("")
def list_notifications(
  # Owner-only: the notification history is the owner's. App tokens have no
  # need to read it and previously could enumerate the full history.
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
  limit: int = Query(20, ge=1, le=100),
  before: str | None = Query(None),
  before_at: datetime | None = Query(None),
):
  """Return notification history, paginated."""
  q = (
    db.query(models.Notification)
    .filter(models.Notification.owner_id == owner.id)
    .order_by(
      models.Notification.sent_at.desc(),
      models.Notification.id.desc(),
    )
  )
  if before_at is not None and (not before or before_at.tzinfo is None):
    raise HTTPException(status_code=400, detail="Invalid notification cursor.")
  if before:
    if before_at is None:
      # Existing ID-only callers retain their cursor contract. The shell also
      # sends the row's timestamp so paging survives that row being removed.
      ref = (
        db.query(models.Notification)
        .filter(
          models.Notification.owner_id == owner.id,
          models.Notification.id == before,
        )
        .one_or_none()
      )
      if ref is None:
        raise HTTPException(status_code=400, detail="Invalid notification cursor.")
      cursor_at = ref.sent_at
    else:
      cursor_at = before_at.astimezone(UTC).replace(tzinfo=None)
    q = q.filter(or_(
      models.Notification.sent_at < cursor_at,
      and_(
        models.Notification.sent_at == cursor_at,
        models.Notification.id < before,
      ),
    ))
  return [
    NotificationOut.model_validate(n) for n in q.limit(limit).all()
  ]
