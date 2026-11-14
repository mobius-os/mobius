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


def run_ended_ts(db: Session, chat_id: str, run_ids) -> dict[str, int]:
  """Each ended run among `run_ids` of `chat_id`, mapped to when it ended."""
  run_ids = {run_id for run_id in run_ids if run_id}
  if not run_ids:
    return {}
  rows = db.query(models.ChatRun.id, models.ChatRun.ended_at).filter(
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.id.in_(run_ids),
    models.ChatRun.ended_at.isnot(None),
  ).all()
  return {
    run_id: round((ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=UTC)).timestamp() * 1000)
    for run_id, ended_at in rows
  }
