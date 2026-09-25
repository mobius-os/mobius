"""Capture immutable live display frontiers without changing chat messages."""

from sqlalchemy.dialects.sqlite import insert

from app import models


def record_activity_position(db, chat_id: str, event_id: str) -> None:
  """Record the first observation in the caller's event transaction.

  A null observation is retained too: retrying a completed event must not
  relocate it into an unrelated later turn. No live sink means no evidence.
  """
  from app.chat_event_sink import active_sink_activity_position

  position = active_sink_activity_position(chat_id)
  db.execute(insert(models.ChatActivityPosition).values(
    chat_id=chat_id, event_id=event_id, position=position,
  ).on_conflict_do_nothing(index_elements=["chat_id", "event_id"]))


def attach_activity_positions(
  db, chat_id: str, events: list[dict],
  fallback_ids: dict[str, str] | None = None,
) -> None:
  """Attach only the requested chat's evidence after ordinary visibility checks.

  ``fallback_ids`` names an older event id whose recorded position an event
  inherits when it has none of its own.
  """
  if not events:
    return
  fallback_ids = fallback_ids or {}
  wanted = [event["id"] for event in events]
  wanted += [fallback_ids[event_id] for event_id in wanted if event_id in fallback_ids]
  positions = dict(db.query(
    models.ChatActivityPosition.event_id, models.ChatActivityPosition.position,
  ).filter(
    models.ChatActivityPosition.chat_id == chat_id,
    models.ChatActivityPosition.event_id.in_(wanted),
  ).all())
  for event in events:
    position = positions.get(event["id"])
    if position is None and event["id"] in fallback_ids:
      position = positions.get(fallback_ids[event["id"]])
    event["display_position"] = position
