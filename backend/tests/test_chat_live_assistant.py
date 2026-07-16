"""Streaming snapshots stay bounded without weakening transcript durability."""

from app import models
from app.chat_transcript import materialized_messages
from app.chat_writer import (
  finalize_response_outcome,
  update_live_assistant,
)


def _chat(db, *, trailing_role="user"):
  messages = [
    {"role": "user", "content": "old", "ts": 1},
    {"role": "assistant", "blocks": [{"type": "text", "text": "done"}],
     "ts": 2},
    {"role": trailing_role, "content": "current", "ts": 3},
  ]
  chat = models.Chat(
    id="live-chat",
    title="Live",
    messages=messages,
    live_assistant={"role": "assistant", "blocks": [], "ts": 4},
  )
  db.add(chat)
  db.commit()
  return chat, messages


def test_stream_snapshot_updates_only_live_value(db):
  chat, history = _chat(db)

  assert update_live_assistant(db, chat.id, {
    "role": "assistant",
    "blocks": [{"type": "text", "text": "streaming"}],
  }) is True

  db.refresh(chat)
  assert chat.messages == history
  assert chat.live_assistant["ts"] == 4
  assert chat.live_assistant["blocks"][0]["text"] == "streaming"
  assert materialized_messages(chat)[-1] == chat.live_assistant


def test_finalize_merges_live_turn_once_and_clears_snapshot(db):
  chat, history = _chat(db)
  update_live_assistant(db, chat.id, {
    "role": "assistant",
    "blocks": [{"type": "text", "text": "partial"}],
  })

  outcome = finalize_response_outcome(
    db, chat.id, [{"type": "text", "text": "complete"}],
  )

  db.refresh(chat)
  assert outcome.value == "applied"
  assert chat.live_assistant is None
  assert len(chat.messages) == len(history) + 1
  assert chat.messages[-1]["ts"] == 4
  assert chat.messages[-1]["blocks"][0]["text"] == "complete"


def test_materialized_snapshot_replaces_question_barrier_row():
  class Row:
    messages = [{
      "role": "assistant",
      "blocks": [{"type": "question", "question_id": "q1"}],
      "ts": 7,
    }]
    live_assistant = {
      "role": "assistant",
      "blocks": [
        {"type": "question", "question_id": "q1", "answers": {"q": "a"}},
        {"type": "text", "text": "continuing"},
      ],
      "ts": 7,
    }

  projected = materialized_messages(Row())
  assert len(projected) == 1
  assert projected[0] == Row.live_assistant
