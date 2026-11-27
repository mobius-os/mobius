"""Shell-level contract for chats waiting on their owner.

Owner-input events carry state only: the kind of interaction that is waiting
and, for durable questions, the exact id used by the question lifecycle.
Secure field metadata and values stay on the chat-scoped secure-input channel.

A saved owner-input card is also the single source of its owner notification:
the platform sends it once when the card is committed, so agents never send
their own "needs your answer" push. A request-list card republished at a
later turn's end notifies only when it shows a request revision the owner has
not been shown (`owner_input_requests.shows_owner_something_new`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from app.broadcast import get_system_broadcast


OwnerInputKind = Literal["question", "secure_input"]
_QUESTION_ID_UNSET = object()


def publish_owner_input_changed(
  chat_id: str,
  input_kind: OwnerInputKind | None,
  *,
  question_id: str | None | object = _QUESTION_ID_UNSET,
  request_card_id: str | None | object = _QUESTION_ID_UNSET,
) -> None:
  """Tell every shell whether a chat is waiting for owner involvement.

  ``questionId`` is optional rather than always nullable. Question producers
  include it so the shell can patch the durable question projection; secure
  inputs omit it so they never overwrite an unrelated question lifecycle.
  ``requestCardId`` is the request-list card (``Chat.open_request_card_id``);
  it never travels as ``questionId``, which the shell treats as the barring
  card that locks the composer.
  """
  event = {
    "type": "chat_owner_input_changed",
    "chatId": chat_id,
    "inputKind": input_kind,
  }
  if question_id is not _QUESTION_ID_UNSET:
    event["questionId"] = question_id
  if request_card_id is not _QUESTION_ID_UNSET:
    event["requestCardId"] = request_card_id
  get_system_broadcast().publish(event)


OWNER_INPUT_NOTIFICATION_TITLE = "Möbius needs your answer"
_OWNER_INPUT_NOTIFICATION_TAG = "owner-input"
_BODY_MAX = 80


def _card_summary(questions: Sequence[dict[str, Any]]) -> str | None:
  """The first question's prompt, shortened for a lock-screen line."""
  for question in questions:
    text = str(
      question.get("question") or question.get("text") or question.get("header") or ""
    ).strip()
    if text:
      return text if len(text) <= _BODY_MAX else text[:_BODY_MAX - 1].rstrip() + "…"
  return None


async def notify_owner_input_card(
  chat_id: str, question_id: str, questions: Sequence[dict[str, Any]],
) -> None:
  """Send the one owner notification for a newly saved owner-input card.

  The notification id derives from the card's question id, so the card owns
  exactly one history row however often its save path runs. The stable tag
  lets a newer card's push replace an older one from the same chat on the
  device. Presence suppression in ``push`` keeps it quiet while the owner is
  already watching the chat.
  """
  from app import models, push
  from app.database import SessionLocal

  with SessionLocal() as db:
    owner_id = db.query(models.Owner.id).scalar()
    if owner_id is None:
      return
    await push.notify_owner_async(
      db,
      owner_id,
      title=OWNER_INPUT_NOTIFICATION_TITLE,
      body=_card_summary(questions),
      source_type="agent",
      source_id=chat_id,
      target=f"/shell/?chat={chat_id}&focus=question",
      tag=_OWNER_INPUT_NOTIFICATION_TAG,
      notification_id=str(uuid5(NAMESPACE_URL, f"owner-input:{question_id}")),
    )
