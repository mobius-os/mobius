"""Safe shell-level owner-input event contract."""

from app import owner_input


def test_owner_input_event_keeps_question_identity_optional(monkeypatch):
  events = []

  class _SystemEvents:
    def publish(self, event):
      events.append(event)

  monkeypatch.setattr(
    owner_input,
    "get_system_broadcast",
    lambda: _SystemEvents(),
  )

  owner_input.publish_owner_input_changed("chat-1", "secure_input")
  owner_input.publish_owner_input_changed(
    "chat-2",
    "question",
    question_id="question-1",
  )
  owner_input.publish_owner_input_changed(
    "chat-2",
    None,
    question_id=None,
  )

  assert events == [
    {
      "type": "chat_owner_input_changed",
      "chatId": "chat-1",
      "inputKind": "secure_input",
    },
    {
      "type": "chat_owner_input_changed",
      "chatId": "chat-2",
      "inputKind": "question",
      "questionId": "question-1",
    },
    {
      "type": "chat_owner_input_changed",
      "chatId": "chat-2",
      "inputKind": None,
      "questionId": None,
    },
  ]
