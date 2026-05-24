"""Shared `PendingQuestion` definition for the AskUserQuestion flow.

The SDK runner constructs one inside `can_use_tool` and inserts it
into the `_pending_questions` registry owned by `chat.py`. Routes
resolve the future via `chat.deliver_answer()`. Keeping the class
in its own module avoids a circular import (chat.py → runner;
runner needs the class) and removes the duck-typed-by-accident
duplication that lived in both modules during the SDK migration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingQuestion:
  """A question waiting for the partner's AskUserQuestion answer.

  Lives in `chat._pending_questions[chat_id]` while the SDK runner's
  `can_use_tool` callback is blocked on `await future`. The
  `POST /messages` handler resolves `future` when an answers payload
  arrives, which unblocks the callback and lets the SDK continue.
  """

  question_id: str
  questions: list[dict[str, Any]]
  future: asyncio.Future
