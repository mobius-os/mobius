"""Provider-neutral interactive-question lifecycle contracts."""

from __future__ import annotations

import asyncio

import pytest

from app.pending_questions import PendingQuestion
from app.question_bridge import (
  QuestionOverlapError,
  QuestionPersistenceError,
  park_question,
)


class _Bus:
  run_token = "run-1"

  def __init__(self, *, fail: bool = False):
    self.fail = fail
    self.events: list[dict] = []

  async def publish_question(self, event: dict) -> None:
    if self.fail:
      raise RuntimeError("write unavailable")
    self.events.append(event)


def test_park_question_publishes_before_waiting_and_cleans_exact_entry(
  monkeypatch,
):
  async def exercise():
    registry: dict[str, PendingQuestion] = {}
    bus = _Bus()
    system_events = []

    monkeypatch.setattr(
      "app.question_bridge.publish_owner_input_changed",
      lambda chat_id, input_kind, *, question_id: system_events.append({
        "type": "chat_owner_input_changed",
        "chatId": chat_id,
        "inputKind": input_kind,
        "questionId": question_id,
      }),
    )
    task = asyncio.create_task(park_question(
      chat_id="chat-1",
      questions=[{"question": "Pick"}],
      bc=bus,
      pending_questions=registry,
    ))
    await asyncio.sleep(0)

    pending = registry["chat-1"]
    assert pending.run_token == "run-1"
    assert bus.events == [{
      "type": "question",
      "question_id": pending.question_id,
      "questions": [{"question": "Pick"}],
    }]
    assert system_events == [{
      "type": "chat_owner_input_changed",
      "chatId": "chat-1",
      "inputKind": "question",
      "questionId": pending.question_id,
    }]
    pending.future.set_result({"Pick": "First"})
    assert await task == {"Pick": "First"}
    assert registry == {}

  asyncio.run(exercise())


def test_park_question_rejects_an_unanswered_overlap():
  async def exercise():
    loop = asyncio.get_running_loop()
    existing = PendingQuestion("q-1", [], loop.create_future())
    registry = {"chat-1": existing}
    with pytest.raises(QuestionOverlapError):
      await park_question(
        chat_id="chat-1",
        questions=[],
        bc=_Bus(),
        pending_questions=registry,
      )
    assert registry == {"chat-1": existing}

  asyncio.run(exercise())


def test_park_question_cleans_registry_when_persistence_fails():
  async def exercise():
    registry: dict[str, PendingQuestion] = {}
    with pytest.raises(QuestionPersistenceError):
      await park_question(
        chat_id="chat-1",
        questions=[],
        bc=_Bus(fail=True),
        pending_questions=registry,
      )
    assert registry == {}

  asyncio.run(exercise())


def test_park_question_does_not_remove_a_newer_replacement():
  async def exercise():
    registry: dict[str, PendingQuestion] = {}
    task = asyncio.create_task(park_question(
      chat_id="chat-1",
      questions=[],
      bc=_Bus(),
      pending_questions=registry,
    ))
    await asyncio.sleep(0)
    original = registry["chat-1"]
    replacement = PendingQuestion(
      "q-2", [], asyncio.get_running_loop().create_future(),
    )
    registry["chat-1"] = replacement
    original.future.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert registry["chat-1"] is replacement

  asyncio.run(exercise())
