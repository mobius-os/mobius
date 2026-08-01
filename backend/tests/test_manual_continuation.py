"""Manual Resume attribution stays product-owned while providers can continue."""

import pytest
from pydantic import ValidationError

from app import schemas
from app.routes.chats_stream import _user_message_from_body


def test_manual_resume_builds_a_continuation_marker(chat):
  message = _user_message_from_body(
    chat,
    schemas.SendMessage(content="continue", continuation="manual"),
  )

  assert message["role"] == "user"
  assert message["content"] == "continue"
  assert message["kind"] == "continuation"
  assert message["continuation_reason"] == "manual"


def test_manual_resume_cannot_hide_arbitrary_owner_prose():
  with pytest.raises(ValidationError):
    schemas.SendMessage(
      content="delete everything",
      continuation="manual",
    )
