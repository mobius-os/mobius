"""Shell-level contract for chats waiting on their owner.

Owner-input events carry state only: the kind of interaction that is waiting
and, for durable questions, the exact id used by the question lifecycle.
Secure field metadata and values stay on the chat-scoped secure-input channel.
"""

from __future__ import annotations

from typing import Literal

from app.broadcast import get_system_broadcast


OwnerInputKind = Literal["question", "secure_input"]
_QUESTION_ID_UNSET = object()


def publish_owner_input_changed(
  chat_id: str,
  input_kind: OwnerInputKind | None,
  *,
  question_id: str | None | object = _QUESTION_ID_UNSET,
) -> None:
  """Tell every shell whether a chat is waiting for owner involvement.

  ``questionId`` is optional rather than always nullable. Question producers
  include it so the shell can patch the durable question projection; secure
  inputs omit it so they never overwrite an unrelated question lifecycle.
  """
  event = {
    "type": "chat_owner_input_changed",
    "chatId": chat_id,
    "inputKind": input_kind,
  }
  if question_id is not _QUESTION_ID_UNSET:
    event["questionId"] = question_id
  get_system_broadcast().publish(event)
