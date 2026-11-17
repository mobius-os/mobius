"""When a chat's runs ended, as the epoch milliseconds a transcript row's `ts` uses.

A reply row's `ts` is fixed when its turn starts (it is part of the row's
identity), so it cannot say when the reply settled. The run that wrote the row
already records `ended_at`; the chat detail read and the stream's final `done`
event both carry it from here as `run_ended_ts`, so the two cannot disagree.
"""

from __future__ import annotations

from datetime import UTC

from sqlalchemy.orm import Session

from app import models

# Only these record when the run's reply actually ended. An "interrupted" run
# was closed later by something else (startup recovery after a restart, a
# newer turn superseding a parked run, a provider switch), so its `ended_at`
# is when that cleanup ran; such replies keep their labelled start instead.
_ENDED_WHEN_REPLY_ENDED = ("completed", "failed", "stopped")


def _owning_chat_ids(db: Session, chat_id: str) -> list[str]:
  """`chat_id` and the chats it was branched from: a branch copies its
  source's replies with their run ids, and those runs belong to the source."""
  chat_ids = []
  current = chat_id
  while current and current not in chat_ids:
    chat_ids.append(current)
    origin = db.query(models.Chat.branch_origin).filter(models.Chat.id == current).scalar()
    current = origin.get("chat_id") if isinstance(origin, dict) else None
  return chat_ids


def run_ended_ts(db: Session, chat_id: str, run_ids) -> dict[str, int]:
  """Each run among `run_ids` that wrote replies in `chat_id` (or in a chat
  it was branched from) and is known to have ended, mapped to when."""
  run_ids = {run_id for run_id in run_ids if run_id}
  if not run_ids:
    return {}
  rows = db.query(models.ChatRun.id, models.ChatRun.ended_at).filter(
    models.ChatRun.chat_id.in_(_owning_chat_ids(db, chat_id)),
    models.ChatRun.id.in_(run_ids),
    models.ChatRun.status.in_(_ENDED_WHEN_REPLY_ENDED),
    models.ChatRun.ended_at.isnot(None),
  ).all()
  return {
    run_id: round((ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=UTC)).timestamp() * 1000)
    for run_id, ended_at in rows
  }
