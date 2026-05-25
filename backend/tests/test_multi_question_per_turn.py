"""Locks in the multi-question-per-turn answer-merge invariant.

CLAUDE.md flags the multi-question case as a latent risk: when an
agent calls AskUserQuestion twice within one turn, the runner's
writeback rebuilds the message from `assistant_blocks` (which has no
`answers` field). Without a careful merge, the second writeback would
wipe the first question's answers.

Today's `_update_last_assistant_message` keys merge by question id
(or, if the id is missing, by joined question text) — NOT by block
position — so a future runner that interleaves a tool block between
two questions on a partial replay still merges correctly. These
tests exercise both id-keyed and text-keyed branches.
"""

from app import models
from app.chat import _update_last_assistant_message


def _seed_chat_with_two_question_blocks(db, *, answers_for_first):
  """Creates a chat whose last assistant message has TWO question
  blocks; the first carries answers, the second does not.

  Returns the chat id.
  """
  chat = models.Chat(
    id="multi-q-chat",
    title="multi q",
    messages=[
      {"role": "user", "content": "kick off"},
      {
        "role": "assistant",
        "content": "thinking",
        "blocks": [
          {"type": "text", "content": "thinking"},
          {
            "type": "question",
            "questions": [
              {"id": "q-color", "question": "Color?",
               "options": ["red", "blue"]},
            ],
            "answers": answers_for_first,
          },
          {
            "type": "question",
            "questions": [
              {"id": "q-size", "question": "Size?",
               "options": ["s", "m", "l"]},
            ],
          },
        ],
      },
    ],
  )
  db.add(chat)
  db.commit()
  return chat.id


def test_id_keyed_merge_preserves_first_answer_when_second_is_answered(db):
  """The agent calls AskUserQuestion twice, the user answers the
  first, then the runner rewrites the assistant message after the
  second answer arrives. The first question's answers MUST survive
  the rewrite — keyed by question id."""
  chat_id = _seed_chat_with_two_question_blocks(
    db, answers_for_first={"q-color": "red"},
  )

  # New message snapshot, rebuilt from assistant_blocks — neither
  # question carries answers (the runner has no answers field).
  rewritten = {
    "role": "assistant",
    "content": "thinking",
    "blocks": [
      {"type": "text", "content": "thinking"},
      {
        "type": "question",
        "questions": [
          {"id": "q-color", "question": "Color?",
           "options": ["red", "blue"]},
        ],
      },
      {
        "type": "question",
        "questions": [
          {"id": "q-size", "question": "Size?",
           "options": ["s", "m", "l"]},
        ],
        # The user just answered q-size — the chats_stream route wrote
        # this into the persisted message before the runner rewrite.
        "answers": {"q-size": "m"},
      },
    ],
  }
  ok = _update_last_assistant_message(db, chat_id, rewritten)
  assert ok is True

  db.refresh(db.query(models.Chat).filter(models.Chat.id == chat_id).first())
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  assistant = chat.messages[-1]
  blocks = assistant["blocks"]
  assert blocks[1]["type"] == "question"
  # The first answer carried over from the prior message snapshot.
  assert blocks[1].get("answers") == {"q-color": "red"}
  # The second answer is the one the rewrite itself supplied.
  assert blocks[2].get("answers") == {"q-size": "m"}


def test_text_keyed_merge_when_id_missing(db):
  """Falls back to joined-text matching when a runner ever omits the
  id. Locks in the secondary key path so a future SDK quirk doesn't
  silently downgrade to position-match."""
  # Seed with text-only questions (no id).
  chat = models.Chat(
    id="multi-q-textkey",
    title="multi q text",
    messages=[
      {"role": "user", "content": "go"},
      {
        "role": "assistant",
        "content": "asking",
        "blocks": [
          {
            "type": "question",
            "questions": [{"question": "Pick a fruit"}],
            "answers": {"Pick a fruit": "apple"},
          },
          {
            "type": "question",
            "questions": [{"question": "Pick a vegetable"}],
          },
        ],
      },
    ],
  )
  db.add(chat)
  db.commit()

  rewritten = {
    "role": "assistant",
    "content": "asking",
    "blocks": [
      {
        "type": "question",
        "questions": [{"question": "Pick a fruit"}],
      },
      {
        "type": "question",
        "questions": [{"question": "Pick a vegetable"}],
        "answers": {"Pick a vegetable": "carrot"},
      },
    ],
  }
  _update_last_assistant_message(db, chat.id, rewritten)

  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  assert blocks[0].get("answers") == {"Pick a fruit": "apple"}
  assert blocks[1].get("answers") == {"Pick a vegetable": "carrot"}


def test_merge_survives_block_reorder(db):
  """If a future runner ever interleaves a tool block between two
  questions during partial replay, id-keyed merge still finds the
  right answers — proving we are NOT relying on block position."""
  chat_id = _seed_chat_with_two_question_blocks(
    db, answers_for_first={"q-color": "blue"},
  )

  # The rewritten message has a tool block sandwiched between the
  # two questions — different positions than the persisted message.
  rewritten = {
    "role": "assistant",
    "content": "thinking",
    "blocks": [
      {
        "type": "question",
        "questions": [
          {"id": "q-color", "question": "Color?",
           "options": ["red", "blue"]},
        ],
      },
      {
        "type": "tool",
        "tool": "Read",
        "input": "/tmp/x",
        "output": "ok",
        "status": "done",
      },
      {
        "type": "question",
        "questions": [
          {"id": "q-size", "question": "Size?",
           "options": ["s", "m", "l"]},
        ],
      },
    ],
  }
  _update_last_assistant_message(db, chat_id, rewritten)

  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  blocks = chat.messages[-1]["blocks"]
  # The first question is at index 0 in the new message vs. index 1
  # in the persisted message — position-match would have failed here.
  assert blocks[0].get("answers") == {"q-color": "blue"}
  # The second question (no prior answer) stays answerless.
  assert "answers" not in blocks[2] or not blocks[2].get("answers")
