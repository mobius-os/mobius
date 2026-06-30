"""Shared `PendingQuestion` definition for the AskUserQuestion flow.

The SDK runner constructs one inside `can_use_tool` and inserts it
into the registry owned by `app.questions` (see `_pending` there).
Routes resolve the future by peeking with `questions.get()`, reclaiming
the same entry with `questions.claim_if()`, then setting the future
result. Keeping the class in its own module avoids a circular import
(questions → runner; runner needs the class).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingQuestion:
  """A question waiting for the partner's AskUserQuestion answer.

  Lives in `questions._pending[chat_id]` while the SDK runner's
  `can_use_tool` callback is blocked on `await future`. The
  `POST /messages` handler resolves `future` when an answers payload
  arrives, which unblocks the callback and lets the SDK continue.

  `run_token` is the persistence run identity of the turn that parked
  this question — the runner stamps the turn's token here so the answer
  route can submit `AnswerQuestion(chat_id, run_token, ...)` and the
  writer actor fences the right `(chat_id, run_token)` snapshot key
  before merging the answer. None for callers that don't allocate a
  token (legacy/test paths); a tokenless `AnswerQuestion` broad-fences
  by chat_id instead.
  """

  question_id: str
  questions: list[dict[str, Any]]
  future: asyncio.Future
  run_token: str | None = None
