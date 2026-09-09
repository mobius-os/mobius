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


def attach_activity_positions(db, chat_id: str, events: list[dict]) -> None:
  """Attach only the requested chat's evidence after ordinary visibility checks."""
  if not events:
    return
  positions = dict(db.query(
    models.ChatActivityPosition.event_id, models.ChatActivityPosition.position,
  ).filter(
    models.ChatActivityPosition.chat_id == chat_id,
    models.ChatActivityPosition.event_id.in_([event["id"] for event in events]),
  ).all())
  for event in events:
    event["display_position"] = positions.get(event["id"])
