"""Chat drawer recency follows owner actions, not generic row updates."""

from datetime import UTC, datetime

from app import models
from app.chat_writer import AppendSteeredUserMessage, get_writer


def test_steered_message_advances_drawer_recency(db):
  old_activity = datetime(2000, 1, 1, tzinfo=UTC)
  chat = models.Chat(
    id="steer-recency",
    title="Steer recency",
    messages=[
      {"role": "user", "content": "start", "ts": 1},
      {"role": "assistant", "content": "working", "ts": 2},
    ],
    activity_at=old_activity,
  )
  db.add(chat)
  db.commit()

  result = get_writer().submit(AppendSteeredUserMessage(
    chat_id=chat.id,
    run_token="steer-recency-run",
    user_msg={
      "role": "user",
      "content": "change course",
      "ts": 3,
      "cid": "steer-recency-cid",
    },
  )).result(timeout=5)

  assert result["stored"]["content"] == "change course"
  db.expire_all()
  persisted = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert persisted.activity_at.replace(tzinfo=UTC) > old_activity
