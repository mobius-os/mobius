"""Exact-chat durable peer notes and helper-result activity projection."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
import json

from sqlalchemy import and_, literal, or_
from sqlalchemy.orm import Session

from app import models
from app.agent_coordination import (
  chat_message_history_query,
  serialize_messages,
)
from app.delegations import (
  TERMINAL_DELEGATION_STATUSES,
  derived_status,
  helper_current_activity,
  running_helper_activity_id,
)


DEFAULT_ACTIVITY_LIMIT = 50
MAX_ACTIVITY_LIMIT = 100


@dataclass(frozen=True, order=True)
class ActivityCursor:
  """Exclusive latest-first boundary shared by both durable event sources."""

  created_at: datetime
  event_id: str


def _cursor_value(cursor: ActivityCursor) -> str:
  raw = json.dumps({
    "v": 1,
    "created_at": cursor.created_at.isoformat(),
    "id": cursor.event_id,
  }, separators=(",", ":")).encode("utf-8")
  return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_cursor(value: str | None) -> ActivityCursor | None:
  if value is None:
    return None
  try:
    padding = "=" * (-len(value) % 4)
    payload = json.loads(base64.urlsafe_b64decode(value + padding))
    raw_created_at = payload["created_at"]
    event_id = payload["id"]
    if payload.get("v") != 1 or not isinstance(event_id, str) or not event_id:
      raise ValueError
    created_at = datetime.fromisoformat(raw_created_at)
    if created_at.tzinfo is not None:
      created_at = created_at.astimezone(UTC).replace(tzinfo=None)
  except (binascii.Error, KeyError, TypeError, ValueError) as exc:
    raise ValueError("Activity cursor is invalid.") from exc
  return ActivityCursor(created_at=created_at, event_id=event_id)


def _peer_events(
  db: Session, chat_id: str, cursor: ActivityCursor | None, limit: int,
) -> list[tuple[ActivityCursor, dict]]:
  message = models.AgentCoordinationMessage
  event_id = literal("peer:") + message.id
  query = chat_message_history_query(db, chat_id)
  if cursor is not None:
    query = query.filter(or_(
      message.created_at < cursor.created_at,
      and_(
        message.created_at == cursor.created_at,
        event_id < cursor.event_id,
      ),
    ))
  rows = query.order_by(
    message.created_at.desc(), event_id.desc(),
  ).limit(limit).all()
  serialized = serialize_messages(db, rows)
  events = []
  for row, item in zip(rows, serialized, strict=True):
    message_id = str(item.pop("id"))
    stable_id = f"peer:{message_id}"
    item.update({
      "id": stable_id,
      "message_id": message_id,
      "type": "peer_message",
      "created_at": row.created_at.isoformat(),
    })
    events.append((
      ActivityCursor(row.created_at, stable_id),
      item,
    ))
  return events


def _run_duration_ms(run) -> int | None:
  """How long a helper's latest turn took, when it has both ends."""
  if run is None or not run.started_at or not run.ended_at:
    return None
  return max(0, int((run.ended_at - run.started_at).total_seconds() * 1000))


def _helper_row_events(
  db: Session, chat_id: str, cursor: ActivityCursor | None, limit: int,
) -> list[tuple[ActivityCursor, dict]]:
  """One row per helper for its whole life, at the point it was launched.

  While the helper works the row carries its current step; once it settles the
  same row carries its final status and duration. Its result is read in the
  helper's own conversation, so there is no second row.
  """
  event_id = literal("delegation:") + models.Delegation.id + literal(":running")
  query = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == chat_id,
  )
  if cursor is not None:
    query = query.filter(or_(
      models.Delegation.created_at < cursor.created_at,
      and_(
        models.Delegation.created_at == cursor.created_at,
        event_id < cursor.event_id,
      ),
    ))
  rows = query.order_by(
    models.Delegation.created_at.desc(), event_id.desc(),
  ).limit(limit).all()
  events = []
  for row in rows:
    status, run, _result = derived_status(db, row, load_result=False)
    if run is None and status == "completed":
      # Without a run, a source-reported "completed" is not terminal evidence;
      # of the no-run overrides only needs_review settles a helper.
      status = "starting"
    settled = status in TERMINAL_DELEGATION_STATUSES
    stable_id = running_helper_activity_id(row.id)
    started = run.started_at if run is not None and run.started_at else row.created_at
    events.append((ActivityCursor(row.created_at, stable_id), {
      "id": stable_id,
      "type": "helper_result",
      "created_at": row.created_at.isoformat(),
      "delegation_id": row.id,
      "task_key": row.task_key,
      "provider": row.provider,
      "model": row.model,
      "started_at": started.isoformat() if started else None,
      "duration_ms": _run_duration_ms(run) if settled else None,
      "activity": None if settled else helper_current_activity(row.child_chat_id),
      "status": status,
      "child_chat_id": row.child_chat_id,
      "source_work_id": row.parent_root_run_id,
      "consumption": _helper_consumption(row, run) if settled else "unknown",
    }))
  return events


def _helper_consumption(row: models.Delegation, run) -> str:
  """How far a settled helper's result has reached its parent agent."""
  if row.result_incorporated_at is not None:
    return "incorporated"
  if row.parent_woken_at is not None:
    return "notified"
  if (
    row.notify_parent_on_complete
    and row.cancelled_at is None
    and run is not None
    and run.status in ("completed", "failed")
  ):
    return "available"
  return "unknown"


def chat_activity_page(
  db: Session,
  chat_id: str,
  *,
  before: str | None = None,
  limit: int = DEFAULT_ACTIVITY_LIMIT,
) -> dict:
  """Merge exact-chat activity sources with stable latest-first pagination."""
  bounded_limit = max(1, min(int(limit), MAX_ACTIVITY_LIMIT))
  cursor = _parse_cursor(before)
  source_limit = bounded_limit + 1
  events = [
    *_peer_events(db, chat_id, cursor, source_limit),
    *_helper_row_events(db, chat_id, cursor, source_limit),
  ]
  events.sort(key=lambda item: item[0], reverse=True)
  page = events[:bounded_limit]
  from app.activity_position import attach_activity_positions
  items = [item for _key, item in page]
  # Helpers that settled before launch rows existed recorded their position
  # only on their old finished event; their single row keeps that place.
  attach_activity_positions(db, chat_id, items, fallback_ids={
    item["id"]: f"delegation:{item['delegation_id']}:completed"
    for item in items if item["type"] == "helper_result"
  })
  return {
    "events": items,
    "next_before": (
      _cursor_value(page[-1][0])
      if len(events) > bounded_limit and page else None
    ),
  }
