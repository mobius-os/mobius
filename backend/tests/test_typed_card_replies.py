"""A card answer the owner typed instead of picking reaches the agent framed.

Agents read "- <question>: <text>" as a choice, so a typed question was parked
for later and the same card re-asked with no visible reply. The turn that
carries such an answer gets a note naming it as the owner's message.
"""

import uuid

import pytest

from app import chat_writer
from app.questions import typed_replies, typed_reply_context

_OPTIONS = [{"id": "0", "label": "Yes (Recommended)"}, {"id": "1", "label": "Not now"}]


def _card(answers, selected=None, questions=None):
  card = {
    "type": "question",
    "question_id": "qid",
    "response_mode": "continuation",
    "questions": questions or [
      {"id": "question-1", "question": "Ship it?", "options": _OPTIONS},
    ],
    "answers": answers,
  }
  if selected is not None:
    card["selected_options"] = selected
  return card


def test_typed_text_on_an_option_card_is_a_typed_reply():
  card = _card({"Ship it?": "what changes for me after this?"})
  assert typed_replies(card) == [("Ship it?", "what changes for me after this?")]


def test_option_pick_is_not_a_typed_reply():
  assert typed_replies(_card({"Ship it?": "Not now"}, {"question-1": ["1"]})) == []


def test_label_answer_from_a_client_without_option_ids_is_a_pick():
  assert typed_replies(_card({"Ship it?": "Not now"})) == []


def test_question_without_options_expects_typed_text():
  card = _card({"Name?": "Ada"}, questions=[{"id": "question-1", "question": "Name?"}])
  assert typed_replies(card) == []


def test_mixed_card_reports_only_the_typed_question():
  card = _card(
    {"Plan?": "One more review round", "Ship it?": "does this break anything?"},
    {"question-0": ["1"]},
    questions=[
      {"id": "question-0", "question": "Plan?", "options": [
        {"id": "0", "label": "Save it"}, {"id": "1", "label": "One more review round"}]},
      {"id": "question-1", "question": "Ship it?", "options": _OPTIONS},
    ],
  )
  assert typed_replies(card) == [("Ship it?", "does this break anything?")]


def _chat(db, card, turn_content, reason="question_answer"):
  chat = chat_writer.create_chat(
    id="typed-reply-chat",
    title="typed reply",
    messages=[
      {"role": "user", "content": "go"},
      {"role": "assistant", "content": "asking", "blocks": [card]},
      {"role": "user", "content": turn_content, "hidden": True,
       "kind": "continuation", "continuation_reason": reason},
    ],
  )
  db.add(chat)
  db.commit()
  return chat


def test_typed_reply_turn_tells_the_agent_to_answer_visibly_first(db):
  content = "- Ship it?: why is option one safer?"
  chat = _chat(db, _card({"Ship it?": "why is option one safer?"}), content)
  note = typed_reply_context(db, chat, content)
  assert "Owner wrote: why is option one safer?" in note
  assert "visible reply text first" in note


def test_option_pick_turn_carries_no_note(db):
  content = "- Ship it?: Not now"
  chat = _chat(db, _card({"Ship it?": "Not now"}, {"question-1": ["1"]}), content)
  assert typed_reply_context(db, chat, content) == ""


def test_note_belongs_only_to_the_turn_that_carries_the_answer(db):
  chat = _chat(db, _card({"Ship it?": "why?"}), "- Ship it?: why?")
  assert typed_reply_context(db, chat, "a later ordinary message") == ""


def test_ordinary_message_is_never_framed_as_a_card_reply(db):
  chat = _chat(db, _card({"Ship it?": "why?"}), "why?", reason=None)
  assert typed_reply_context(db, chat, "why?") == ""


class _Provider:
  name = "Claude Code"

  def check_auth(self, _data_dir):
    return None

  async def ensure_auth(self, _data_dir):
    return None

  def build_env(self, **_kwargs):
    return {}


@pytest.mark.asyncio
async def test_the_answer_turn_reaches_the_agent_with_the_typed_reply_note(
  monkeypatch, db, owner_token,
):
  from app import chat as chat_mod
  from app import chat_queue, claude_sdk_runner, schemas
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartTurn, get_writer

  chat = chat_writer.create_chat(
    id=str(uuid.uuid4()), title="typed reply", agent_settings_json={"model": "claude-opus-4-8"},
    messages=[
      {"role": "user", "content": "go"},
      {"role": "assistant", "content": "asking",
       "blocks": [_card({"Ship it?": "can you explain that first?"})]},
    ],
  )
  db.add(chat)
  db.commit()
  calls = []

  async def fake_runner(**kwargs):
    calls.append(kwargs)
    return {"session_id": "session-1", "cost_usd": 0.0, "error": None}

  async def fake_complete_turn(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  async def fake_record_run_metrics(**_kwargs):
    return None

  monkeypatch.setattr(chat_mod, "registered_provider", lambda _id: _Provider())
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete_turn)
  monkeypatch.setattr(chat_mod, "_record_run_metrics", fake_record_run_metrics)
  monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)

  content = "- Ship it?: can you explain that first?"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token="answer-turn",
    user_msg={"role": "user", "content": content, "ts": 10**13, "cid": "answer-turn",
              "hidden": True, "kind": "continuation", "continuation_reason": "question_answer"},
    title_source=content, default_provider="claude",
  )).result(timeout=5)
  create_broadcast(chat.id)
  try:
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content=content)],
      chat_id=chat.id, session_id=None, provider_id="claude", run_token="answer-turn",
    )
  finally:
    remove_broadcast(chat.id)

  prompt = calls[0]["user_message"]
  assert "Owner wrote: can you explain that first?" in prompt
  assert prompt.index("Owner wrote:") < prompt.index(content)
