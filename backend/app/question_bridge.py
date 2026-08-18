"""Provider-neutral parking for an interactive agent question.

Claude and Codex expose different SDK callbacks and answer wire shapes, but the
Möbius lifecycle in between is identical: reject an overlapping question,
register a future, durably publish the card, wait without a timeout, and remove
the exact registry entry on every exit path. Keeping that transaction here
prevents the provider adapters from drifting on persistence or cleanup rules.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping, Sequence
from typing import Any
from uuid import uuid4

from app.broadcast import get_system_broadcast
from app.pending_questions import PendingQuestion


class QuestionOverlapError(RuntimeError):
  """Another unanswered question already owns this chat."""


class QuestionPersistenceError(RuntimeError):
  """The question card could not be durably committed before broadcast."""


async def park_question(
  *,
  chat_id: str,
  questions: Sequence[dict[str, Any]],
  bc: Any,
  pending_questions: MutableMapping[str, PendingQuestion],
) -> dict[str, Any]:
  """Publish and await one question using the shared Möbius lifecycle.

  Provider adapters retain their admission checks, payload validation, answer
  translation, and error presentation. Cancellation intentionally propagates
  so each SDK callback can express it in its native result.
  """
  existing = pending_questions.get(chat_id)
  if existing is not None and not existing.future.done():
    raise QuestionOverlapError(
      f"AskUserQuestion already pending for chat {chat_id}"
    )

  future = asyncio.get_running_loop().create_future()
  payload = list(questions)
  pending = PendingQuestion(
    question_id=str(uuid4()),
    questions=payload,
    future=future,
    run_token=getattr(bc, "run_token", None),
  )
  pending_questions[chat_id] = pending

  try:
    try:
      await bc.publish_question({
        "type": "question",
        "question_id": pending.question_id,
        "questions": payload,
      })
      get_system_broadcast().publish({
        "type": "chat_owner_input_changed",
        "chatId": chat_id,
        "questionId": pending.question_id,
      })
    except Exception as exc:
      raise QuestionPersistenceError(
        "could not save the question"
      ) from exc
    answers = await future
    return answers or {}
  finally:
    if pending_questions.get(chat_id) is pending:
      pending_questions.pop(chat_id, None)
